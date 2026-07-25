"""Typed configuration for the AstrBot WebUI module.

Capability-wall decision (rule 5 of the module's engineering rules): only
``astrbot/webui/services/core_facade.py`` is allowed to import
``astrbot.core.utils.shared_preferences.SharedPreferences``. This module
must therefore never import it, directly or transitively -- and it does
not.

How ``get_webui_jwt_secret`` still works without that import: this module
exposes the *policy* (which scope/scope_id/key to persist under, and the
lazy-generate-on-first-use behaviour) as a small pure function that takes
the preference get/put operations as **injected callables**, matching the
exact async call shape of ``SharedPreferences.get_async``/``put_async``.
Whoever owns ``core_facade.py`` is expected to call it like this::

    from astrbot.webui.config import get_webui_jwt_secret

    secret = await get_webui_jwt_secret(sp.get_async, sp.put_async)

where ``sp`` is the process-wide ``SharedPreferences`` instance (imported
only inside ``core_facade.py``). This keeps ``config.py`` free of any
``SharedPreferences`` symbol -- so it stays trivially unit-testable with
plain fakes and never violates the capability wall -- while still producing
exactly the persisted value the design requires:
``scope='global', scope_id='webui', key='jwt_secret'``. This secret is
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

WEBUI_JWT_SECRET_SCOPE = "global"
WEBUI_JWT_SECRET_SCOPE_ID = "webui"
WEBUI_JWT_SECRET_KEY = "jwt_secret"

# ---------------------------------------------------------------------------
# Cookie / header names. These must never collide with the core dashboard's
# `astrbot_dashboard_jwt` cookie, so that no proxy/middleware/browser can
# ever confuse a webui end-user session with a core dashboard admin session
# (rule 6).
# ---------------------------------------------------------------------------

WEBUI_JWT_COOKIE_NAME = "astrbot_webui_jwt"
WEBUI_REFRESH_COOKIE_NAME = "astrbot_webui_refresh"
WEBUI_CSRF_COOKIE_NAME = "astrbot_webui_csrf"
WEBUI_CSRF_HEADER_NAME = "X-Webui-CSRF-Token"


@dataclass(frozen=True)
class WebUiSettings:
    """Immutable, process-wide settings for the webui module.

    None of these values are secret; the JWT signing secret itself is
    handled separately by :func:`get_webui_jwt_secret` (see module
    docstring for why it lives outside this dataclass).
    """

    # --- Token lifetimes ---------------------------------------------------
    access_token_ttl_seconds: int = 15 * 60
    """Access JWT TTL: 15 minutes."""
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    """Refresh token TTL: 30 days."""

    # --- Cookie / CSRF wiring ------------------------------------------------
    jwt_cookie_name: str = WEBUI_JWT_COOKIE_NAME
    refresh_cookie_name: str = WEBUI_REFRESH_COOKIE_NAME
    csrf_cookie_name: str = WEBUI_CSRF_COOKIE_NAME
    csrf_header_name: str = WEBUI_CSRF_HEADER_NAME
    refresh_cookie_path: str = "/api/v1/webui/auth/refresh"
    """Path scope for the refresh cookie: only sent to the refresh endpoint."""

    # --- Registration ---------------------------------------------------
    self_registration_open: bool = True
    """Whether POST /api/v1/webui/auth/register is enabled (Phase 1 default).

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

    # --- Per-user quotas backing WebUiUsageCounter checks -------------------
    max_requests_per_window: int = 200
    """Max chat-message requests allowed within one usage window."""
    usage_window_seconds: int = 24 * 60 * 60
    """Length of one usage-counter accounting window: 1 day."""
    max_concurrent_runs_per_user: int = 2
    """Max simultaneously in-flight chat runs allowed for a single user."""


DEFAULT_WEBUI_SETTINGS = WebUiSettings()


def get_webui_settings() -> WebUiSettings:
    """Return the module's settings.

    Phase 1 ships no on-disk override mechanism; this indirection exists so
    a later phase can source overrides (env vars, an admin-configurable
    table) without changing every call site.
    """
    return DEFAULT_WEBUI_SETTINGS


# ---------------------------------------------------------------------------
# JWT secret: lazily generated, persisted via injected SharedPreferences
# accessors (see module docstring for the capability-wall rationale).
# ---------------------------------------------------------------------------

PreferenceGetter = Callable[[str, str, str], Awaitable[str | None]]
"""Matches the call shape of ``SharedPreferences.get_async(scope, scope_id, key)``."""

PreferenceSetter = Callable[[str, str, str, str], Awaitable[None]]
"""Matches the call shape of ``SharedPreferences.put_async(scope, scope_id, key, value)``."""

_secret_generation_lock = asyncio.Lock()


async def get_webui_jwt_secret(
    get_preference: PreferenceGetter,
    put_preference: PreferenceSetter,
) -> str:
    """Lazily fetch-or-generate the webui module's own JWT signing secret.

    ``get_preference``/``put_preference`` must be callables with the exact
    call shape of ``SharedPreferences.get_async``/``put_async`` -- pass the
    bound methods of the process's ``SharedPreferences`` instance directly::

        secret = await get_webui_jwt_secret(sp.get_async, sp.put_async)

    On first call (no existing value under
    ``scope='global', scope_id='webui', key='jwt_secret'``), generates a
    fresh 32-byte cryptographically random urlsafe-base64 string via
    ``secrets.token_urlsafe(32)`` and persists it before returning it.
    Subsequent calls return the persisted value unchanged. An in-process
    lock prevents two concurrent first-callers in the same process from
    generating (and persisting) two different values.
    """
    async with _secret_generation_lock:
        existing = await get_preference(
            WEBUI_JWT_SECRET_SCOPE,
            WEBUI_JWT_SECRET_SCOPE_ID,
            WEBUI_JWT_SECRET_KEY,
        )
        if existing:
            return existing

        new_secret = secrets.token_urlsafe(32)
        await put_preference(
            WEBUI_JWT_SECRET_SCOPE,
            WEBUI_JWT_SECRET_SCOPE_ID,
            WEBUI_JWT_SECRET_KEY,
            new_secret,
        )
        return new_secret
