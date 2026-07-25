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
4. **No portal session is ever default-open to the operator's global KB
   corpora (Phase 3).** ``retrieve_knowledge_base``
   (``astrbot/core/tools/knowledge_base_tools.py:24-87``) treats a session
   umo with *no* ``kb_config`` key at all as "fall through to the
   operator's global ``kb_names`` config" -- only a ``kb_config`` that is
   explicitly present, with an ``kb_ids`` key, means anything at all to
   that function; an explicit empty ``kb_ids`` list is the only spelling of
   "this session uses no knowledge base". :func:`create_session` therefore
   always writes ``{"kb_ids": [], "top_k": ...}`` immediately after
   creating a session, and :func:`_owned_session_row` (the ownership gate
   every session-scoped function in this module funnels through, including
   :func:`send_message`/:func:`regenerate` -- the only two paths that can
   ever trigger an actual core chat turn for a portal session) backfills
   that same explicit empty binding for any session that predates this
   invariant. See :func:`_ensure_explicit_kb_config`'s own docstring for
   why that backfill fully closes the hole rather than merely narrowing it.
5. **Agent Bundle bindings never leave a session in a hybrid state
   (Phase 4).** ``PortalAgentBundle`` (``db/models.py``) is the only thing
   an end user ever names when "choosing an agent" -- never a persona id,
   provider id, or kb_id directly (rule 6). This module resolves what a
   bundle *means* into exactly two core-side binding surfaces --
   :func:`set_session_persona` (``conv_mgr.update_conversation(persona_id=
   ...)``) and :func:`set_session_provider` (the per-umo ``sp`` key
   ``provider_perf_chat_completion``) -- and both accept ``None`` to mean
   "this dimension has no bundle-forced value right now", resolving that
   to a fully-determined, deterministic state (the operator's own default
   persona; no per-umo provider preference at all) rather than silently
   leaving behind whatever a *previously* selected bundle wrote. This
   matters specifically when a session switches from one bundle to
   another: without this, a dimension the new bundle is silent on could
   keep showing the *old* bundle's value forever -- a state that was never
   any single operator-configured combination. See each function's own
   docstring for the core ground truth backing its specific reset
   behaviour (the two dimensions are NOT symmetric: persona requires an
   explicit resolved value because core's own ``update_conversation``
   treats ``persona_id=None`` as "leave unchanged", never "clear"; provider
   requires *removing* the per-umo key because ``get_using_provider``
   already treats an absent key as "fall through to the operator's
   default" by construction).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from fastapi import Request
from sqlmodel import col, select

from astrbot.core import logger, sp
from astrbot.core.platform.message_type import MessageType
from astrbot.dashboard.services.chat_service import ChatService, ChatServiceError
from astrbot.portal.config import get_portal_jwt_secret, get_portal_settings
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
    """Phase 3: mirrors ``PortalSettings.kb_selection_enabled`` (see
    :func:`get_chat_configs`) rather than being a static constant, so the
    operator's master switch actually turns the front-end affordance off."""
    agent_bundles: bool = False
    """Phase 4: mirrors ``PortalSettings.agent_bundle_selection_enabled``
    (see :func:`get_chat_configs`), exactly like ``knowledge_base`` mirrors
    ``kb_selection_enabled`` above -- the operator's master switch actually
    turns the front-end bundle-picker affordance off when disabled, rather
    than this being a static constant."""

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


_MISSING_KB_CONFIG = object()
"""Sentinel distinguishing "key entirely absent" from "key present but an
empty/falsy value" when reading ``kb_config`` back out of ``sp`` -- see
:func:`_ensure_explicit_kb_config`, which must only ever backfill on the
former, never re-write a value a caller genuinely set to something falsy."""


def _default_kb_config() -> dict[str, Any]:
    """The explicit "no knowledge base" binding written for every new
    session and backfilled onto every pre-Phase-3 one (module docstring,
    invariant 4)."""
    return {"kb_ids": [], "top_k": get_portal_settings().default_kb_top_k}


def _session_umo(session) -> str:
    """Build the unified message origin for a WebChat session's base
    (non-thread) conversation -- the exact umo ``retrieve_knowledge_base``
    resolves a session's ``kb_config`` against.

    Mirrors ``astrbot.dashboard.services.chat_service.
    build_webchat_unified_msg_origin`` exactly, but is duplicated locally
    (not imported) so this module's own contract on what a ``session``
    object must expose stays visible and minimal in one place, and so it
    tolerates a session-like object that does not define ``is_group`` at
    all (defaulting to ``0``, i.e. non-group -- ``PlatformSession.
    is_group``'s own default) rather than depending on that helper's exact
    attribute-access shape. Every real ``PlatformSession`` row always has
    ``is_group``; this tolerance exists purely for lightweight test
    doubles that intentionally expose only ``session_id``/``creator``/
    ``platform_id`` (this module's only real, load-bearing attribute
    contract on ``session`` before this function was added).
    """
    is_group = getattr(session, "is_group", 0)
    message_type = (
        MessageType.GROUP_MESSAGE.value if is_group else MessageType.FRIEND_MESSAGE.value
    )
    return (
        f"{session.platform_id}:{message_type}:"
        f"{session.platform_id}!{session.creator}!{session.session_id}"
    )


async def _ensure_explicit_kb_config(session) -> None:
    """Backfill the default-open KB hole for a session created before
    Phase 3 shipped this invariant.

    Verified ground truth (``astrbot/core/tools/knowledge_base_tools.py:
    24-87``): ``retrieve_knowledge_base`` only ever treats a session as
    "bound to no knowledge base" when its ``kb_config`` is present *and*
    carries an empty ``kb_ids`` list. A umo with no ``kb_config`` key at
    all instead falls through to the operator's **global** ``kb_names``
    config -- silently exposing whatever corpora the operator configured
    there to every end user of every session that predates this code.

    This function is called from :func:`_owned_session_row`, the single
    ownership gate every session-scoped function in this module funnels
    through -- critically including :func:`send_message` and
    :func:`regenerate`, the *only* two facade entry points that can ever
    cause an actual core chat turn (and therefore an actual
    ``retrieve_knowledge_base`` call) to run for a portal session. Because
    of that, this lazy backfill is not merely a stopgap: within the portal's
    own threat model, the ONLY way a message ever reaches
    ``retrieve_knowledge_base`` for a given session is via one of those two
    functions, and both always call this first. So by the time any turn
    could possibly read a stale/missing ``kb_config``, this has already
    replaced it with an explicit empty one. There is no code path left by
    which an untouched pre-Phase-3 session could leak the operator's global
    KBs to an end user through the portal.

    A *startup sweep* over every existing ``webchat`` ``PlatformSession``
    row was considered instead (or in addition) and deliberately not
    implemented here: ``BaseDatabase`` exposes no method to enumerate
    platform sessions across every creator (only
    ``get_platform_sessions_by_creator_paginated``, scoped to one user, and
    ``get_platform_sessions_by_ids``, scoped to a known id list) -- adding
    one would mean changing a core database module, which is out of this
    phase's charter. Given the choke-point argument above, it would also be
    redundant: a session nobody ever touches again through the portal can
    never trigger a chat turn through the portal either, so there is
    nothing left for a sweep to protect against that this backfill does
    not already cover the moment it matters.

    Idempotent and cheap once backfilled (one extra ``sp`` read, no write)
    -- which is every session from this point forward, since
    :func:`create_session` now writes this same explicit binding up front.
    """
    umo = _session_umo(session)
    existing = await sp.session_get(umo, "kb_config", default=_MISSING_KB_CONFIG)
    if existing is _MISSING_KB_CONFIG:
        await sp.session_put(umo, "kb_config", _default_kb_config())
        logger.info(
            f"[portal] backfilled explicit empty kb_config for pre-existing "
            f"session {session.session_id} (closing default-open KB fallback)"
        )


async def _owned_session_row(chat_service: ChatService, user_id: str, session_id: str):
    """Fetch a WebChat session row and verify ``user_id`` owns it.

    This is the primary ownership gate: it is checked here, directly
    against the database row, *before* any ``ChatService`` method is
    invoked -- so callers of this module never depend on ``ChatService``'s
    own internal error message wording to stay secure.

    Also the single choke point that backfills a missing ``kb_config``
    (:func:`_ensure_explicit_kb_config`) for every session-scoped operation
    in this module -- see that function's docstring and invariant 4 in the
    module docstring for why funneling the backfill through here, rather
    than through some separate startup job, is sufficient to close the
    default-open KB hole.
    """
    session = await chat_service.db.get_platform_session_by_id(session_id)
    if (
        not session
        or session.creator != user_id
        or session.platform_id != _WEBCHAT_PLATFORM_ID
    ):
        raise ResourceNotFoundError("Session not found")
    await _ensure_explicit_kb_config(session)
    return session


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------


async def create_session(user_id: str, request: Request) -> dict[str, Any]:
    """Create a new WebChat session owned by ``user_id``.

    The platform is always ``webchat`` -- callers cannot select a different
    platform through this facade.

    Immediately writes an explicit ``{"kb_ids": [], "top_k": ...}``
    ``kb_config`` for the new session's umo (module docstring, invariant
    4) -- a brand-new session must never rely on the
    :func:`_ensure_explicit_kb_config` backfill (that exists for sessions
    that predate this invariant); it should simply never have a moment
    where its ``kb_config`` is absent at all.
    """
    chat_service = _chat_service(request)
    try:
        result = await chat_service.new_session(user_id, _WEBCHAT_PLATFORM_ID)
    except ChatServiceError as exc:
        raise _translate_chat_service_error(exc) from exc

    session = await chat_service.db.get_platform_session_by_id(result["session_id"])
    if session is None:
        # Core's own insert just returned successfully a moment ago; a miss
        # here would mean core's own write path is broken, not something
        # this module can meaningfully recover from.
        logger.error(
            f"[portal] session {result['session_id']} vanished immediately "
            "after creation; kb_config was not written"
        )
        return result

    umo = _session_umo(session)
    await sp.session_put(umo, "kb_config", _default_kb_config())
    return result


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
    del user_id  # No per-user variation yet; kept for signature symmetry
    # with every other facade function and to leave room for a future
    # per-user override without changing every call site.
    settings = get_portal_settings()
    return ChatConfigs(
        knowledge_base=settings.kb_selection_enabled,
        agent_bundles=settings.agent_bundle_selection_enabled,
    ).to_dict()


# ---------------------------------------------------------------------------
# Knowledge-base session binding (Phase 3). See module docstring invariant 4
# and ``kb_visibility_service.py`` (the portal-side authorization whitelist
# gating which ``kb_id``s an end user may even choose from) for the full
# picture. Everything below is deliberately "dumb": it verifies ownership
# and moves data in/out of ``sp``/``kb_mgr`` with no whitelist/quota logic of
# its own -- ``kb_visibility_service.bind_session`` is where every count/
# top_k/visibility/existence check happens *before* it ever calls
# :func:`set_session_kb_config`.
# ---------------------------------------------------------------------------


async def set_session_kb_config(
    user_id: str,
    request: Request,
    session_id: str,
    kb_ids: Sequence[str],
    top_k: int,
) -> dict[str, Any]:
    """Overwrite an owned session's ``kb_config`` with ``kb_ids``/``top_k``.

    Verifies ``user_id`` owns ``session_id`` (raising
    :class:`ResourceNotFoundError` otherwise) and derives the umo to write
    server-side from that same owned session row -- never from any
    client-supplied umo/session field -- via :func:`_session_umo`.

    Deliberately performs NO whitelist/count/range validation of ``kb_ids``/
    ``top_k`` -- mirrors :func:`save_upload`'s "no MIME/size validation
    happens here either" contract. ``kb_visibility_service.bind_session``
    MUST run ``assert_all_visible`` (portal whitelist) and a live core
    existence check, and clamp ``top_k`` to
    ``PortalSettings.max_kb_top_k``/count to
    ``PortalSettings.max_kb_ids_per_session``, *before* ever calling this
    function. Calling this directly with unvalidated input would silently
    reintroduce the exact default-open-adjacent risk this phase exists to
    close (an unauthorized ``kb_id`` bound straight into a session's live
    retrieval config).
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)
    kb_config = {
        "kb_ids": [str(kb_id) for kb_id in kb_ids],
        "top_k": int(top_k),
    }
    await sp.session_put(umo, "kb_config", kb_config)
    logger.info(
        f"[portal] user {user_id} set kb_config for session {session_id}: "
        f"{len(kb_config['kb_ids'])} kb(s), top_k={kb_config['top_k']}"
    )
    return kb_config


async def get_session_kb_config(
    user_id: str, request: Request, session_id: str
) -> dict[str, Any]:
    """Read back an owned session's ``kb_config`` as plain data.

    Always returns a well-shaped ``{"kb_ids": [...], "top_k": ...}`` dict --
    never ``None`` and never a malformed/partial value -- since
    ``_owned_session_row`` (called here for the ownership check) already
    backfills a missing key via :func:`_ensure_explicit_kb_config`. The
    extra defensive re-shaping below guards only against a value that is
    present but not the shape this module itself ever writes (e.g. hand-
    edited via the operator's own ``SessionManagementService`` path, which
    shares the same ``kb_config`` key -- see ``AVAILABLE_SESSION_RULE_KEYS``
    ground truth).
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)
    config = await sp.session_get(umo, "kb_config", default=None)
    if not isinstance(config, dict):
        return _default_kb_config()
    kb_ids = config.get("kb_ids")
    if not isinstance(kb_ids, list):
        kb_ids = []
    top_k = config.get("top_k", get_portal_settings().default_kb_top_k)
    if not isinstance(top_k, int) or isinstance(top_k, bool):
        top_k = get_portal_settings().default_kb_top_k
    return {"kb_ids": [str(kb_id) for kb_id in kb_ids], "top_k": top_k}


# ---------------------------------------------------------------------------
# Live core KB resolution. Knowledge bases are global operator resources
# with no owner column (verified ground truth,
# astrbot/core/knowledge_base/models.py:24,31) -- there is no per-user
# ownership check to perform here, only a plain-data snapshot of whatever
# currently exists in core's in-memory ``KnowledgeBaseManager.kb_insts``.
# ``kb_visibility_service.py`` is the ONLY expected caller of these two
# functions: it is not permitted to import ``kb_mgr``/``core_lifecycle``
# itself (only this file may reach core), so every "does this kb_id still
# exist, and what does it currently look like" question it has must come
# through here.
# ---------------------------------------------------------------------------


def _kb_manager(request: Request):
    return request.app.state.core_lifecycle.kb_manager


def _kb_helper_to_dict(kb_helper) -> dict[str, Any]:
    kb = kb_helper.kb
    return {
        "kb_id": kb.kb_id,
        "kb_name": kb.kb_name,
        "description": kb.description,
        "emoji": kb.emoji,
        "doc_count": kb.doc_count,
        "chunk_count": kb.chunk_count,
        "available": kb_helper.init_error is None,
    }


async def get_kb_live_info(request: Request, kb_id: str) -> dict[str, Any] | None:
    """A plain-data snapshot of one core KB, or ``None`` if ``kb_id`` does
    not currently exist in core.

    ``None`` here is exactly the "dangling" condition
    ``kb_visibility_service`` must treat as "not selectable, not existing"
    regardless of what this ``kb_id`` used to be -- see
    ``PortalKbVisibility.kb_name``'s own docstring (db/models.py) for why a
    whitelist row can outlive the KB it was granted for.
    """
    kb_mgr = _kb_manager(request)
    kb_helper = await kb_mgr.get_kb(kb_id)
    if kb_helper is None:
        return None
    return _kb_helper_to_dict(kb_helper)


async def get_kb_live_info_batch(
    request: Request, kb_ids: Sequence[str]
) -> dict[str, dict[str, Any] | None]:
    """Batch form of :func:`get_kb_live_info`: one dict entry per (unique)
    id in ``kb_ids``, ``None`` for any that no longer exist in core.

    Not a database query at all -- ``KnowledgeBaseManager.get_kb`` is a
    plain in-memory dict lookup against ``kb_insts`` -- so "one query, not
    N" is satisfied by construction rather than needing a dedicated batch
    core API; this just avoids callers re-implementing the same loop.
    """
    kb_mgr = _kb_manager(request)
    result: dict[str, dict[str, Any] | None] = {}
    for kb_id in kb_ids:
        kb_id = str(kb_id)
        if kb_id in result:
            continue
        kb_helper = await kb_mgr.get_kb(kb_id)
        result[kb_id] = None if kb_helper is None else _kb_helper_to_dict(kb_helper)
    return result


async def list_all_core_kbs(request: Request) -> list[dict[str, Any]]:
    """Every knowledge base currently loaded in core, as plain data.

    NOT exposed to any end-user-facing route -- this is the operator-only
    "browse core's inventory to decide what to whitelist next" primitive
    for the admin KB-visibility page, mirroring the existing (and already
    operator-gated) ``astrbot/dashboard/api/knowledge_bases.py`` listing,
    just funneled through the capability wall so
    ``kb_visibility_service.py`` never needs its own ``kb_mgr`` import.
    Callers (admin routes) MUST gate this behind ``require_system_scope``
    themselves -- this function performs no authorization of its own, same
    as every other core-inventory read in this module (see
    :func:`get_chat_configs`'s neighboring comment on why the *portal*
    dashboard's own ``ConfigProfileService`` listing is never reused here
    for the equivalent end-user-facing case).
    """
    kb_mgr = _kb_manager(request)
    kbs = await kb_mgr.list_kbs()
    return [
        {
            "kb_id": kb.kb_id,
            "kb_name": kb.kb_name,
            "description": kb.description,
            "emoji": kb.emoji,
            "doc_count": kb.doc_count,
            "chunk_count": kb.chunk_count,
        }
        for kb in kbs
    ]


async def write_default_kb_config_for_umo(umo: str) -> None:
    """Write the explicit "no knowledge base" binding for a raw ``umo``.

    Exists for the session-rule guard (``astrbot/portal/session_rule_guard.py``),
    which is handed a umo string by core's session-rule route rather than an
    owned session object, and which cannot reach ``sp`` itself -- only this
    module may (capability wall, module docstring).

    Deliberately writes an explicit empty ``kb_ids`` instead of removing the
    key: ``retrieve_knowledge_base`` reads a *missing* ``kb_config`` as "use the
    operator's global ``kb_names``", so removal would re-open the default-open
    fallback rather than close it.
    """
    await sp.session_put(umo, "kb_config", _default_kb_config())


async def reset_session_persona_override(umo: str) -> None:
    """Remove a forced ``persona_id`` from ``session_service_config`` for a
    raw ``umo``, leaving every other sub-key untouched.

    Exists for the session-rule guard (``astrbot/portal/session_rule_guard.py``),
    which is handed a umo string by core's session-rule delete route rather
    than an owned session object, and which cannot reach ``sp`` itself --
    only this module may (capability wall, module docstring).

    ``session_service_config`` is a shared container: besides ``persona_id``
    it also carries ``llm_enabled``/``tts_enabled``
    (``astrbot/core/star/session_llm_manager.py``). A blanket replace or
    removal of the whole key would silently reset those too, so this merges:
    it pops only the ``persona_id`` sub-key and writes the dict back,
    letting persona resolution fall back to the conversation's own persona /
    the Agent Bundle's ``conv_mgr`` write (``PersonaManager.
    resolve_selected_persona``, ``astrbot/core/persona_mgr.py:75-127``).

    No-ops (no write at all) when there is nothing to remove -- e.g. right
    after a blanket ``sp`` clear already wiped the umo, or when no override
    was ever set -- so a reset never manufactures an empty dict where none
    existed.
    """
    existing = await sp.session_get(umo, "session_service_config", default=None)
    if not isinstance(existing, dict) or "persona_id" not in existing:
        return
    existing = dict(existing)
    existing.pop("persona_id", None)
    await sp.session_put(umo, "session_service_config", existing)


async def clear_chat_provider_preference_for_umo(umo: str) -> None:
    """Remove the ``provider_perf_chat_completion`` per-umo preference for a
    raw ``umo``.

    Exists for the session-rule guard, same reasoning as
    :func:`reset_session_persona_override`. Plain removal, not a placeholder
    write -- absence is the safe state for this key:
    ``ProviderManager.get_using_provider`` (``astrbot/core/provider/
    manager.py:215-245``) already treats a missing/unresolvable per-umo key
    as "fall through to the operator's ``default_provider_id``", so this
    mirrors exactly what a non-guarded ``session_remove`` would have done.
    """
    await sp.session_remove(umo, _PROVIDER_PERF_CHAT_COMPLETION_KEY)


# ---------------------------------------------------------------------------
# Agent Bundle bindings (Phase 4). See module docstring invariant 5 and
# ``PortalAgentBundle``'s own docstring (db/models.py) for the full picture.
# Everything below is deliberately "dumb" in exactly the same sense
# ``set_session_kb_config`` is: no whitelist/existence validation of its own
# -- ``agent_bundle_service.py`` MUST resolve and validate a bundle (enabled,
# every referenced persona/provider/kb_id still exists, kb_ids still
# ``PortalKbVisibility``-whitelisted) *before* ever calling any of these
# write functions, and MUST apply all of them for one bundle-selection
# before considering the operation done (all-or-nothing -- see that
# module's own docstring).
#
# Persona and provider are NOT symmetric (module docstring invariant 5
# explains why); read each function's own docstring rather than assuming
# they mirror each other.
# ---------------------------------------------------------------------------


def _persona_manager(request: Request):
    """The process-wide ``PersonaManager`` instance -- same duck-typed
    access pattern as :func:`_kb_manager`, no separate import of
    ``PersonaManager`` needed (the capability wall's "may import
    ``astrbot.core.persona_mgr``" allowance is a ceiling, not a
    requirement; this module already satisfies every Phase 4 need through
    plain attribute access off the same already-constructed
    ``core_lifecycle``, exactly like ``_kb_manager``/``_chat_service``)."""
    return request.app.state.core_lifecycle.persona_mgr


def _provider_manager(request: Request):
    """The process-wide ``ProviderManager`` instance. See :func:`_persona_manager`."""
    return request.app.state.core_lifecycle.provider_manager


async def _ensure_conversation_id(chat_service: ChatService, umo: str) -> str:
    """Return an existing (verified-to-exist) conversation id for ``umo``,
    creating one if necessary.

    Mirrors ``astr_main_agent._get_session_conv``'s own get-or-create dance
    (``astrbot/core/astr_main_agent.py:255-269``) exactly: a brand-new
    portal session has no conversation at all until its first message
    (``create_session`` never creates one -- verified ground truth,
    ``ChatService.new_session``), and ``ConversationManager.
    session_conversations``'s in-memory cache can point at a
    ``conversation_id`` for a conversation that was since deleted
    out-of-band. Without re-verifying existence, a persona write here could
    silently target a conversation row that is no longer there --
    ``ConversationManager.update_conversation`` performs no existence check
    of its own before issuing its ``UPDATE ... WHERE conversation_id = cid``.
    """
    conv_mgr = chat_service.conv_mgr
    conversation_id = await conv_mgr.get_curr_conversation_id(umo)
    if conversation_id and await conv_mgr.get_conversation(umo, conversation_id):
        return conversation_id
    return await conv_mgr.new_conversation(umo, _WEBCHAT_PLATFORM_ID)


async def persona_exists(request: Request, persona_id: str) -> bool:
    """Whether ``persona_id`` currently resolves to a real persona in core.

    Uses ``PersonaManager.get_persona_v3_by_id`` -- the exact same lookup
    ``resolve_selected_persona``/``astr_main_agent`` themselves use to
    decide whether a persona id is "real", including the special-cased
    always-exists ``"default"`` id. Returns a plain ``bool``, never the
    live ``Personality`` object -- ``agent_bundle_service.py`` (the only
    permitted caller) may not reach ``persona_mgr`` itself (capability
    wall) and must never receive anything beyond "does this exist right
    now" from this module.
    """
    if not persona_id:
        return False
    return _persona_manager(request).get_persona_v3_by_id(persona_id) is not None


async def get_persona_existence_batch(
    request: Request, persona_ids: Sequence[str]
) -> dict[str, bool]:
    """Batch form of :func:`persona_exists`: one entry per unique id in
    ``persona_ids``. Not a database query at all -- ``personas_v3`` is an
    in-memory list -- so this just avoids callers (the admin bundle-list
    dangling-status view) re-implementing the same loop themselves.
    """
    persona_mgr = _persona_manager(request)
    result: dict[str, bool] = {}
    for persona_id in persona_ids:
        persona_id = str(persona_id)
        if persona_id in result:
            continue
        result[persona_id] = persona_mgr.get_persona_v3_by_id(persona_id) is not None
    return result


async def provider_exists(request: Request, provider_id: str) -> bool:
    """Whether ``provider_id`` currently resolves to a live provider
    instance in core (``ProviderManager.get_provider_by_id`` -- the exact
    lookup ``get_using_provider`` itself falls back to for a per-umo
    preference). Plain ``bool``, never the live provider instance -- a
    provider object routinely embeds API keys/secrets and must never reach
    ``agent_bundle_service.py``.
    """
    if not provider_id:
        return False
    provider = await _provider_manager(request).get_provider_by_id(provider_id)
    return provider is not None


async def get_provider_existence_batch(
    request: Request, provider_ids: Sequence[str]
) -> dict[str, bool]:
    """Batch form of :func:`provider_exists`: one entry per unique id in
    ``provider_ids``."""
    provider_mgr = _provider_manager(request)
    result: dict[str, bool] = {}
    for provider_id in provider_ids:
        provider_id = str(provider_id)
        if provider_id in result:
            continue
        provider = await provider_mgr.get_provider_by_id(provider_id)
        result[provider_id] = provider is not None
    return result


async def set_session_persona(
    user_id: str, request: Request, session_id: str, persona_id: str | None
) -> dict[str, Any]:
    """Force -- or reset -- an owned session's effective persona.

    BINDING SURFACE (load-bearing choice, see module docstring invariant 5
    and ``PortalAgentBundle``'s own docstring): writes through
    ``conv_mgr.update_conversation(persona_id=...)`` -- the conversation's
    own persona field -- never ``session_service_config['persona_id']``.
    Ground truth (``PersonaManager.resolve_selected_persona``,
    ``astrbot/core/persona_mgr.py:75-127``): that function reads
    ``session_service_config['persona_id']`` FIRST as a *force-applied*
    override and only falls back to the conversation's own persona when
    that key is absent. The conversation-level surface is what this
    phase's plan explicitly specifies for ``agent_routes.py``, and picking
    it also means this facade never has to read-modify-write a dict shared
    with unrelated concerns (``llm_enabled``/``tts_enabled`` also live
    under ``session_service_config``, per ``session_llm_manager.py`` --
    exactly the trap ``set_session_kb_config``'s neighbours warn about for
    a *different* shared key). The known, currently-open residual gap this
    choice leaves (a data-scope caller can still force
    ``session_service_config['persona_id']`` through core's own
    ``POST /api/v1/sessions/rules`` route and shadow this binding) is
    ``session_rule_guard.py``'s to close, not this function's -- see
    ``PortalAgentBundle``'s docstring for the exact fix shape recorded for
    whoever extends that guard.

    ``persona_id=None`` means "this session has no bundle-forced persona
    right now" and is used for BOTH "the bundle being applied doesn't set
    one" and "the bundle was fully unbound" -- deliberately the same call
    either way (mirrors :func:`set_session_provider`'s identical shape).
    Rather than leaving whatever a *previously* selected bundle wrote in
    place (a hybrid state that is neither this call's outcome nor the
    operator's default -- exactly what module docstring invariant 5
    forbids), this resolves and writes the operator's OWN currently
    configured default persona (``PersonaManager.get_default_persona_v3``)
    so "no bundle persona" deterministically means the same thing on every
    call, regardless of what was bound before.

    This is a snapshot, not a live pointer: core's own
    ``ConversationManager.update_conversation`` -> ``BaseDatabase.
    update_conversation`` treats ``persona_id=None`` as "leave the column
    unchanged", never "clear it to ``NULL``" (verified ground truth,
    ``astrbot/core/db/sqlite.py:396-397`` -- the ``values`` dict is only
    populated ``if persona_id is not None``). There is therefore no
    core-provided way to write a real, dynamically-re-resolved "no
    persona_id at all" back onto a conversation row once one has been set;
    the closest correct substitute is writing the operator's default
    persona's *current* name explicitly. If the operator changes their
    default persona later, an already-unbound session does not
    retroactively follow that change -- a known, documented limitation of
    this reset, not an oversight (see ``contracts_for_downstream``).

    Always ensures the target conversation actually exists first (see
    :func:`_ensure_conversation_id`), since a session created but never
    yet messaged in has none.
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)
    conversation_id = await _ensure_conversation_id(chat_service, umo)

    resolved_persona_id = persona_id
    if resolved_persona_id is None:
        default_persona = await _persona_manager(request).get_default_persona_v3(umo=umo)
        resolved_persona_id = default_persona["name"] if default_persona else "default"

    await chat_service.conv_mgr.update_conversation(
        unified_msg_origin=umo,
        conversation_id=conversation_id,
        persona_id=resolved_persona_id,
    )
    logger.info(
        f"[portal] user {user_id} set persona for session {session_id}: "
        f"{resolved_persona_id!r} (bundle-forced={persona_id is not None})"
    )
    return {"persona_id": resolved_persona_id}


_PROVIDER_PERF_CHAT_COMPLETION_KEY = "provider_perf_chat_completion"
"""Exact key name verified against ``ProviderType.CHAT_COMPLETION.value``
(``astrbot/core/provider/manager.py``) -- ``ProviderManager.
get_using_provider`` reads ``f"provider_perf_{provider_type.value}"``, so
this literal must stay byte-for-byte in sync with that enum value. Kept as
a plain literal rather than importing ``ProviderType`` (an unnecessary
capability-wall-adjacent import for one already-verified constant string --
same reasoning as ``_MEDIA_PART_TYPES`` above)."""


async def set_session_provider(
    user_id: str, request: Request, session_id: str, chat_provider_id: str | None
) -> dict[str, Any]:
    """Write -- or remove -- an owned session's per-umo chat-completion
    provider preference.

    BINDING SURFACE: the ``sp`` key ``provider_perf_chat_completion``
    (``scope='umo', scope_id=umo``) -- verified ground truth,
    ``ProviderManager.get_using_provider``
    (``astrbot/core/provider/manager.py:215-245``): a umo's value under
    this exact key is read FIRST, before the operator's own
    ``default_provider_id``. Writing it here is sufficient to steer which
    provider a portal session's chat turns use, entirely server-side --
    the client never supplies a provider id anywhere in this call chain
    (module docstring, invariant 1); ``chat_provider_id`` must already have
    been resolved from an operator-curated ``PortalAgentBundle`` row by
    ``agent_bundle_service.py`` before this is ever called. This function
    performs no existence validation of its own (see :func:`provider_exists`
    for that check, which callers MUST run first).

    ``chat_provider_id=None`` REMOVES the key entirely rather than writing
    any sentinel -- unlike persona (:func:`set_session_persona`),
    ``get_using_provider`` already treats an absent/unresolvable per-umo
    key as "fall through to the operator's own ``default_provider_id``" by
    construction (ground truth: ``if provider_id: provider = self.inst_map.
    get(provider_id)`` then ``if not provider: ... config[...].get(
    "default_provider_id")``). "Absent is safe" for this dimension by
    construction, so there is no reset action needed beyond simply never
    writing (or actively clearing) a value -- do NOT copy
    :func:`set_session_persona`'s "resolve and write an explicit default"
    shape here; that would be solving a problem this dimension does not
    have. This also means calling this with ``None`` is the correct,
    identical action for BOTH "this bundle does not set a provider
    preference" and "the bundle was fully unbound".
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)

    if chat_provider_id:
        chat_provider_id = str(chat_provider_id)
        await sp.session_put(umo, _PROVIDER_PERF_CHAT_COMPLETION_KEY, chat_provider_id)
        logger.info(
            f"[portal] user {user_id} set chat provider preference for session "
            f"{session_id}: {chat_provider_id}"
        )
        return {"chat_provider_id": chat_provider_id}

    await sp.session_remove(umo, _PROVIDER_PERF_CHAT_COMPLETION_KEY)
    logger.info(
        f"[portal] user {user_id} cleared chat provider preference for session "
        f"{session_id}"
    )
    return {"chat_provider_id": None}


_AGENT_BUNDLE_SESSION_KEY = "portal_agent_bundle_id"
"""A portal-private ``sp`` key -- deliberately NOT one of core's own
recognized session-rule keys (``AVAILABLE_SESSION_RULE_KEYS``,
``astrbot/dashboard/services/session_management_service.py:16-24``), so
core's own ``POST /api/v1/sessions/rules`` route (which only ever accepts a
fixed, closed whitelist of key names) cannot read, write, or clear it at
all. This module's own bookkeeping of "which bundle is currently selected"
therefore needs no ``session_rule_guard.py`` registration to stay safe from
the exact bypass class that route enables for the keys it does recognize --
unlike ``session_service_config``/``kb_config``/``provider_perf_chat_completion``,
nothing outside ``agent_bundle_service.py`` (via this module) ever reads or
writes this key, so there is no other surface to shadow it through."""


async def set_session_bundle_id(
    user_id: str, request: Request, session_id: str, bundle_id: str
) -> None:
    """Record which :class:`PortalAgentBundle` an owned session currently
    has selected.

    Bookkeeping only -- never itself applies persona/provider/KB state; see
    :func:`set_session_persona`/:func:`set_session_provider`/
    ``set_session_kb_config`` for the actual binding writes and
    ``agent_bundle_service.bind_session`` for the all-or-nothing
    orchestration across all of them. Called last in that orchestration
    (after every other write has already succeeded) so a session is never
    observed with this bookkeeping value pointing at a bundle whose
    persona/provider/KB state was not, in fact, fully applied.
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)
    await sp.session_put(umo, _AGENT_BUNDLE_SESSION_KEY, str(bundle_id))


async def clear_session_bundle_id(user_id: str, request: Request, session_id: str) -> None:
    """The "no bundle selected" bookkeeping state.

    Removes the key entirely rather than writing e.g. an empty string --
    mirrors :func:`set_session_provider`'s "absence is the single safe
    value" shape for this dimension. There is no shadowing risk to defend
    against here (see :data:`_AGENT_BUNDLE_SESSION_KEY`'s own docstring),
    so unlike persona there is nothing to resolve; removal is simply
    correct on its own.
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)
    await sp.session_remove(umo, _AGENT_BUNDLE_SESSION_KEY)


async def get_session_agent_bindings(
    user_id: str, request: Request, session_id: str
) -> dict[str, Any]:
    """Read back an owned session's current agent-bundle-related bindings
    as plain data: ``{"bundle_id", "persona_id", "chat_provider_id"}``.

    ``persona_id``/``chat_provider_id`` reflect exactly what THIS module's
    own binding surfaces (:func:`set_session_persona`/
    :func:`set_session_provider`) currently hold -- the conversation's own
    ``persona_id`` column and the ``provider_perf_chat_completion`` ``sp``
    key, respectively -- NOT necessarily the value that would actually
    resolve at chat time if some other caller had separately forced
    ``session_service_config['persona_id']`` through core's own
    session-rule route (the documented, currently-open shadowing gap --
    see ``PortalAgentBundle``'s docstring and module docstring invariant
    5). This function answers "what did the portal's own bundle feature
    last write", which is the only thing this module has any authority
    over; it does not call ``resolve_selected_persona``/
    ``get_using_provider`` itself.

    ``persona_id`` is ``None`` when the session has no conversation yet
    (nothing has ever been written -- a brand-new, never-messaged
    session). ``chat_provider_id`` is ``None`` when no per-umo preference
    is currently set, whether because none was ever bound or because it
    was explicitly cleared -- the two are indistinguishable by design,
    since "absent" is the single safe value for that dimension (see
    :func:`set_session_provider`). ``bundle_id`` is ``None`` when no
    bundle is currently selected for this session.
    """
    chat_service = _chat_service(request)
    session = await _owned_session_row(chat_service, user_id, session_id)
    umo = _session_umo(session)

    conv_mgr = chat_service.conv_mgr
    persona_id: str | None = None
    conversation_id = await conv_mgr.get_curr_conversation_id(umo)
    if conversation_id:
        conversation = await conv_mgr.get_conversation(umo, conversation_id)
        if conversation:
            persona_id = conversation.persona_id

    chat_provider_id = await sp.session_get(
        umo, _PROVIDER_PERF_CHAT_COMPLETION_KEY, default=None
    )
    bundle_id = await sp.session_get(umo, _AGENT_BUNDLE_SESSION_KEY, default=None)

    return {
        "bundle_id": bundle_id,
        "persona_id": persona_id,
        "chat_provider_id": chat_provider_id,
    }


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
    "set_session_kb_config",
    "get_session_kb_config",
    "get_kb_live_info",
    "get_kb_live_info_batch",
    "list_all_core_kbs",
    "write_default_kb_config_for_umo",
    "reset_session_persona_override",
    "clear_chat_provider_preference_for_umo",
    "persona_exists",
    "get_persona_existence_batch",
    "provider_exists",
    "get_provider_existence_batch",
    "set_session_persona",
    "set_session_provider",
    "set_session_bundle_id",
    "clear_session_bundle_id",
    "get_session_agent_bindings",
]
