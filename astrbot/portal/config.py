"""Typed configuration for the AstrBot Portal module.

Capability-wall decision (rule 5 of the module's engineering rules): only
``astrbot/portal/services/core_facade.py`` is allowed to import
``astrbot.core.utils.shared_preferences.SharedPreferences``. This module
must therefore never import it, directly or transitively -- and it does
not.

How ``get_portal_jwt_secret`` still works without that import: this module
exposes the *policy* (which scope/scope_id/key to persist under, and the
lazy-generate-on-first-use behaviour) as a small pure function that takes
the preference get/put operations as **injected callables**, matching the
exact async call shape of ``SharedPreferences.get_async``/``put_async``.
Whoever owns ``core_facade.py`` is expected to call it like this::

    from astrbot.portal.config import get_portal_jwt_secret

    secret = await get_portal_jwt_secret(sp.get_async, sp.put_async)

where ``sp`` is the process-wide ``SharedPreferences`` instance (imported
only inside ``core_facade.py``). This keeps ``config.py`` free of any
``SharedPreferences`` symbol -- so it stays trivially unit-testable with
plain fakes and never violates the capability wall -- while still producing
exactly the persisted value the design requires:
``scope='global', scope_id='portal', key='jwt_secret'``. This secret is
never ``config['dashboard']['jwt_secret']`` and is never derived from it
(rule 6).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# JWT secret storage location (SharedPreferences scope/scope_id/key triple).
# ---------------------------------------------------------------------------

PORTAL_JWT_SECRET_SCOPE = "global"
PORTAL_JWT_SECRET_SCOPE_ID = "portal"
PORTAL_JWT_SECRET_KEY = "jwt_secret"

# ---------------------------------------------------------------------------
# Cookie / header names. These must never collide with the core dashboard's
# `astrbot_dashboard_jwt` cookie, so that no proxy/middleware/browser can
# ever confuse a portal end-user session with a core dashboard admin session
# (rule 6).
# ---------------------------------------------------------------------------

PORTAL_JWT_COOKIE_NAME = "astrbot_portal_jwt"
PORTAL_REFRESH_COOKIE_NAME = "astrbot_portal_refresh"
PORTAL_CSRF_COOKIE_NAME = "astrbot_portal_csrf"
PORTAL_CSRF_HEADER_NAME = "X-Portal-CSRF-Token"


@dataclass(frozen=True)
class PortalSettings:
    """Immutable, process-wide settings for the portal module.

    None of these values are secret; the JWT signing secret itself is
    handled separately by :func:`get_portal_jwt_secret` (see module
    docstring for why it lives outside this dataclass).
    """

    # --- Token lifetimes ---------------------------------------------------
    access_token_ttl_seconds: int = 15 * 60
    """Access JWT TTL: 15 minutes."""
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    """Refresh token TTL: 30 days."""

    # --- Cookie / CSRF wiring ------------------------------------------------
    jwt_cookie_name: str = PORTAL_JWT_COOKIE_NAME
    refresh_cookie_name: str = PORTAL_REFRESH_COOKIE_NAME
    csrf_cookie_name: str = PORTAL_CSRF_COOKIE_NAME
    csrf_header_name: str = PORTAL_CSRF_HEADER_NAME
    refresh_cookie_path: str = "/api/v1/portal/auth/refresh"
    """Path scope for the refresh cookie: only sent to the refresh endpoint."""

    # --- Registration ---------------------------------------------------
    self_registration_open: bool = True
    """Whether POST /api/v1/portal/auth/register is enabled (Phase 1 default).

    When an admin disables self-registration (Phase 5a lands
    admin-provisioned invites), this should be flipped to False.
    """

    # --- Auth endpoint rate limiting (register/login/refresh) --------------
    # Shaped after astrbot.dashboard.server._AuthRateLimiter's token-bucket.
    auth_rate_limit_capacity: int = 5
    """Token-bucket capacity per IP+email dimension."""
    auth_rate_limit_refill_per_minute: float = 1.0
    """Tokens refilled per minute."""
    auth_failure_delay_seconds: float = 3.0
    """Fixed delay applied after a failed login attempt (timing-attack /
    brute-force mitigation, mirrors astrbot.dashboard's auth_service.py
    pattern)."""

    # --- Per-user quotas backing PortalUsageCounter checks -------------------
    max_requests_per_window: int = 200
    """Max chat-message requests allowed within one usage window."""
    usage_window_seconds: int = 24 * 60 * 60
    """Length of one usage-counter accounting window: 1 day."""
    max_concurrent_runs_per_user: int = 2
    """Max simultaneously in-flight chat runs allowed for a single user."""

    # --- Upload policy (Phase 2: file/image attachments) --------------------
    #
    # None of these limits exist in core today: `ChatService.save_uploaded_file`
    # (`astrbot/dashboard/services/chat_service.py:592-640`) has no size cap
    # and no MIME allowlist whatsoever -- it derives `attach_type` purely from
    # the client-supplied multipart `content_type` and, for anything that
    # isn't an image, never even sniffs the real bytes. The portal is the
    # only layer that will ever enforce these, so getting sane defaults here
    # matters; see `PortalAttachmentOwner` (db/models.py) for the byte_size
    # bookkeeping and `PortalUsageCounter.uploaded_bytes` for the rolling
    # daily total these limits are checked against.
    max_upload_file_bytes: int = 20 * 1024 * 1024
    """Hard cap on a single uploaded file, in bytes (20 MiB).

    Chosen as a middle ground for a consumer chat product: comfortably
    above a typical phone camera photo (2-8 MB) or a multi-minute voice
    recording encoded as compressed audio, while still small enough that
    a handful of concurrent uploads can't exhaust disk or memory on a
    single-process, single-SQLite-writer deployment (see plan section 6's
    SQLite single-writer risk entry). Deliberately conservative rather
    than generous: raising a limit later is a trivial, low-risk config
    change; a deployment that got hit with disk exhaustion because the
    default was too permissive is not. Applies uniformly across all
    `attach_type`s for Phase 2 -- there is no per-type override yet.
    """

    max_daily_upload_bytes: int = 200 * 1024 * 1024
    """Rolling per-user daily upload quota, in bytes (200 MiB).

    Sized as roughly 10x `max_upload_file_bytes`, i.e. a real user can
    still have a heavy attachment day (multiple photos, a couple of voice
    notes, a document) without hitting the ceiling, while a compromised or
    abusive account can't use the upload endpoint as free unbounded
    storage against the operator's disk. Checked against
    `PortalUsageCounter.uploaded_bytes` for the current day's window
    before the upload route accepts a file; when exceeded the route
    should return 429 with the remaining-quota figure (plan section 5,
    Phase 2 acceptance criterion 3), not silently truncate or drop the
    upload.
    """

    allowed_upload_mime_types: frozenset[str] = frozenset(
        {
            # Images (attach_type='image'; core re-sniffs the real bytes for
            # these via detect_image_mime_type_async, so this allowlist is
            # the pre-sniff gate on the client's declared content_type).
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
            # Audio / voice messages (attach_type='record'). Core does NOT
            # sniff these -- this allowlist is the only validation they get.
            "audio/mpeg",
            "audio/mp4",
            "audio/wav",
            "audio/x-wav",
            "audio/webm",
            "audio/ogg",
            "audio/aac",
            # Video (attach_type='video'). Also unsniffed by core.
            "video/mp4",
            "video/webm",
            "video/quicktime",
            # Plain documents (attach_type='file'). Also unsniffed by core.
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/json",
        }
    )
    """MIME-type allowlist for uploads, enforced against the client-declared
    `content_type` before core ever writes bytes to disk (and, for images,
    re-checked against core's post-sniff detected MIME).

    Deliberately excludes anything that renders/executes as active content
    if served back to a browser or that commonly disguises unwanted
    payloads: no `image/svg+xml` (SVG can carry embedded `<script>`, an XSS
    vector when this attachment is later served or previewed), no
    `text/html`/`application/javascript`, no archive formats
    (`application/zip` and friends -- zip-bomb/decompression and
    "malware disguised as a document" risk, and archives are not a useful
    input to a multimodal chat model anyway), and no Office macro-enabled
    formats. Legacy/macro-enabled MS Office (`.doc`/`.xls`/`.ppt`,
    `application/vnd.ms-*`) and their modern OOXML equivalents
    (`application/vnd.openxmlformats-officedocument.*`) are also left off
    this default list -- if a deployment needs them, add the specific MIME
    types deliberately rather than opening the whole Office family
    (`.doc`/`.xls` can carry macros; the newer OOXML types are safer but
    still parsed by third-party libraries with their own attack surface).
    This is a static allowlist for Phase 2; there is no admin-configurable
    override yet (matches this dataclass's existing "no on-disk override
    mechanism" limitation noted on `get_portal_settings`).
    """

    allowed_upload_attach_types: frozenset[str] = frozenset(
        {"image", "record", "video", "file"}
    )
    """Allowlist of `attach_type` buckets (core's own vocabulary from
    `ChatService.save_uploaded_file`: 'image' | 'record' | 'video' | 'file')
    the upload route will accept. Ships with all four enabled by default --
    the MIME allowlist above is what actually keeps this Phase 2 default
    narrow and safe, this list exists as a second, independent knob (e.g. a
    deployment that wants to disable video uploads entirely to save disk,
    without touching the MIME list) rather than as the primary gate.
    """

    # --- Knowledge-base policy (Phase 3: KB visibility + session binding) ---
    #
    # See `astrbot/core/tools/knowledge_base_tools.py:24-87` for the
    # default-open trap these settings exist alongside: a session with no
    # explicit `kb_config` silently falls through to the operator's *global*
    # `kb_names` config, not to "no KB". These settings only govern the
    # *portal-authorized* binding path (`PortalKbVisibility`, db/models.py) --
    # they do not by themselves close that trap; the service/route layer
    # closes it by always writing an explicit `kb_config` (empty `kb_ids` by
    # default) rather than leaving the key unset. `kb_selection_enabled`
    # below is the master switch for whether that binding path is even
    # reachable at all.
    kb_selection_enabled: bool = True
    """Whether end users may see/bind any knowledge base at all.

    Defaults on since Phase 3 assumes a deployment that wants the feature,
    but an operator running a plain chatbot with no KB use case at all
    should be able to turn the entire surface off with one flag -- when
    `False`, the KB list/bind routes should refuse the request outright
    (this module's own choice, not a core one) rather than simply returning
    an empty whitelist, so the feature is verifiably inert rather than
    quietly empty.
    """

    max_kb_ids_per_session: int = 3
    """Maximum number of distinct `kb_id`s a single session's `kb_config`
    may bind at once.

    Chosen as a small, deliberately conservative ceiling for a
    consumer-facing product: `kb_mgr.retrieve` (invoked from
    `retrieve_knowledge_base`) fans a single query out across every bound
    KB and concatenates results, so each additional KB directly multiplies
    retrieval latency and LLM context spent on (often redundant) retrieved
    chunks for one user turn. Three is enough for a real multi-corpus use
    case (e.g. "product docs" + "internal FAQ" + "policy handbook"
    simultaneously) without letting a session silently accumulate the
    operator's entire whitelist over time and turn every message into a
    full-catalog fan-out search.
    """

    default_kb_top_k: int = 5
    """Default `top_k` applied to a session's `kb_config` when a user binds
    KBs without specifying one.

    Matches core's own fallback default for the global (non-portal) path
    (`config.get("kb_final_top_k", 5)` in
    `astrbot/core/tools/knowledge_base_tools.py`), so a portal-bound session
    behaves like the operator's existing global-KB behavior in the
    dimension the portal doesn't have an opinion on, rather than silently
    picking a different retrieval depth than what the operator is already
    used to seeing.
    """

    max_kb_top_k: int = 10
    """Hard ceiling on the `top_k` a user-facing request may set for their
    own session's `kb_config`, independent of `default_kb_top_k`.

    `top_k` (unlike `kb_fusion_top_k`, which always comes from global
    config and is never user-controllable at all -- see
    `knowledge_base_tools.py`) is read from the *session's own* config, so
    without a ceiling here a client could request an arbitrarily large
    `top_k` and turn one chat message into a disproportionately expensive
    retrieval + a context window stuffed with low-relevance chunks. Set
    to 2x the default: enough headroom for a user who genuinely wants a
    broader search on one question, without allowing an unbounded value.
    """


DEFAULT_PORTAL_SETTINGS = PortalSettings()


def get_portal_settings() -> PortalSettings:
    """Return the module's settings.

    Phase 1 ships no on-disk override mechanism; this indirection exists so
    a later phase can source overrides (env vars, an admin-configurable
    table) without changing every call site.
    """
    return DEFAULT_PORTAL_SETTINGS


# ---------------------------------------------------------------------------
# JWT secret: lazily generated, persisted via injected SharedPreferences
# accessors (see module docstring for the capability-wall rationale).
# ---------------------------------------------------------------------------

PreferenceGetter = Callable[[str, str, str], Awaitable[str | None]]
"""Matches the call shape of ``SharedPreferences.get_async(scope, scope_id, key)``."""

PreferenceSetter = Callable[[str, str, str, str], Awaitable[None]]
"""Matches the call shape of ``SharedPreferences.put_async(scope, scope_id, key, value)``."""

_secret_generation_lock = asyncio.Lock()


async def get_portal_jwt_secret(
    get_preference: PreferenceGetter,
    put_preference: PreferenceSetter,
) -> str:
    """Lazily fetch-or-generate the portal module's own JWT signing secret.

    ``get_preference``/``put_preference`` must be callables with the exact
    call shape of ``SharedPreferences.get_async``/``put_async`` -- pass the
    bound methods of the process's ``SharedPreferences`` instance directly::

        secret = await get_portal_jwt_secret(sp.get_async, sp.put_async)

    On first call (no existing value under
    ``scope='global', scope_id='portal', key='jwt_secret'``), generates a
    fresh 32-byte cryptographically random urlsafe-base64 string via
    ``secrets.token_urlsafe(32)`` and persists it before returning it.
    Subsequent calls return the persisted value unchanged. An in-process
    lock prevents two concurrent first-callers in the same process from
    generating (and persisting) two different values.
    """
    async with _secret_generation_lock:
        existing = await get_preference(
            PORTAL_JWT_SECRET_SCOPE,
            PORTAL_JWT_SECRET_SCOPE_ID,
            PORTAL_JWT_SECRET_KEY,
        )
        if existing:
            return existing

        new_secret = secrets.token_urlsafe(32)
        await put_preference(
            PORTAL_JWT_SECRET_SCOPE,
            PORTAL_JWT_SECRET_SCOPE_ID,
            PORTAL_JWT_SECRET_KEY,
            new_secret,
        )
        return new_secret
