"""End-user chat HTTP routes for the AstrBot Portal module.

Every handler here goes through :mod:`astrbot.portal.services.core_facade` --
the single, audited seam that is allowed to reach ``ChatService``. This file
must never import ``ChatService``, ``ConversationService``,
``SessionManagementService``, ``conv_mgr``, or any other capability-wall
member directly (rule 5 of the module's engineering rules); doing so would
fail the import-linter contract. Every ``user_id`` used below comes from
:func:`astrbot.portal.auth.dependencies.get_current_user` -- never from a
request body, query string, or header -- so the ownership checks already
built into ``core_facade``/``ChatService`` (plain ``creator``/``username``
string comparisons) give per-user isolation "for free".

Two deliberate Phase 1 security-scoping decisions beyond the assignment's
literal endpoint list, both flowing directly from facts already verified
for this module:

1. **No client-supplied provider/model routing.** Neither
   :class:`SendMessageRequest` nor any other request model below declares a
   ``selected_provider``/``selected_model`` field, and every one of them
   sets ``model_config = ConfigDict(extra="ignore")`` -- so even if a
   client includes those keys in the JSON body, pydantic silently drops
   them before this module ever sees them. Nothing here ever reads,
   echoes, or forwards those two keys (see
   ``astr_main_agent.py:223-237``: ``_select_provider`` trusts them with no
   authorization check at all).
2. **Text-only message bodies in Phase 1.** ``SendMessageRequest.message``
   is typed as plain ``str``, not the ``str | list`` union
   ``core_facade.send_message`` itself accepts, and
   :func:`_ensure_text_only_content` rejects any non-``"plain"`` part in a
   message-edit payload. Rationale: the core webchat message-parts pipeline
   (``astrbot.core.platform.sources.webchat.message_parts_helper
   .build_webchat_message_parts``) resolves any non-``plain``/``reply``
   part purely by ``attachment_id`` via ``get_attachment_by_id`` -- and
   ``Attachment`` has no owner column at all (verified ground truth: see
   ``astrbot/core/db/po.py:340-361``). Phase 1 ships no upload endpoint
   (that is explicitly Phase 2 work), so the *only* way a list-shaped or
   attachment-referencing payload could reach that code path in this phase
   is through these two routes. Accepting it here would let any
   authenticated portal user embed and have rendered an attachment_id
   belonging to a *different* user/dashboard admin -- a cross-tenant IDOR
   with no ownership check anywhere in the chain. Rejecting non-text
   payloads at the route boundary closes that off entirely until Phase 2
   adds real per-user attachment ownership.

Per-user concurrent-run limiting (the "CONTRACT for chat_routes.py" in
``auth/rate_limit.py``'s module docstring) is implemented in
:func:`_acquire_run_slot`/:func:`_release_run_slot`: the DB-backed
``PortalUsageCounter`` row is the authoritative check-and-increment (a single
transaction, upsert-on-conflict to avoid the documented
``UniqueConstraint(user_id, window_start)`` race), backed by the
process-local ``InProcessConcurrencyGuard`` as a fast-path/defense-in-depth
layer on top. Both are released when the SSE stream this route serves
finishes (normally, on error, or on client disconnect), via
:func:`_guarded_run_stream`'s ``finally`` block -- not when some notion of
"the underlying ChatService run" finishes, which this module has no direct
visibility into.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime, timezone
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from astrbot import logger
from astrbot.portal.auth.dependencies import get_current_user, require_csrf
from astrbot.portal.auth.rate_limit import (
    can_start_run_for_counter,
    get_concurrency_guard,
)
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalUsageCounter, PortalUser
from astrbot.portal.services import core_facade
from astrbot.portal.services.core_facade import (
    CoreFacadeError,
    ResourceNotFoundError,
    ValidationError,
)

router = APIRouter(prefix="/chat", tags=["Portal Chat"])
"""Mounted by ``astrbot.portal.api.router.portal_router`` (prefix ``/portal``),
itself mounted under ``/api/v1`` by the dashboard's ``build_api_router`` --
so every path below ends up public at ``/api/v1/portal/chat/...``, matching
this assignment's endpoint list verbatim. This router must NOT declare its
own ``/portal`` prefix (that would double it up to ``/portal/portal/chat/...``
once the aggregate router in ``api/router.py`` applies its own)."""

_SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "Transfer-Encoding": "chunked",
    "Connection": "keep-alive",
}
"""Identical to the header set ``astrbot.dashboard.api.chat._send_chat`` uses
for its ``StreamingResponse`` -- keeping these byte-identical is part of
preserving the SSE contract the reused frontend renderer depends on."""


# ---------------------------------------------------------------------------
# Request models. Every one of them sets extra="ignore" so stray client
# fields (most importantly selected_provider/selected_model -- see module
# docstring, invariant 1) are silently dropped rather than stored or
# accessible anywhere in this module.
# ---------------------------------------------------------------------------


class SessionRenameRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    display_name: str = Field(min_length=1, max_length=200)


class BatchDeleteRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    session_ids: list[str] = Field(min_length=1, max_length=200)


class SendMessageRequest(BaseModel):
    """See module docstring, invariant 2, for why ``message`` is ``str``
    only rather than ``core_facade.send_message``'s full ``str | list``."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1)
    flags: dict[str, bool] | None = None


class MessageEditRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    content: dict[str, Any]


def _ensure_text_only_content(content: dict[str, Any]) -> None:
    """Reject a message-edit payload containing any non-``"plain"`` part.

    See module docstring, invariant 2. ``ChatService.update_message``'s own
    ``sanitize_message_content`` only checks that ``content.message`` is a
    list and strips ``path`` fields -- it does **not** re-resolve each
    part's ``attachment_id`` against ``get_attachment_by_id`` the way the
    send-message path does, so it provides no ownership check at all for
    whatever shape the client hands it.
    """
    parts = content.get("message")
    if not isinstance(parts, list) or not parts:
        raise HTTPException(
            status_code=400, detail="content.message must be a non-empty list"
        )
    for part in parts:
        if not isinstance(part, dict) or part.get("type") != "plain":
            raise HTTPException(
                status_code=400,
                detail="Only plain-text message parts are supported in this phase",
            )


# ---------------------------------------------------------------------------
# core_facade error translation. ResourceNotFoundError -> 404 (never 403,
# so ownership failures are indistinguishable from "does not exist" --
# rule 3 of core_facade's own security checklist). ValidationError -> 400.
# ---------------------------------------------------------------------------


def _raise_for_core_error(exc: CoreFacadeError) -> NoReturn:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(exc, ValidationError):
        raise HTTPException(
            status_code=400, detail=str(exc) or "Invalid request"
        ) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Per-user concurrent chat-run limiting. DB-backed PortalUsageCounter is the
# authoritative, restart-surviving check-and-increment; InProcessConcurrency
# Guard is a same-process fast-path/defense-in-depth layer on top of it, per
# auth/rate_limit.py's module docstring. Both use fresh, self-managed DB
# sessions (not a FastAPI Depends()-injected one) because the slot must be
# released from inside the SSE stream's own teardown, long after any
# request-scoped dependency would already have been torn down.
# ---------------------------------------------------------------------------


def _window_start(now: datetime | None = None) -> datetime:
    settings = get_portal_settings()
    window_seconds = max(int(settings.usage_window_seconds), 1)
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(
        (epoch // window_seconds) * window_seconds, tz=timezone.utc
    )


async def _load_or_create_counter(
    session, user_id: str, window_start: datetime
) -> PortalUsageCounter | None:
    """Fetch this user's usage-counter row for ``window_start``, creating it
    on first use. Returns ``None`` if a concurrent insert raced us twice in
    a row (the caller must treat that as "try again later", not a bug)."""
    for _ in range(2):
        result = await session.execute(
            select(PortalUsageCounter).where(
                PortalUsageCounter.user_id == user_id,
                PortalUsageCounter.window_start == window_start,
            )
        )
        counter = result.scalar_one_or_none()
        if counter is not None:
            return counter

        counter = PortalUsageCounter(
            user_id=user_id,
            window_start=window_start,
            request_count=0,
            concurrent_runs=0,
        )
        session.add(counter)
        try:
            await session.flush()
            return counter
        except IntegrityError:
            # Another concurrent request for the same (user_id, window_start)
            # inserted first (UniqueConstraint race) -- re-fetch instead of
            # blind-inserting again.
            await session.rollback()
    return None


async def _acquire_run_slot(user_id: str) -> datetime:
    """Reserve one concurrent-run slot for ``user_id``.

    Raises ``HTTPException(429)`` if the user is already at their
    concurrent-run cap (``PortalSettings.max_concurrent_runs_per_user``) on
    either the DB-backed or in-process layer. Returns the ``window_start``
    used, so the caller releases against the exact same row later even if
    the accounting window rolls over mid-run.
    """
    settings = get_portal_settings()
    window_start = _window_start()

    async with portal_async_sessionmaker() as session:
        counter = await _load_or_create_counter(session, user_id, window_start)
        if counter is None:
            raise HTTPException(status_code=429, detail="请求过于频繁，请稍后重试")
        if not can_start_run_for_counter(counter, settings=settings):
            await session.rollback()
            raise HTTPException(
                status_code=429, detail="并发对话数已达上限，请稍后重试"
            )
        counter.concurrent_runs += 1
        counter.request_count += 1
        await session.commit()

    if not await get_concurrency_guard().try_acquire(user_id):
        # DB slot was granted but the in-process guard is already full
        # (e.g. stale DB counters after a mid-process crash) -- release the
        # DB slot we just took and fail closed rather than over-admit.
        await _release_run_slot(user_id, window_start, release_in_process=False)
        raise HTTPException(status_code=429, detail="并发对话数已达上限，请稍后重试")

    return window_start


async def _release_run_slot(
    user_id: str, window_start: datetime, *, release_in_process: bool = True
) -> None:
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalUsageCounter).where(
                PortalUsageCounter.user_id == user_id,
                PortalUsageCounter.window_start == window_start,
            )
        )
        counter = result.scalar_one_or_none()
        if counter is not None and counter.concurrent_runs > 0:
            counter.concurrent_runs -= 1
            await session.commit()
        else:
            await session.rollback()

    if release_in_process:
        await get_concurrency_guard().release(user_id)


def _guarded_run_stream(
    stream: AsyncIterator[str], user_id: str, window_start: datetime
) -> AsyncIterator[str]:
    """Wrap an SSE stream so the concurrency slot acquired for it is always
    released once the stream ends -- normal completion, an error inside the
    generator, or the client disconnecting (which raises ``GeneratorExit``
    at the ``async for`` below, still caught by ``finally``)."""

    async def _wrapped() -> AsyncIterator[str]:
        try:
            async for chunk in stream:
                yield chunk
        finally:
            try:
                await _release_run_slot(user_id, window_start)
            except Exception:
                logger.exception(
                    f"[portal] failed to release chat-run concurrency slot "
                    f"for user {user_id}"
                )

    return _wrapped()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


@router.get("/sessions")
async def list_chat_sessions(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        sessions = await core_facade.list_sessions(current_user.user_id, request)
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)

    total = len(sessions)
    start = (page - 1) * page_size
    page_items = sessions[start : start + page_size]
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    return {
        "sessions": page_items,
        "pagination": {
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        },
    }


@router.post("/sessions", status_code=201)
async def create_chat_session(
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await core_facade.create_session(current_user.user_id, request)
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.post("/sessions/batch-delete")
async def batch_delete_chat_sessions(
    payload: BatchDeleteRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await core_facade.batch_delete_sessions(
            current_user.user_id, request, payload.session_ids
        )
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.get("/sessions/{session_id}")
async def get_chat_session(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    try:
        return await core_facade.get_owned_session(
            current_user.user_id, request, session_id
        )
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.patch("/sessions/{session_id}")
async def rename_chat_session(
    session_id: str,
    payload: SessionRenameRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await core_facade.rename_session(
            current_user.user_id, request, session_id, payload.display_name
        )
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.delete("/sessions/{session_id}", status_code=204)
async def delete_chat_session(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> None:
    try:
        await core_facade.delete_session(current_user.user_id, request, session_id)
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


# ---------------------------------------------------------------------------
# Messaging / streaming. Frame contract preserved verbatim: `data:
# {json}\n\n`, heartbeat `: heartbeat\n\n`, frame types
# plain/image/record/file/video/end/complete/agent_stats/attachment_saved/
# message_saved/error (see chat_service.py:787-1046) -- these routes never
# touch the stream's bytes, only pass the AsyncIterator straight into
# StreamingResponse, so the reused frontend renderer works unchanged.
# ---------------------------------------------------------------------------


@router.post("/sessions/{session_id}/messages")
async def send_chat_message(
    session_id: str,
    payload: SendMessageRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> StreamingResponse:
    user_id = current_user.user_id
    window_start = await _acquire_run_slot(user_id)
    try:
        stream = await core_facade.send_message(
            user_id, request, session_id, payload.message, flags=payload.flags
        )
    except CoreFacadeError as exc:
        await _release_run_slot(user_id, window_start)
        _raise_for_core_error(exc)
    except Exception:
        await _release_run_slot(user_id, window_start)
        raise

    return StreamingResponse(
        _guarded_run_stream(stream, user_id, window_start),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


@router.get("/runs/{run_id}/stream")
async def stream_chat_run(
    run_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> StreamingResponse:
    """SSE resume/attach endpoint. ``run_id`` is attacker-guessable, so
    ownership is verified by ``core_facade.stream_run`` synchronously --
    before this function ever constructs a ``StreamingResponse`` -- and a
    mismatch surfaces as ``ResourceNotFoundError`` -> HTTP 404 (never 403),
    so not a single byte of another user's run is ever emitted."""
    try:
        stream = await core_facade.stream_run(current_user.user_id, request, run_id)
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)

    return StreamingResponse(
        stream, media_type="text/event-stream", headers=_SSE_HEADERS
    )


@router.post("/sessions/{session_id}/stop")
async def stop_chat_session(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    try:
        return await core_facade.stop_run(current_user.user_id, request, session_id)
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.patch("/sessions/{session_id}/messages/{message_id}")
async def edit_chat_message(
    session_id: str,
    message_id: int,
    payload: MessageEditRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    if payload.content.get("type") != "user":
        raise HTTPException(
            status_code=400, detail="Only user messages can be edited"
        )
    _ensure_text_only_content(payload.content)
    try:
        return await core_facade.update_message(
            current_user.user_id, request, session_id, message_id, payload.content
        )
    except CoreFacadeError as exc:
        _raise_for_core_error(exc)


@router.post("/sessions/{session_id}/messages/{message_id}/regenerate")
async def regenerate_chat_message(
    session_id: str,
    message_id: int,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> StreamingResponse:
    user_id = current_user.user_id
    window_start = await _acquire_run_slot(user_id)
    try:
        stream = await core_facade.regenerate(user_id, request, session_id, message_id)
    except CoreFacadeError as exc:
        await _release_run_slot(user_id, window_start)
        _raise_for_core_error(exc)
    except Exception:
        await _release_run_slot(user_id, window_start)
        raise

    return StreamingResponse(
        _guarded_run_stream(stream, user_id, window_start),
        media_type="text/event-stream",
        headers=_SSE_HEADERS,
    )


# ---------------------------------------------------------------------------
# Chat configs
# ---------------------------------------------------------------------------


@router.get("/configs")
async def get_chat_configs(
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    return await core_facade.get_chat_configs(current_user.user_id)


__all__ = ["router"]
