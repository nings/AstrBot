from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import mimetypes
import re
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import jwt
from fastapi import Depends, FastAPI, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from markitdown_no_magika import MarkItDown
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from .config import Plan, Settings
from .store import PUBLIC_FILE_FIELDS, Store

logger = logging.getLogger("astrbot_gateway")

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
DOCUMENT_SUFFIXES = {
    ".pdf",
    ".docx",
    ".pptx",
    ".xlsx",
    ".xls",
    ".csv",
    ".txt",
    ".md",
    ".json",
    ".html",
}
SAFE_SUB = re.compile(r"[A-Za-z0-9_.-]{1,64}")
CHUNK_SIZE = 1024 * 1024


@dataclass(frozen=True)
class User:
    username: str
    plan: Plan


class ChatRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)
    session_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,64}$")
    file_ids: list[str] = Field(default_factory=list, max_length=10)


def create_app(
    settings: Settings,
    astrbot_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    """Build the gateway app.

    Args:
        settings: Gateway settings.
        astrbot_transport: Optional transport for the AstrBot client, used by
            tests to replace the real AstrBot server.

    Returns:
        The FastAPI application.
    """
    store = Store(settings.data_dir / "gateway.db")
    files_dir = settings.data_dir / "files"
    max_file_bytes = settings.max_file_mb * 1024 * 1024

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await store.open()
        app.state.astrbot = httpx.AsyncClient(
            base_url=settings.astrbot_base_url,
            headers={"X-API-Key": settings.astrbot_api_key},
            timeout=httpx.Timeout(10.0, read=600.0),
            transport=astrbot_transport,
        )

        async def cleanup_expired_files() -> None:
            while True:
                try:
                    for record in await store.expired_files(
                        settings.file_ttl_days * 86400
                    ):
                        Path(record["path"]).unlink(missing_ok=True)
                        if record["text_path"]:
                            Path(record["text_path"]).unlink(missing_ok=True)
                        await store.delete_file(record["owner"], record["file_id"])
                except Exception:
                    logger.exception("Failed to clean up expired files")
                await asyncio.sleep(3600)

        cleanup_task = (
            asyncio.create_task(cleanup_expired_files())
            if settings.file_ttl_days > 0
            else None
        )
        try:
            yield
        finally:
            if cleanup_task:
                cleanup_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await cleanup_task
            await app.state.astrbot.aclose()
            await store.close()

    app = FastAPI(title="AstrBot Gateway", lifespan=lifespan)

    async def current_user(request: Request) -> User:
        """Authenticate the caller with an HS256 JWT issued by your auth system.

        The AstrBot username is always derived from the token's `sub` claim and
        never taken from the request body, so callers cannot impersonate other
        users or AstrBot admins.

        Args:
            request: Incoming request carrying `Authorization: Bearer <jwt>`.

        Returns:
            The authenticated user with their plan.

        Raises:
            HTTPException: 401 for a missing or invalid token, 403 for an
                unknown plan.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            raise HTTPException(401, "Missing bearer token")
        try:
            claims = jwt.decode(
                auth_header.removeprefix("Bearer ").strip(),
                settings.jwt_secret,
                algorithms=["HS256"],
                audience=settings.jwt_audience,
                issuer=settings.jwt_issuer,
                options={"require": ["sub", "exp"]},
            )
        except jwt.InvalidTokenError as exc:
            raise HTTPException(401, f"Invalid token: {exc}") from exc

        sub = str(claims["sub"])
        # Readable usernames for simple IDs, hashed ones otherwise. The distinct
        # prefixes keep the two forms from colliding.
        username = (
            f"u_{sub}"
            if SAFE_SUB.fullmatch(sub)
            else f"h_{hashlib.sha256(sub.encode()).hexdigest()[:32]}"
        )
        plan_name = claims.get("plan") or settings.default_plan
        if plan_name not in settings.plans:
            raise HTTPException(403, f"Unknown plan: {plan_name}")
        return User(username=username, plan=settings.plans[plan_name])

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.post("/v1/files")
    async def upload_file(
        request: Request,
        file: UploadFile,
        user: User = Depends(current_user),
    ):
        name = Path((file.filename or "").replace("\\", "/")).name[:255] or "file"
        suffix = Path(name).suffix.lower()
        if suffix in IMAGE_SUFFIXES:
            kind = "image"
        elif suffix in DOCUMENT_SUFFIXES:
            kind = "document"
        else:
            raise HTTPException(415, f"Unsupported file type: {suffix or name}")

        # Stored under a per-user folder with a random name, so files with the
        # same original name never overwrite each other.
        file_id = uuid.uuid4().hex
        user_dir = files_dir / user.username
        user_dir.mkdir(parents=True, exist_ok=True)
        path = user_dir / f"{file_id}{suffix}"
        text_path = None
        astrbot_attachment_id = None
        mime_type = (
            file.content_type
            or mimetypes.guess_type(name)[0]
            or "application/octet-stream"
        )
        try:
            size = 0
            with path.open("wb") as out:
                while chunk := await file.read(CHUNK_SIZE):
                    size += len(chunk)
                    if size > max_file_bytes:
                        raise HTTPException(
                            413, f"File exceeds {settings.max_file_mb} MB"
                        )
                    out.write(chunk)
            if size == 0:
                raise HTTPException(400, "File is empty")
            if (
                await store.storage_used(user.username) + size
                > user.plan.storage_mb * 1024 * 1024
            ):
                raise HTTPException(413, "Storage quota exceeded")

            if kind == "document":
                # Extract text here so the file itself never reaches AstrBot.
                try:
                    result = await asyncio.to_thread(MarkItDown().convert, str(path))
                    text = (result.text_content or "").strip()
                except Exception as exc:
                    logger.warning("Text extraction failed for %s: %s", path, exc)
                    text = ""
                if not text:
                    raise HTTPException(422, "No text could be extracted from file")
                text_path = path.with_name(f"{file_id}.extracted.md")
                text_path.write_text(text, encoding="utf-8")
            else:
                # Images go to AstrBot under the random name. Clients never see
                # AstrBot's attachment ID, so they can only reach it through
                # their own gateway file ID.
                resp = await request.app.state.astrbot.post(
                    "/api/v1/file",
                    files={"file": (path.name, path.read_bytes(), mime_type)},
                )
                body = resp.json()
                if body.get("status") != "ok":
                    logger.error("AstrBot rejected upload: %s", body.get("message"))
                    raise HTTPException(502, "Upstream upload failed")
                astrbot_attachment_id = body["data"]["attachment_id"]
        except BaseException:
            path.unlink(missing_ok=True)
            if text_path:
                text_path.unlink(missing_ok=True)
            raise

        record = {
            "file_id": file_id,
            "owner": user.username,
            "name": name,
            "kind": kind,
            "mime_type": mime_type,
            "size": size,
            "path": str(path),
            "text_path": str(text_path) if text_path else None,
            "astrbot_attachment_id": astrbot_attachment_id,
            "created_at": time.time(),
        }
        await store.add_file(record)
        return {key: record[key] for key in PUBLIC_FILE_FIELDS}

    @app.get("/v1/files")
    async def list_files(user: User = Depends(current_user)):
        return {
            "files": [
                {key: record[key] for key in PUBLIC_FILE_FIELDS}
                for record in await store.list_files(user.username)
            ]
        }

    @app.get("/v1/files/{file_id}/content")
    async def download_file(file_id: str, user: User = Depends(current_user)):
        records = await store.get_files(user.username, [file_id])
        if not records:
            raise HTTPException(404, "File not found")
        return FileResponse(
            records[0]["path"],
            media_type=records[0]["mime_type"],
            filename=records[0]["name"],
        )

    @app.delete("/v1/files/{file_id}")
    async def delete_file(file_id: str, user: User = Depends(current_user)):
        records = await store.get_files(user.username, [file_id])
        if not records:
            raise HTTPException(404, "File not found")
        Path(records[0]["path"]).unlink(missing_ok=True)
        if records[0]["text_path"]:
            Path(records[0]["text_path"]).unlink(missing_ok=True)
        await store.delete_file(user.username, file_id)
        return {"deleted": file_id}

    @app.post("/v1/chat")
    async def chat(
        payload: ChatRequest,
        request: Request,
        user: User = Depends(current_user),
    ):
        file_ids = list(dict.fromkeys(payload.file_ids))
        records = await store.get_files(user.username, file_ids)
        if len(records) != len(file_ids):
            raise HTTPException(404, "File not found")
        if not await store.consume_chat(user.username, user.plan.daily_chat_limit):
            raise HTTPException(429, "Daily chat limit reached")

        parts = []
        for record in records:
            if record["kind"] == "image":
                parts.append(
                    {
                        "type": "image",
                        "attachment_id": record["astrbot_attachment_id"],
                        "filename": record["name"],
                    }
                )
                continue
            text = Path(record["text_path"]).read_text(encoding="utf-8")
            if len(text) > settings.max_doc_chars:
                text = (
                    text[: settings.max_doc_chars]
                    + f"\n[Truncated: showing the first {settings.max_doc_chars} characters]"
                )
            parts.append(
                {
                    "type": "plain",
                    "text": f"<document name={json.dumps(record['name'], ensure_ascii=False)}>\n"
                    f"{text}\n</document>\n",
                }
            )
        parts.append({"type": "plain", "text": payload.message})

        # Only these fields are sent upstream. Fields such as selected_provider
        # or _skip_user_history are never forwarded from clients.
        session_id = payload.session_id or uuid.uuid4().hex
        upstream_body = {
            "username": user.username,
            "session_id": session_id,
            "message": parts,
        }
        if user.plan.config_id:
            upstream_body["config_id"] = user.plan.config_id

        client: httpx.AsyncClient = request.app.state.astrbot
        resp = await client.send(
            client.build_request("POST", "/api/v1/chat", json=upstream_body),
            stream=True,
        )
        if "text/event-stream" not in resp.headers.get("content-type", ""):
            # AstrBot reports request errors as a JSON envelope instead of SSE.
            raw = await resp.aread()
            await resp.aclose()
            try:
                message = json.loads(raw).get("message") or "Upstream error"
            except (ValueError, AttributeError):
                message = "Upstream error"
            logger.warning("AstrBot chat error for %s: %s", user.username, message)
            return JSONResponse({"detail": message}, status_code=400)

        return StreamingResponse(
            resp.aiter_raw(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Session-Id": session_id},
            background=BackgroundTask(resp.aclose),
        )

    @app.get("/v1/chat/sessions")
    async def list_sessions(
        request: Request,
        page: int = Query(1, ge=1),
        page_size: int = Query(20, ge=1, le=100),
        user: User = Depends(current_user),
    ):
        resp = await request.app.state.astrbot.get(
            "/api/v1/chat/sessions",
            params={"username": user.username, "page": page, "page_size": page_size},
        )
        body = resp.json()
        if body.get("status") != "ok":
            raise HTTPException(502, body.get("message") or "Upstream error")
        return body["data"]

    return app
