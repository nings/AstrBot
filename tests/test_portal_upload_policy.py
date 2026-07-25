"""Guardrail tests for the portal module's Phase 2 upload policy enforcement.

Covers ``PortalSettings``' upload gates (``astrbot/portal/config.py``) as
enforced by ``astrbot.portal.services.attachment_service.upload_file`` and
surfaced through ``POST /api/v1/portal/files``
(``astrbot/portal/api/file_routes.py``):

* Single-file size cap (``max_upload_file_bytes``) rejected before a byte is
  written to core's shared attachments directory.
* MIME-type allowlist (``allowed_upload_mime_types``).
* Per-user rolling daily byte quota (``max_daily_upload_bytes``, backed by
  ``PortalUsageCounter.uploaded_bytes``) -- a quota-exceeded upload returns
  429 with the remaining-quota figure in the body, per this phase's
  contract (``PortalUsageCounter.uploaded_bytes``'s own docstring in
  ``db/models.py``), and quota accounting is strictly per-user (one user's
  uploads never consume another user's quota, since each row is keyed by
  ``(user_id, window_start)``).
* CSRF double-submit and authentication are enforced on the upload route
  exactly like every other portal write route.

None of core (``ChatService.save_uploaded_file``) enforces any of the above
-- verified ground truth: no size limit, no MIME allowlist anywhere in core.
The portal is the only layer that ever will, so these tests exercise
``PortalSettings`` overrides (via ``dataclasses.replace``, monkeypatched
onto ``attachment_service.get_portal_settings``) to keep the byte counts
in each test small and fast rather than uploading real megabyte-sized
payloads against the real (20 MiB / 200 MiB) defaults.

App/fixture plumbing intentionally mirrors
``tests/test_portal_attachment_isolation.py`` (same ``FakeChatService``
shape, same temp-portal-database wiring) -- duplicated rather than shared,
matching this test suite's existing per-file fixture style (see
``tests/test_portal_auth.py`` and ``tests/test_portal_chat_isolation.py``,
neither of which shares fixtures via conftest.py either).
"""

from __future__ import annotations

import asyncio
import dataclasses
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.portal.api import chat_routes
from astrbot.portal.api.router import portal_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalUser
from astrbot.portal.services import attachment_service, core_facade

PORTAL_TEST_SECRET = "portal-upload-policy-test-secret-32bytes!"


# ---------------------------------------------------------------------------
# FakeChatService -- identical shape/contract to the one in
# test_portal_attachment_isolation.py; duplicated (not imported) since
# tests/ has no shared conftest fixture for portal fakes and each Phase 1
# sibling file follows the same duplication pattern.
# ---------------------------------------------------------------------------


class _FakeCoreAttachment:
    def __init__(self, attachment_id: str, path: str, attach_type: str, mime_type: str) -> None:
        self.attachment_id = attachment_id
        self.path = path
        self.type = attach_type
        self.mime_type = mime_type


def _attach_type_for_content_type(content_type: str) -> str:
    if content_type.startswith("image/"):
        return "image"
    if content_type.startswith("audio/"):
        return "record"
    if content_type.startswith("video/"):
        return "video"
    return "file"


class _FakeDb:
    def __init__(self) -> None:
        self.attachments_by_id: dict[str, _FakeCoreAttachment] = {}

    async def get_attachment_by_id(self, attachment_id: str) -> _FakeCoreAttachment | None:
        return self.attachments_by_id.get(attachment_id)

    async def delete_attachment(self, attachment_id: str) -> None:
        self.attachments_by_id.pop(attachment_id, None)


class FakeChatService:
    def __init__(self, attachments_dir: Path) -> None:
        self.attachments_dir = attachments_dir
        self.db = _FakeDb()
        self.save_uploaded_file_calls: list[str] = []

    async def save_uploaded_file(self, upload: Any) -> dict[str, Any]:
        self.save_uploaded_file_calls.append(upload.filename)
        dest = self.attachments_dir / upload.filename
        await upload.save(str(dest))
        attachment_id = str(uuid.uuid4())
        content_type = getattr(upload, "content_type", None) or "application/octet-stream"
        self.db.attachments_by_id[attachment_id] = _FakeCoreAttachment(
            attachment_id=attachment_id,
            path=str(dest),
            attach_type=_attach_type_for_content_type(content_type),
            mime_type=content_type,
        )
        return {"attachment_id": attachment_id}


# ---------------------------------------------------------------------------
# App / client plumbing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-upload-policy-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def attachments_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "core-attachments"
    directory.mkdir()
    return directory


@pytest.fixture
def fake_chat_service(attachments_dir: Path) -> FakeChatService:
    return FakeChatService(attachments_dir)


@pytest.fixture
def app(portal_sessionmaker, fake_chat_service: FakeChatService, monkeypatch) -> FastAPI:
    from types import SimpleNamespace

    application = FastAPI()
    application.include_router(portal_router, prefix="/api/v1")
    application.state.services = SimpleNamespace(chat=fake_chat_service)
    application.state.portal_jwt_secret = PORTAL_TEST_SECRET

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    monkeypatch.setattr(chat_routes, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(attachment_service, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(core_facade, "portal_async_sessionmaker", portal_sessionmaker)

    return application


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _insert_user(sessionmaker, *, user_id: str) -> None:
    async with sessionmaker() as session:
        session.add(
            PortalUser(
                user_id=user_id,
                email=f"{user_id}@example.test",
                password_hash=None,
                display_name=user_id,
                auth_method="local",
                role="user",
                status="active",
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def two_users(portal_sessionmaker) -> tuple[str, str]:
    user_a = f"user-a-{uuid.uuid4().hex}"
    user_b = f"user-b-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    await _insert_user(portal_sessionmaker, user_id=user_b)
    return user_a, user_b


def _auth(user_id: str) -> tuple[dict, dict]:
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


async def _upload(
    client: httpx.AsyncClient,
    cookies: dict,
    headers: dict,
    *,
    filename: str = "upload.txt",
    content: bytes = b"hello world",
    content_type: str = "text/plain",
) -> httpx.Response:
    return await client.post(
        "/api/v1/portal/files",
        files={"file": (filename, content, content_type)},
        cookies=cookies,
        headers=headers,
    )


def _override_settings(monkeypatch, **overrides: Any) -> None:
    """Monkeypatch ``attachment_service.get_portal_settings`` (the
    module-level name that module's own functions call) to return a
    ``dataclasses.replace``d copy of the real settings with the given
    field overrides -- lets each test shrink the size/quota caps to a few
    bytes instead of uploading real megabyte-sized payloads."""
    base = get_portal_settings()
    custom = dataclasses.replace(base, **overrides)
    monkeypatch.setattr(attachment_service, "get_portal_settings", lambda: custom)


# ---------------------------------------------------------------------------
# 6. A file over the single-file size cap is rejected before it is written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_oversized_file_is_rejected_before_being_written(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    fake_chat_service: FakeChatService,
    monkeypatch,
):
    _override_settings(monkeypatch, max_upload_file_bytes=10)
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    response = await _upload(client, cookies, headers, content=b"x" * 50)

    assert response.status_code == 400, response.text
    # Never reached core's write path at all.
    assert fake_chat_service.save_uploaded_file_calls == []
    assert fake_chat_service.db.attachments_by_id == {}


# ---------------------------------------------------------------------------
# 7. A MIME type outside the allowlist is rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disallowed_mime_type_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    response = await _upload(
        client,
        cookies,
        headers,
        filename="payload.zip",
        content=b"PK\x03\x04not-a-real-zip-but-irrelevant",
        content_type="application/zip",
    )

    assert response.status_code == 415, response.text
    assert fake_chat_service.save_uploaded_file_calls == []


@pytest.mark.asyncio
async def test_disallowed_svg_mime_type_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str], fake_chat_service: FakeChatService
):
    """SVG is deliberately excluded from the allowlist (can carry embedded
    ``<script>``, an XSS vector if ever served/previewed back) -- see
    ``PortalSettings.allowed_upload_mime_types``'s own docstring."""
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    response = await _upload(
        client,
        cookies,
        headers,
        filename="evil.svg",
        content=b"<svg onload='alert(1)'></svg>",
        content_type="image/svg+xml",
    )

    assert response.status_code == 415, response.text
    assert fake_chat_service.save_uploaded_file_calls == []


# ---------------------------------------------------------------------------
# 8. Exceeding the per-user daily byte quota returns 429 and conveys
#    remaining quota.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_exceeded_returns_429_with_remaining_quota(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    fake_chat_service: FakeChatService,
    monkeypatch,
):
    _override_settings(monkeypatch, max_daily_upload_bytes=20)
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    response = await _upload(client, cookies, headers, content=b"x" * 25)

    assert response.status_code == 429, response.text
    detail = response.json()["detail"]
    assert detail["remaining_quota_bytes"] == 20
    assert "message" in detail
    # The rejected upload must never have reached core's write path.
    assert fake_chat_service.save_uploaded_file_calls == []


# ---------------------------------------------------------------------------
# 9. Quota accounting is per-user: A's uploads do not consume B's quota.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_quota_accounting_is_per_user(
    client: httpx.AsyncClient, two_users: tuple[str, str], monkeypatch
):
    _override_settings(monkeypatch, max_daily_upload_bytes=30)
    user_a, user_b = two_users
    cookies_a, headers_a = _auth(user_a)
    cookies_b, headers_b = _auth(user_b)

    # A spends 25 of their 30-byte daily quota.
    first = await _upload(client, cookies_a, headers_a, content=b"a" * 25)
    assert first.status_code == 201, first.text
    assert first.json()["remaining_quota_bytes"] == 5

    # A's second upload exceeds their own remaining quota (5 bytes left).
    second = await _upload(client, cookies_a, headers_a, content=b"a" * 25)
    assert second.status_code == 429, second.text

    # B, a completely different user, still has their full 30-byte quota
    # untouched by anything A did.
    third = await _upload(client, cookies_b, headers_b, content=b"b" * 25)
    assert third.status_code == 201, third.text
    assert third.json()["remaining_quota_bytes"] == 5


# ---------------------------------------------------------------------------
# 10. An anonymous request and a request missing the CSRF header are both
#     rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_anonymous_upload_request_is_rejected(client: httpx.AsyncClient):
    response = await client.post(
        "/api/v1/portal/files",
        files={"file": ("a.txt", b"hello", "text/plain")},
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_upload_missing_csrf_header_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    cookies, _headers = _auth(user_a)

    response = await client.post(
        "/api/v1/portal/files",
        files={"file": ("a.txt", b"hello", "text/plain")},
        cookies=cookies,
        # No X-Portal-CSRF-Token header, even though the CSRF cookie is
        # present -- the double-submit check must still reject this.
    )
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# 11. Concurrent uploads cannot exceed the daily quota (lost-update race).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_exceed_the_daily_quota(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    monkeypatch,
):
    """Fire more concurrent uploads than the quota allows; only the fitting ones win.

    This pins the SERVICE, not just the SQL shape. The reservation was
    originally a read-then-write (read uploaded_bytes, compare to the cap,
    write back total + size), so concurrent requests all read the same
    pre-increment baseline, all passed the check, and a burst could exceed the
    cap by roughly (concurrent requests x file size). It is now one conditional
    UPDATE whose WHERE clause carries the comparison.

    Reverting attachment_service.upload_file to read-then-write must make this
    test fail, which is the whole point of asserting on the committed total.
    """
    from sqlmodel import col, select

    from astrbot.portal.db.models import PortalUsageCounter

    payload = b"x" * 100
    cap = 300  # exactly 3 uploads fit
    attempts = 8
    _override_settings(
        monkeypatch, max_upload_file_bytes=1000, max_daily_upload_bytes=cap
    )

    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    responses = await asyncio.gather(
        *(
            _upload(
                client,
                cookies,
                headers,
                filename=f"burst-{i}.txt",
                content=payload,
            )
            for i in range(attempts)
        )
    )

    accepted = [r for r in responses if r.status_code == 201]
    rejected = [r for r in responses if r.status_code == 429]

    assert len(accepted) == cap // len(payload), (
        f"expected exactly {cap // len(payload)} uploads to fit under the "
        f"{cap}-byte cap, got {len(accepted)} "
        f"(statuses: {[r.status_code for r in responses]})"
    )
    assert len(accepted) + len(rejected) == attempts, (
        "every request must be either accepted or quota-rejected, got "
        f"{[r.status_code for r in responses]}"
    )

    async with portal_sessionmaker() as session:
        total = (
            await session.execute(
                select(PortalUsageCounter.uploaded_bytes).where(
                    col(PortalUsageCounter.user_id) == user_a
                )
            )
        ).scalar_one()
    assert total <= cap, f"committed total {total} exceeded the cap {cap}"
