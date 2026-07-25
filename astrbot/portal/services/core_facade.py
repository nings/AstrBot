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

Three hard security invariants enforced everywhere below:

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
3. **No unverified attachment_id ever reaches ``ChatService`` (Phase 2).**
   ``build_webchat_message_parts`` resolves a media part's file purely by
   ``attachment_id`` via ``get_attachment_by_id``, and that core lookup has
   *no owner filter at all* (``Attachment`` has no owner column -- verified
   ground truth). :func:`send_message` and :func:`update_message` therefore
   whitelist-filter every message-part key down to
   ``{"type", "attachment_id", "filename", "text", "message_id",
   "selected_text"}`` (stripping any client-supplied ``path``/
   ``stored_filename``) and, when the sanitized parts contain at least one
   media ``attachment_id``, require an injected
   :data:`AttachmentOwnershipVerifier` callable (e.g.
   ``attachment_service.assert_owns_all``) to confirm ``user_id`` owns
   every one of them *before* the payload reaches ``ChatService`` -- if no
   verifier is supplied while a media part is present, both functions raise
   :class:`ValidationError` rather than silently forwarding an unverified
   id. This makes it structurally impossible to call either function with
   an unverified attachment_id by accident.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlmodel import col, select

from astrbot.core import logger, sp
from astrbot.dashboard.services.chat_service import ChatService, ChatServiceError
from astrbot.portal.config import get_portal_jwt_secret
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalAttachmentOwner

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

# Media part types recognized by the core webchat message-parts pipeline
# (astrbot.core.platform.sources.webchat.message_parts_helper.MEDIA_PART_TYPES).
# Duplicated here as a literal frozenset rather than imported: these four
# strings have been stable since Phase 1's ground truth was recorded, and
# importing a helper from that core submodule just to get one constant is
# not worth a new capability-wall surface.
_MEDIA_PART_TYPES = frozenset({"image", "record", "file", "video"})

# The only message-part keys a client is ever allowed to have survive into a
# payload this module hands to ChatService. In particular this excludes
# `path` (an internal, server-resolved absolute filesystem path -- see
# `strip_message_parts_path_fields`'s own module in message_parts_helper.py)
# and `stored_filename` (would let a client point straight at another
# user's on-disk file by name, given core's shared flat attachments
# directory -- see `PortalAttachmentOwner`'s docstring in db/models.py).
# `filename` is retained but is display-only: `build_webchat_message_parts`
# always re-derives the actual file to open from `attachment_id`, never
# from `filename`.
_ALLOWED_MESSAGE_PART_KEYS = frozenset(
    {"type", "attachment_id", "filename", "text", "message_id", "selected_text"}
)

AttachmentOwnershipVerifier = Callable[[str, list[str]], Awaitable[None]]
"""Callable contract for the injected attachment-ownership-verification hook
accepted by :func:`send_message`/:func:`update_message`.

Must raise a :class:`CoreFacadeError` (in practice
:class:`ResourceNotFoundError`) if any id in the given list is not owned by
the given ``user_id`` -- see ``attachment_service.assert_owns_all`` (batch,
one query) for the concrete implementation routes are expected to pass in.
Never invented/defaulted to a no-op inside this module: see invariant 3 in
the module docstring for why the absence of a verifier must fail closed
instead.
"""


def _sanitize_message_parts(parts: list) -> list[dict]:
    """Whitelist-filter every key of every part to
    ``_ALLOWED_MESSAGE_PART_KEYS``.

    Non-dict entries are dropped outright (mirrors
    ``build_webchat_message_parts``'s own tolerant behavior for non-strict
    callers) rather than raising -- shape/strict validation is core's job;
    this function's only responsibility is stripping fields a client must
    never control before anything downstream ever sees them.
    """
    sanitized: list[dict] = []
    for part in parts:
        if not isinstance(part, dict):
            continue
        sanitized.append(
            {
                key: value
                for key, value in part.items()
                if key in _ALLOWED_MESSAGE_PART_KEYS
            }
        )
    return sanitized


def _collect_media_attachment_ids(parts: list[dict]) -> list[str]:
    """Every distinct ``attachment_id`` referenced by a media-typed part,
    in first-seen order."""
    ids: list[str] = []
    seen: set[str] = set()
    for part in parts:
        # Normalize identically to core's build_webchat_message_parts
        # (str(...).strip()) before comparing against _MEDIA_PART_TYPES.
        # This match must never be stricter/narrower than core's own --
        # otherwise a type string core's .strip() recognizes as media
        # (e.g. " image", "image\n") would slip past this ownership gate
        # unverified while still being resolved by get_attachment_by_id,
        # which has no owner filter (see module docstring, invariant 3).
        part_type = str(part.get("type", "")).strip()
        if part_type not in _MEDIA_PART_TYPES:
            continue
        attachment_id = part.get("attachment_id")
        if not attachment_id:
            continue
        attachment_id = str(attachment_id)
        if attachment_id not in seen:
            seen.add(attachment_id)
            ids.append(attachment_id)
    return ids


async def _verify_media_parts_ownership(
    user_id: str,
    parts: list[dict],
    verify_attachment_ownership: AttachmentOwnershipVerifier | None,
) -> None:
    """Enforce invariant 3 (module docstring) for already-sanitized ``parts``.

    Fails closed: if the payload references at least one attachment_id and
    no verifier was supplied, this raises :class:`ValidationError` rather
    than silently letting the part through -- it must be impossible to
    reach ``ChatService`` with an unverified attachment_id, not merely
    discouraged.
    """
    attachment_ids = _collect_media_attachment_ids(parts)
    if not attachment_ids:
        return
    if verify_attachment_ownership is None:
        raise ValidationError(
            "This message references attachments but no ownership verifier "
            "was supplied; refusing to forward unverified attachment_ids"
        )
    await verify_attachment_ownership(user_id, attachment_ids)


@dataclass(slots=True)
class ChatConfigs:
    """Everything an untrusted end user may see about chat capabilities."""

    streaming: bool = True
    inline_genui: bool = True
    default_system_prompt: bool = True
    multimodal_upload: bool = True
    """Phase 2: uploads are accepted (see ``save_upload``/``resolve_attachment``
    below and ``attachment_service.py`` for the enforcing layer)."""
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
    verify_attachment_ownership: AttachmentOwnershipVerifier | None = None,
) -> AsyncIterator[str]:
    """Send a message in an owned session and return the SSE stream.

    The payload handed to ``ChatService.build_chat_stream`` is constructed
    entirely server-side: ``selected_provider``/``selected_model`` are never
    read from ``message``/``flags`` and are never forwarded, so the caller
    cannot steer provider/model routing (see module docstring, invariant 1).
    The server-resolved per-umo default provider is always used.

    When ``message`` is the list form (Phase 2 multimodal input), every part
    is whitelist-filtered to
    ``{"type", "attachment_id", "filename", "text", "message_id",
    "selected_text"}`` before anything else happens -- a client-supplied
    ``path`` or ``stored_filename`` can never survive into the payload
    handed to core. If, after sanitizing, at least one media part carries an
    ``attachment_id``, ``verify_attachment_ownership`` is called with
    ``(user_id, [attachment_id, ...])`` *before* ``ChatService.build_chat_
    stream`` is ever invoked (module docstring, invariant 3); omitting the
    verifier while a media part is present raises :class:`ValidationError`
    rather than silently forwarding an unverified id -- see
    :data:`AttachmentOwnershipVerifier` and ``attachment_service.
    assert_owns_all`` for the concrete callable routes should pass in.
    """
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)

    prepared_message: str | list = message
    if isinstance(message, list):
        prepared_message = _sanitize_message_parts(message)
        await _verify_media_parts_ownership(
            user_id, prepared_message, verify_attachment_ownership
        )

    post_data: dict[str, Any] = {
        "message": prepared_message,
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
    *,
    verify_attachment_ownership: AttachmentOwnershipVerifier | None = None,
) -> dict[str, Any]:
    """Edit the latest user message of an owned session (branches history;
    caller must follow up with a new ``send_message``/``regenerate``).

    Same attachment-ownership and key-whitelisting treatment as
    :func:`send_message` (module docstring, invariant 3) applies to
    ``content["message"]`` when it is a list: core's own
    ``ChatService.update_message`` -> ``sanitize_message_content`` only ever
    strips ``path``, it never re-verifies an ``attachment_id`` against any
    ownership table (verified ground truth) -- so without this, an edit
    payload would be a second, unaudited route to the exact same IDOR
    :func:`send_message` guards against.
    """
    chat_service = _chat_service(request)
    await _owned_session_row(chat_service, user_id, session_id)

    prepared_content = content
    message_parts = content.get("message")
    if isinstance(message_parts, list):
        sanitized_parts = _sanitize_message_parts(message_parts)
        await _verify_media_parts_ownership(
            user_id, sanitized_parts, verify_attachment_ownership
        )
        prepared_content = {**content, "message": sanitized_parts}

    try:
        return await chat_service.update_message(
            user_id,
            {
                "session_id": session_id,
                "message_id": message_id,
                "content": prepared_content,
            },
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

    Takes no message-parts input from the client at all -- only
    ``session_id``/``message_id`` -- so invariant 3 (attachment ownership,
    see module docstring) does not apply here: whatever media parts
    ``prepare_regenerate_message_payload`` reconstructs come from this same
    owned session's own prior history, not from anything in this request.
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
# Attachments (Phase 2: multimodal upload). Every core Attachment row has no
# owner column at all (verified ground truth); ownership lives entirely in
# ``astrbot.portal.db.models.PortalAttachmentOwner``, written by
# ``attachment_service.upload_file`` immediately after ``save_upload``
# returns. This module still touches that table directly in
# ``resolve_attachment`` below -- see that function's docstring for why.
# ---------------------------------------------------------------------------


class _RenamedUpload:
    """Wrap an upload-like object (must already expose an async
    ``save(destination)`` method -- e.g. ``attachment_service.
    _BufferedUpload``) to force the ``filename`` seen by ``ChatService.
    save_uploaded_file`` to a caller-supplied value.

    Exists solely so :func:`save_upload` can inject the *already-uniquified*
    stored filename (see that function's docstring) without needing to know
    anything about the concrete upload object's own type.
    """

    __slots__ = ("_inner", "filename", "content_type")

    def __init__(self, inner: Any, filename: str) -> None:
        self._inner = inner
        self.filename = filename
        self.content_type = getattr(inner, "content_type", None)

    async def save(self, destination: str) -> None:
        await self._inner.save(destination)


async def save_upload(
    user_id: str,
    request: Request,
    upload: Any,
    *,
    stored_filename: str,
) -> dict[str, Any]:
    """Persist an already-validated upload via ``ChatService.save_uploaded_file``.

    SECURITY (uniquification trust boundary): ``stored_filename`` MUST
    already be uniquified by the caller (``attachment_service.upload_file``,
    a uuid4-hex-prefixed stem) *before* this function is ever called. Core's
    own ``sanitize_upload_filename`` (``chat_service.py:41-48``) does NOT
    uniquify -- it only replaces empty/``.``/``..`` names with a fresh uuid
    and otherwise returns the client's basename unchanged -- and every
    upload lands in one shared flat directory
    (``ChatService.attachments_dir``). Two different users uploading a
    same-named file therefore collide on disk under an unmodified filename:
    the second write silently overwrites the first, and the first user's
    already-issued attachment then serves the second user's bytes -- a real
    cross-tenant content-confusion bug (see ``PortalAttachmentOwner``'s
    class docstring in ``db/models.py`` for the full writeup). This function
    does NOT perform that uniquification itself; it trusts its caller
    already did, and only wraps ``upload`` so ``ChatService.
    save_uploaded_file`` sees ``stored_filename`` as the upload's
    ``filename`` rather than whatever the original object's own ``filename``
    attribute happened to hold.

    No MIME/size/quota validation happens here either -- that is entirely
    ``attachment_service.upload_file``'s job and MUST run before this
    function is ever called (this function unconditionally writes whatever
    bytes ``upload.save()`` produces to disk).

    Returns plain data only, never the core ``Attachment`` ORM object:
    ``{"attachment_id", "stored_filename", "mime_type", "attach_type",
    "byte_size"}``. ``mime_type``/``attach_type`` reflect core's own
    post-write value (post-sniff for images; the client-declared
    ``content_type`` for every other attach_type, since core does not sniff
    those -- see the module's ground truth). ``byte_size`` is a fresh
    ``stat()`` of the file core actually wrote, never a client-reported
    content-length, so quota accounting never trusts a client-controlled
    number.
    """
    chat_service = _chat_service(request)
    shim = _RenamedUpload(upload, stored_filename)
    try:
        result = await chat_service.save_uploaded_file(shim)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc

    attachment = await chat_service.db.get_attachment_by_id(result["attachment_id"])
    if attachment is None:
        # Core just inserted this row a moment ago; a miss here means
        # core's own write path is broken, not an ownership issue.
        raise ValidationError("Upload did not persist correctly")

    try:
        byte_size = Path(attachment.path).stat().st_size
    except OSError as exc:
        raise ValidationError("Uploaded file is not accessible on disk") from exc

    logger.info(
        f"[portal] user {user_id} uploaded attachment {attachment.attachment_id} "
        f"({attachment.type}, {byte_size} bytes)"
    )
    return {
        "attachment_id": attachment.attachment_id,
        "stored_filename": Path(attachment.path).name,
        "mime_type": attachment.mime_type,
        "attach_type": attachment.type,
        "byte_size": byte_size,
    }


async def discard_orphaned_upload(request: Request, attachment_id: str) -> None:
    """Best-effort rollback: delete both the core ``Attachment`` row and its
    on-disk file for an upload whose core write succeeded but whose portal
    bookkeeping (the ``PortalAttachmentOwner`` insert / quota reservation)
    failed immediately afterward.

    NOT owner-checked and deliberately not exposed through any route: this
    exists purely for ``attachment_service.upload_file`` to call,
    synchronously, for an ``attachment_id`` it itself just minted via
    :func:`save_upload` earlier in the very same request -- there is no
    client-supplied input involved anywhere in this call. Never call this
    with an attachment_id sourced from a client request.

    Two independent SQLite database files (core's and the portal's) plus a
    filesystem write cannot be rolled back as one atomic transaction, so
    this is inherently best-effort: failures here are logged, never raised,
    so a cleanup failure can never mask the original error that triggered
    the rollback.
    """
    chat_service = _chat_service(request)
    try:
        attachment = await chat_service.db.get_attachment_by_id(attachment_id)
    except Exception:
        logger.exception(
            f"[portal] failed to look up orphaned upload {attachment_id} for cleanup"
        )
        return

    if attachment is not None:
        try:
            Path(attachment.path).unlink(missing_ok=True)
        except OSError:
            logger.warning(
                f"[portal] failed to unlink orphaned upload file for attachment "
                f"{attachment_id} at {attachment.path}"
            )
        try:
            await chat_service.db.delete_attachment(attachment_id)
        except Exception:
            logger.exception(
                f"[portal] failed to delete orphaned core Attachment row {attachment_id}"
            )


async def resolve_attachment(
    user_id: str, request: Request, attachment_id: str
) -> tuple[str, str]:
    """Resolve an attachment to ``(path, mime_type)`` for a file ``user_id`` owns.

    TRUST BOUNDARY: although ``attachment_service.assert_owns``/``download``
    are expected to already have checked ownership before calling this, this
    function performs its OWN independent ownership check against
    ``PortalAttachmentOwner`` here -- it never merely trusts that a caller
    already checked. This matters specifically here because this is the one
    facade function that hands back a real filesystem path, and core's own
    ``BaseDatabase.get_attachment_by_id`` (what core's own
    ``resolve_attachment_file`` is built on) has NO owner filter whatsoever
    (verified ground truth) -- so this function must never degrade into an
    unchecked passthrough no matter how it is later refactored or called
    from. ``user_id`` must still always be derived from the verified JWT by
    the caller, never from a client-supplied field, exactly like every
    other function in this module.

    Raises :class:`ResourceNotFoundError` (never a distinct "exists but not
    yours" error) when the attachment is missing, unowned, or its file is
    no longer on disk -- so a caller can never use this to enumerate other
    users' attachment ids.
    """
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAttachmentOwner).where(
                col(PortalAttachmentOwner.attachment_id) == attachment_id
            )
        )
        owner = result.scalar_one_or_none()
    if owner is None or owner.user_id != user_id:
        raise ResourceNotFoundError("Attachment not found")

    chat_service = _chat_service(request)
    attachment = await chat_service.db.get_attachment_by_id(attachment_id)
    if attachment is None:
        # Shadow row exists but the core row is gone (e.g. deleted out of
        # band) -- still 404, never leak the discrepancy to the caller.
        raise ResourceNotFoundError("Attachment not found")

    file_path = Path(attachment.path)
    if not file_path.exists():
        raise ResourceNotFoundError("Attachment not found")

    return str(file_path), attachment.mime_type


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
    "AttachmentOwnershipVerifier",
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
    "save_upload",
    "discard_orphaned_upload",
    "resolve_attachment",
    "get_chat_configs",
]
