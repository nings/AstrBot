"""The capability wall: the only file in ``astrbot/portal/**`` permitted to
import ``astrbot.dashboard.services.chat_service.ChatService``,
``astrbot.core.utils.shared_preferences`` (the process-wide ``sp`` instance),
``astrbot.core.conversation_mgr``, or ``astrbot.core.persona_mgr``.

Every function exported here takes the authenticated ``user_id`` (derived
from a verified portal JWT -- never from a client-supplied field) as its
first parameter and narrows every core call down to that user's own data
before it ever reaches :class:`ChatService`. No other file under
``astrbot/portal/**`` may import ``ChatService``,
``ConversationService``, ``SessionManagementService``, ``ApiKeyService``,
or ``SubAgentOrchestrator`` -- they must call through this module instead.

Two hard security invariants enforced everywhere below:

1. **No client-supplied provider/model routing.** ``astr_main_agent.py``'s
   ``_select_provider`` trusts ``selected_provider``/``selected_model`` with
   no authorization check whatsoever -- any caller-supplied value is a
   direct privilege-escalation / cost-abuse vector. Every payload handed to
   :meth:`ChatService.build_chat_stream` is built from scratch in this
   module; those two keys are never read from, or forwarded out of, a
   client request.
2. **Fail closed, never confirm existence.** Every operation scoped to a
   session/run that isn't owned by ``user_id`` raises
   :class:`ResourceNotFoundError` -- the same exception, with the same
   generic message, regardless of whether the resource is missing entirely
   or simply belongs to someone else. Callers (the route layer) should map
   this to HTTP 404, never 403. Ownership is checked here, directly against
   the row/state ChatService itself would check, *before* ChatService is
   ever invoked -- so this guarantee does not depend on parsing whatever
   error string a future ChatService revision happens to raise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from fastapi import Request

from astrbot.core import logger, sp
from astrbot.dashboard.services.chat_service import ChatService, ChatServiceError
from astrbot.portal.config import get_portal_jwt_secret

# ---------------------------------------------------------------------------
# Exceptions. Intentionally there is no "permission denied" variant: every
# ownership failure must surface as ResourceNotFoundError so callers cannot
# accidentally leak existence via a 403.
# ---------------------------------------------------------------------------


class CoreFacadeError(Exception):
    """Base class for all errors raised by this module."""


class ResourceNotFoundError(CoreFacadeError):
    """The requested session/run/message does not exist for this user.

    Raised uniformly whether the resource truly does not exist or exists but
    belongs to a different user -- callers must map this to HTTP 404, never
    403 (see rule 3 of the module's security checklist).
    """


class ValidationError(CoreFacadeError):
    """The request was rejected for a reason unrelated to ownership.

    e.g. empty message content, editing a non-latest message, malformed
    content shape. Safe to relay to the client as-is (HTTP 400-style); it
    never reveals anything about another user's data.
    """


# Only WebChat sessions are ever reachable through the portal module. This is
# enforced independently of (in addition to) ChatService's own creator
# check, so a session created by this same user_id under some other
# platform_id (which "shouldn't" be reachable anyway, since user_id is a
# random uuid unlikely to collide with any other platform's username) can
# never be touched through this facade.
_WEBCHAT_PLATFORM_ID = "webchat"

# The only WebChat request flags that are safe to accept verbatim from a
# client: they are pure UI toggles (inline generative-UI rendering, default
# system prompt, streaming on/off) and carry no authorization weight. Any
# other key is silently dropped.
_ALLOWED_WEBCHAT_FLAGS = frozenset(
    {"enable_inline_genui", "enable_default_system_prompt", "enable_streaming"}
)


@dataclass(slots=True)
class ChatConfigs:
    """Everything an untrusted end user may see about chat capabilities."""

    streaming: bool = True
    inline_genui: bool = True
    default_system_prompt: bool = True
    multimodal_upload: bool = False
    """Phase 2 has not shipped yet; uploads are not accepted."""
    knowledge_base: bool = False
    """Phase 3 has not shipped yet."""
    agent_bundles: bool = False
    """Phase 4 has not shipped yet; there is no user-facing provider/persona
    picker in Phase 1 -- the server always resolves the per-umo default."""

    def to_dict(self) -> dict[str, Any]:
        return {
            "streaming": self.streaming,
            "inline_genui": self.inline_genui,
            "default_system_prompt": self.default_system_prompt,
            "multimodal_upload": self.multimodal_upload,
            "knowledge_base": self.knowledge_base,
            "agent_bundles": self.agent_bundles,
        }


@dataclass(slots=True)
class BatchDeleteResult:
    deleted_count: int
    failed_count: int
    failed_session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "deleted_count": self.deleted_count,
            "failed_count": self.failed_count,
            "failed_session_ids": self.failed_session_ids,
        }


# ---------------------------------------------------------------------------
# Accessors. The portal router is mounted into the *same* FastAPI app the
# dashboard builds in ``create_dashboard_asgi_app`` (one line
# ``router.include_router(portal_router)`` in
# ``astrbot/dashboard/api/router.py`` -- see the module design doc), so
# ``request.app.state.services.chat`` is the exact same already-constructed
# ``ChatService`` instance every operator-facing route uses. We deliberately
# read it off ``request.app.state`` rather than constructing our own
# ``ChatService``, so there is exactly one instance (and therefore one
# ``chat_runs``/``chat_runs_by_session`` bookkeeping dict) per process.
# ---------------------------------------------------------------------------


def _chat_service(request: Request) -> ChatService:
    return request.app.state.services.chat


async def get_jwt_secret() -> str:
    """Return the portal module's own JWT signing secret.

    Lazily generated on first call and persisted via ``SharedPreferences``
    at ``scope='global', scope_id='portal', key='jwt_secret'`` -- see
    ``astrbot.portal.config.get_portal_jwt_secret`` for the full rationale.
    This is the only place in the module that touches ``sp`` directly.
    """
    return await get_portal_jwt_secret(sp.get_async, sp.put_async)


def _sanitize_flags(flags: dict[str, Any] | None) -> dict[str, bool]:
    if not isinstance(flags, dict):
        return {}
    return {
        key: value
        for key, value in flags.items()
        if key in _ALLOWED_WEBCHAT_FLAGS and isinstance(value, bool)
    }


def _translate_chat_service_error(exc: ChatServiceError) -> CoreFacadeError:
    """Best-effort secondary translation for errors not already prevented by
    our own pre-checks below (defense in depth; the primary ownership gate
    never depends on this)."""
    text = str(exc)
    lowered = text.lower()
    if "not found" in lowered or "permission denied" in lowered:
        return ResourceNotFoundError("Resource not found")
    return ValidationError(text)


async def _owned_session_row(chat_service: ChatService, user_id: str, session_id: str):
    """Fetch a WebChat session row and verify ``user_id`` owns it.

    This is the primary ownership gate: it is checked here, directly
    against the database row, *before* any ``ChatService`` method is
    invoked -- so callers of this module never depend on ``ChatService``'s
    own internal error message wording to stay secure.
    """
    session = await chat_service.db.get_platform_session_by_id(session_id)
    if (
        not session
        or session.creator != user_id
        or session.platform_id != _WEBCHAT_PLATFORM_ID
    ):
        raise ResourceNotFoundError("Session not found")
    return session


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def create_session(user_id: str, request: Request) -> dict[str, Any]:
    """Create a new WebChat session owned by ``user_id``.

    The platform is always ``webchat`` -- callers cannot select a different
    platform through this facade.
    """
    chat_service = _chat_service(request)
    try:
        return await chat_service.new_session(user_id, _WEBCHAT_PLATFORM_ID)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def list_sessions(user_id: str, request: Request) -> list[dict[str, Any]]:
    """Return only the WebChat sessions owned by ``user_id``.

    Never accepts a caller-supplied owner/platform filter: the platform is
    hardcoded to ``webchat`` and the creator filter is always ``user_id``.
    """
    chat_service = _chat_service(request)
    try:
        return await chat_service.get_sessions(user_id, _WEBCHAT_PLATFORM_ID)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def get_owned_session(
    user_id: str, request: Request, session_id: str
) -> dict[str, Any]:
    """Return session detail (history, threads, active runs) for a session
    owned by ``user_id``, or raise :class:`ResourceNotFoundError`."""
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        return await chat_service.get_session(user_id, session_id)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def rename_session(
    user_id: str, request: Request, session_id: str, display_name: str
) -> dict[str, Any]:
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        return await chat_service.update_session_display_name(
            user_id, session_id, display_name
        )
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def delete_session(user_id: str, request: Request, session_id: str) -> None:
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        await chat_service.delete_webchat_session(user_id, session_id)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def batch_delete_sessions(
    user_id: str, request: Request, session_ids: list[str]
) -> dict[str, Any]:
    """Delete every session in ``session_ids`` owned by ``user_id``.

    Unlike ``ChatService.batch_delete_sessions``, the per-item failure
    report here never distinguishes "not found" from "permission denied"
    from "internal error" -- every failure is reported as an opaque
    ``session_id`` in ``failed_session_ids``, so a caller cannot use this
    endpoint to enumerate which session ids exist for other users.
    """
    chat_service = _chat_service(request)
    sessions = await chat_service.db.get_platform_sessions_by_ids(session_ids)
    sessions_by_id = {session.session_id: session for session in sessions}

    deleted_count = 0
    failed_session_ids: list[str] = []
    for session_id in session_ids:
        session = sessions_by_id.get(session_id)
        if (
            not session
            or session.creator != user_id
            or session.platform_id != _WEBCHAT_PLATFORM_ID
        ):
            failed_session_ids.append(session_id)
            continue
        try:
            await chat_service.delete_session_internal(session, user_id)
            deleted_count += 1
        except Exception:
            logger.warning(f"[portal] failed to delete session {session_id}")
            failed_session_ids.append(session_id)

    return BatchDeleteResult(
        deleted_count=deleted_count,
        failed_count=len(failed_session_ids),
        failed_session_ids=failed_session_ids,
    ).to_dict()


# ---------------------------------------------------------------------------
# Messaging / streaming
# ---------------------------------------------------------------------------


async def send_message(
    user_id: str,
    request: Request,
    session_id: str,
    message: str | list,
    *,
    flags: dict[str, Any] | None = None,
) -> AsyncIterator[str]:
    """Send a message in an owned session and return the SSE stream.

    The payload handed to ``ChatService.build_chat_stream`` is constructed
    entirely server-side: ``selected_provider``/``selected_model`` are never
    read from ``message``/``flags`` and are never forwarded, so the caller
    cannot steer provider/model routing (see module docstring, invariant 1).
    The server-resolved per-umo default provider is always used.
    """
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)

    post_data: dict[str, Any] = {
        "message": message,
        "session_id": session_id,
        "flags": _sanitize_flags(flags),
        # selected_provider / selected_model deliberately absent.
    }
    try:
        return await chat_service.build_chat_stream(user_id, post_data)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def stream_run(user_id: str, request: Request, run_id: str) -> AsyncIterator[str]:
    """Attach to (or resume) the SSE stream of an in-flight or recently
    finished chat run owned by ``user_id``."""
    chat_service = _chat_service(request)
    run = chat_service.chat_runs.get(run_id)
    if run is None or run.username != user_id:
        raise ResourceNotFoundError("Run not found")
    try:
        return await chat_service.build_chat_run_stream(user_id, run_id)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def resume_run(user_id: str, request: Request, run_id: str) -> AsyncIterator[str]:
    """Alias of :func:`stream_run` for callers that model "resume" as a
    distinct operation from the initial subscribe; the underlying
    ``ChatService.build_chat_run_stream`` call is identical either way."""
    return await stream_run(user_id, request, run_id)


async def stop_run(user_id: str, request: Request, session_id: str) -> dict[str, Any]:
    """Stop all in-flight generations for an owned session."""
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        return await chat_service.stop_session(user_id, session_id)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def update_message(
    user_id: str,
    request: Request,
    session_id: str,
    message_id: int,
    content: dict[str, Any],
) -> dict[str, Any]:
    """Edit the latest user message of an owned session (branches history;
    caller must follow up with a new ``send_message``/``regenerate``)."""
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        return await chat_service.update_message(
            user_id,
            {"session_id": session_id, "message_id": message_id, "content": content},
        )
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


async def regenerate(
    user_id: str, request: Request, session_id: str, message_id: int
) -> AsyncIterator[str]:
    """Regenerate a bot message in an owned session and return the SSE
    stream for the new run.

    Same server-side payload construction guarantee as :func:`send_message`:
    ``selected_provider``/``selected_model`` are never sourced from the
    client and are explicitly stripped from the prepared payload before it
    is handed to ``build_chat_stream``, even though
    ``prepare_regenerate_message_payload`` would only ever echo back what we
    ourselves passed in (defense in depth against that method changing
    behavior in a future core upgrade).
    """
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)
    try:
        chat_payload = await chat_service.prepare_regenerate_message_payload(
            user_id, {"session_id": session_id, "message_id": message_id}
        )
        chat_payload.pop("selected_provider", None)
        chat_payload.pop("selected_model", None)
        return await chat_service.build_chat_stream(user_id, chat_payload)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc


# ---------------------------------------------------------------------------
# Chat configs: intentionally NOT a pass-through to
# ``ConfigProfileService.list_profiles()`` (the operator-facing
# ``GET /api/v1/chat/configs`` endpoint). That service is outside this
# module's permitted import list, and for good reason: ``list_profiles()``
# returns ``{"info_list": acm.get_conf_list()}`` -- the full set of raw
# AstrBotConfig profiles, including provider settings (which routinely embed
# API keys/secrets) and every other admin-only setting. None of that may
# ever reach an untrusted end user. What is returned below is a small,
# static, hand-picked capability descriptor with no provider inventory and
# no admin config of any kind.
# ---------------------------------------------------------------------------


async def get_chat_configs(user_id: str) -> dict[str, Any]:
    """Return the (very small) set of chat capability flags a portal end
    user may see. Deliberately does not expose any provider/model
    inventory or admin configuration -- see module comment above."""
    del user_id  # No per-user variation in Phase 1; kept for signature
    # symmetry with every other facade function and to leave room for a
    # future per-user override (e.g. once PortalAgentBundle ships in Phase 4)
    # without changing every call site.
    return ChatConfigs().to_dict()


__all__ = [
    "CoreFacadeError",
    "ResourceNotFoundError",
    "ValidationError",
    "get_jwt_secret",
    "create_session",
    "list_sessions",
    "get_owned_session",
    "rename_session",
    "delete_session",
    "batch_delete_sessions",
    "send_message",
    "stream_run",
    "resume_run",
    "stop_run",
    "update_message",
    "regenerate",
    "get_chat_configs",
]
