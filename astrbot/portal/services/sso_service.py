"""SSO/OAuth login, first-login policy, and account-linking service (Phase 5b).

Reuses this module's own existing primitives rather than hand-rolling a
second auth path: token issuance goes through
``astrbot.portal.services.session_service.issue_token_pair`` (the exact same
access/refresh-token minting every local-auth login already uses), and the
callback path is throttled through ``astrbot.portal.auth.rate_limit``'s
existing :class:`~astrbot.portal.auth.rate_limit.LoginThrottle`.

**Read ``astrbot.portal.services.sso.base`` first** -- this module treats a
provider's declared ``trust_basis`` as authoritative and never lets an
adapter-reported :class:`~astrbot.portal.services.sso.base.VerifiedIdentity`
override it (see ``security_decisions``).

The account-takeover surface, and what this module does about it
------------------------------------------------------------------
Matching an SSO identity to an existing local account **by email** is a
takeover vector whenever the presenter of that email is not cryptographically
proven to own it -- and even when it is (a verified OIDC ``email_verified``
claim), auto-linking on first login is still a *silent* privilege escalation
into an account the user never explicitly told this SSO identity to bind
to. This module therefore never does it, in either direction:

* **Login with an unrecognized identity never creates or matches an
  account.** See :func:`_complete_login` / :class:`SsoAccountNotProvisionedError`.
  There is no code path anywhere in this file that looks up a
  ``PortalUser`` by email during an SSO login. An identity is only ever
  resolved to a local account via a pre-existing ``PortalOAuthIdentity``
  row, which can only have been created through the explicit two-step
  linking flow below (or admin tooling, out of this file's scope).
* **Linking to an already-logged-in account requires a second, separate,
  server-verified confirmation step** (:func:`start_link` ->
  :func:`complete_callback` -> :func:`confirm_link`) -- completing the OAuth
  callback alone produces only an opaque, short-lived, single-use
  ``confirmation_token``; no ``PortalOAuthIdentity`` row is written until
  :func:`confirm_link` is called *by the same already-authenticated user_id*
  that started the flow, presenting that exact token.

See ``security_decisions`` in this phase's handoff report for the full
reasoning and the specific things this design deliberately does NOT do
(e.g. no auto-provisioning of new accounts from an unrecognized SSO login).
"""

from __future__ import annotations

import asyncio
import hmac
import secrets
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.auth.rate_limit import get_login_throttle
from astrbot.portal.config import (
    ALL_SSO_PROVIDERS,
    SSO_PROVIDER_FEISHU,
    SSO_PROVIDER_OIDC,
    SSO_PROVIDER_WECOM,
    PortalSettings,
    get_portal_settings,
    get_sso_provider_availability,
)
from astrbot.portal.db.models import PortalOAuthIdentity, PortalUser
from astrbot.portal.services import session_service
from astrbot.portal.services.sso.base import (
    SsoProvider,
    SsoProviderError,
    VerifiedIdentity,
)
from astrbot.portal.services.sso.feishu import FeishuProvider
from astrbot.portal.services.sso.oidc import OidcProvider
from astrbot.portal.services.sso.wecom import WeComProvider

# ---------------------------------------------------------------------------
# Cookie contract for the browser-binding half of CSRF protection.
#
# This is this module's one piece of "cookie naming" surface -- distinct
# from astrbot.portal.config's PORTAL_*_COOKIE_NAME constants (those are the
# already-logged-in session's cookies; this one only ever exists for the
# duration of one in-flight OAuth round trip). Whoever wires the actual
# routes (auth_routes.py, not owned by this file) must:
#
#   * On start: `response.set_cookie(SSO_STATE_COOKIE_NAME, result.state,
#     httponly=True, samesite="lax", secure=<same helper auth_routes.py
#     already uses>, max_age=result.state_cookie_max_age_seconds,
#     path=<the callback path for this provider only>)`.
#   * On callback: read `request.cookies.get(SSO_STATE_COOKIE_NAME)` and
#     pass it as `complete_callback`'s `state_cookie_value` verbatim --
#     never substitute the query-string `state` for both, that would defeat
#     the entire point of the binding check (see `complete_callback`).
# ---------------------------------------------------------------------------

SSO_STATE_COOKIE_NAME = "astrbot_portal_sso_state"

_LINK_CONFIRMATION_TTL_SECONDS = 300
"""How long a completed LINK-purpose callback's confirmation_token remains
presentable to confirm_link before it must be discarded. Short and
independent of PortalSettings.sso_state_ttl_seconds (which bounds the OAuth
round trip itself, a different, earlier window) -- this is the window for
the *second* human action ("yes, actually link this"), not the redirect
round trip. Not a PortalSettings field; see notes_for_integrator for why."""


# ---------------------------------------------------------------------------
# Exceptions. Mirrors session_service.py's own hierarchy shape (a single
# module-rooted base class, specific leaf classes routes can catch by type)
# so callers already familiar with that module's error-handling convention
# do not need a second mental model for this one. astrbot.portal.services.sso.base
# defines a SEPARATE hierarchy (SsoProviderError and subclasses) for
# adapter/protocol-layer failures -- both may propagate out of this module's
# functions; see each function's docstring for which.
# ---------------------------------------------------------------------------


class SsoServiceError(Exception):
    """Base class for every error this module raises directly (as opposed to
    SsoProviderError subclasses propagating up from an adapter)."""


class SsoDisabledError(SsoServiceError):
    """SSO as a whole, or this specific provider, is not enabled/fully
    configured on this deployment. Maps to a 403/404-shaped response --
    never reveal *which* configuration field is missing to an
    unauthenticated caller."""


class UnknownSsoProviderError(SsoServiceError):
    """``provider_id`` is not a member of ``ALL_SSO_PROVIDERS`` at all (a
    typo/unsupported value in a client-constructed URL, not a configuration
    problem)."""


class SsoProviderNotImplementedError(SsoServiceError):
    """``provider_id`` is a real member of ``ALL_SSO_PROVIDERS`` but
    ``_get_provider`` has no branch/adapter wired up for it. Currently
    unreachable in practice -- OIDC, WeCom, and Feishu (the three members of
    ``ALL_SSO_PROVIDERS``) are all dispatched -- but kept in the public
    exception vocabulary (and in ``auth_routes.py``'s mapping) as the
    documented, forward-compatible outcome for a future provider identifier
    that gets added to ``ALL_SSO_PROVIDERS`` before its adapter lands here."""


class SsoRateLimitedError(SsoServiceError):
    """The SSO start or callback path is currently throttled for this
    IP/provider dimension (via the existing ``LoginThrottle``)."""


class InvalidSsoStateError(SsoServiceError):
    """The presented ``state`` (and/or its browser-binding cookie) does not
    correspond to a live, not-yet-consumed pending flow -- covers CSRF
    (forged/foreign state), expiry, and replay (already-consumed state) as
    one generic outcome, deliberately: a caller must not be able to
    distinguish "this state never existed"/"this state was forged for a
    different browser"/"this state already got used" from each other, for
    the same enumeration-resistance reason ``session_service.AuthenticationError``
    collapses "unknown email" and "wrong password"."""


class SsoAccountNotProvisionedError(SsoServiceError):
    """A LOGIN-purpose callback resolved a verified identity that has no
    existing ``PortalOAuthIdentity`` binding to an active local account.
    See module docstring's "account-takeover surface" section for why this
    is a hard stop rather than an auto-provisioning or auto-linking path."""


class SsoIdentityAlreadyLinkedError(SsoServiceError):
    """The verified identity is already bound (via ``PortalOAuthIdentity``)
    to a **different** ``user_id`` than the one attempting to link it."""


class SsoLinkConfirmationError(SsoServiceError):
    """The presented ``confirmation_token`` is invalid, expired,
    already-consumed, or does not belong to the currently authenticated
    caller. Deliberately one generic error for all of those (same
    enumeration-resistance rationale as ``InvalidSsoStateError``)."""


class SsoIdentityNotLinkedError(SsoServiceError):
    """``unlink`` was asked to remove a ``(user_id, provider)`` binding that
    does not exist."""


class SsoCannotRemoveLastCredentialError(SsoServiceError):
    """``unlink`` would leave the account with zero usable sign-in
    methods (no password AND no remaining linked identity) -- refused
    unconditionally; see ``unlink``'s docstring."""


# ---------------------------------------------------------------------------
# Public result shapes.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SsoStartResult:
    """What a ``.../sso/{provider}/start`` route hands back to the browser."""

    authorize_url: str
    """Redirect the browser here."""
    state: str
    """Set this as the value of the ``SSO_STATE_COOKIE_NAME`` cookie (see
    module-level cookie-contract comment) -- this is NOT returned to the
    client as a visible/JS-readable value for any other purpose; its only
    job is to become that one httpOnly cookie's value."""
    state_cookie_max_age_seconds: int


@dataclass(frozen=True)
class SsoLoginResult:
    """A completed LOGIN-purpose callback that resolved to a known, active
    local account. The route should mint cookies from ``token_pair`` exactly
    the way ``auth_routes.py``'s local login does (same
    ``TokenPair``/cookie-setting helper)."""

    token_pair: session_service.TokenPair
    user: PortalUser


@dataclass(frozen=True)
class SsoLinkPendingResult:
    """A completed LINK-purpose callback. **No binding has been written
    yet** -- the caller must still call :func:`confirm_link` with this
    ``confirmation_token`` while authenticated as the same user_id that
    started the flow, or nothing happens and this expires unused."""

    confirmation_token: str
    provider: str
    email: str | None
    display_name: str | None
    expires_at: datetime


class _FlowPurpose(str, Enum):
    LOGIN = "login"
    LINK = "link"


# ---------------------------------------------------------------------------
# Pending-flow storage.
#
# PROCESS-LOCAL in-memory state, deliberately -- matching
# astrbot.portal.auth.rate_limit's existing, explicitly-documented
# single-process/replicas:1 convention (LoginThrottle, InProcessConcurrencyGuard)
# rather than a new DB table: this file's ownership does not include
# astrbot/portal/db/models.py, so a durable, multi-replica-safe
# PortalSsoFlow table is not something this file set can add -- see
# notes_for_integrator for the exact shape a future phase should add there
# if multi-replica deployment is ever required. Every value stored here is
# non-sensitive enough (an OAuth nonce/PKCE verifier tied to one in-flight
# login, or a link-confirmation ticket with a 5-minute TTL) that losing it
# on a process restart is a availability inconvenience (the user re-attempts
# the flow), never a security problem, matching the same trade-off already
# accepted for LoginThrottle's failure/lockout counters.
#
# Single-use is enforced by `_take` atomically popping the entry under the
# lock in the same step that reads it -- there is no separate "mark
# consumed" step for a second, concurrent, or replayed request to race
# against.
# ---------------------------------------------------------------------------


@dataclass
class _PendingFlow:
    provider: str
    purpose: _FlowPurpose
    nonce: str
    code_verifier: str
    redirect_uri: str
    link_user_id: str | None
    expires_at: float


@dataclass
class _PendingLink:
    user_id: str
    provider: str
    subject: str
    email: str | None
    display_name: str | None
    expires_at: float


class _ExpiringSingleUseStore:
    """Generic process-local ``{key: value}`` store with TTL expiry and
    pop-on-read (single-use) semantics, guarded by one lock. Shared
    implementation for both the pending-flow store and the pending-link
    store below -- they have identical lifecycle requirements (short TTL,
    exactly-once consumption) and differ only in the value type stored."""

    def __init__(self) -> None:
        self._entries: dict[str, tuple[float, object]] = {}
        self._lock = asyncio.Lock()

    async def put(self, key: str, value: object, *, expires_at: float) -> None:
        async with self._lock:
            self._evict_expired()
            self._entries[key] = (expires_at, value)

    async def take(self, key: str) -> object | None:
        """Atomically fetch-and-remove. Returns ``None`` for a missing,
        already-consumed, or expired key -- all three collapse to the same
        outcome for the caller, which is exactly the enumeration-resistance
        property ``InvalidSsoStateError``/``SsoLinkConfirmationError``
        need."""
        async with self._lock:
            entry = self._entries.pop(key, None)
            if entry is None:
                return None
            expires_at, value = entry
            if time.monotonic() > expires_at:
                return None
            return value

    def _evict_expired(self) -> None:
        now = time.monotonic()
        stale = [k for k, (exp, _v) in self._entries.items() if now > exp]
        for k in stale:
            del self._entries[k]


_pending_flows = _ExpiringSingleUseStore()
_pending_links = _ExpiringSingleUseStore()


# ---------------------------------------------------------------------------
# Provider registry.
# ---------------------------------------------------------------------------

_oidc_provider_cache: OidcProvider | None = None
_oidc_provider_cache_key: tuple | None = None


def _get_oidc_provider(settings: PortalSettings) -> OidcProvider:
    """Lazily construct (and cache) the process-wide :class:`OidcProvider`.

    Caching the instance (not just its discovery document) matters:
    ``OidcProvider``'s own discovery/JWKS caches live on the instance, so a
    fresh instance per call would defeat both caches and re-fetch on every
    single login. ``PortalSettings`` is an immutable, process-wide singleton
    with no live-reload mechanism (see ``config.py``'s own docstring), so
    keying the cache off its relevant field values and rebuilding only when
    they change is correct without needing any invalidation signal.
    """
    global _oidc_provider_cache, _oidc_provider_cache_key
    key = (
        settings.oidc_client_id,
        settings.oidc_client_secret,
        settings.oidc_issuer,
        settings.oidc_discovery_url,
        settings.oidc_scopes,
    )
    if _oidc_provider_cache is None or _oidc_provider_cache_key != key:
        _oidc_provider_cache = OidcProvider(
            client_id=settings.oidc_client_id or "",
            client_secret=settings.oidc_client_secret or "",
            issuer=settings.oidc_issuer,
            discovery_url=settings.oidc_discovery_url,
            scopes=settings.oidc_scopes,
        )
        _oidc_provider_cache_key = key
    return _oidc_provider_cache


_wecom_provider_cache: WeComProvider | None = None
_wecom_provider_cache_key: tuple | None = None

_feishu_provider_cache: FeishuProvider | None = None
_feishu_provider_cache_key: tuple | None = None


def _get_wecom_provider(settings: PortalSettings) -> WeComProvider:
    """Lazily construct (and cache) the process-wide :class:`WeComProvider`,
    mirroring :func:`_get_oidc_provider`'s rationale: the adapter caches its
    own corp access token across calls, so a fresh instance per request would
    silently throw that cache away (extra gettoken round-trip per login, not
    a correctness bug, but wasted latency/quota -- see notes_for_integrator).

    Only ever called after ``_require_provider_enabled`` has confirmed
    ``get_sso_provider_availability()[SSO_PROVIDER_WECOM]`` is true, so
    ``corp_id``/``agent_id``/``app_secret`` are guaranteed non-empty here and
    ``WeComProvider.__init__``'s own ``ValueError`` guard cannot fire.
    """
    global _wecom_provider_cache, _wecom_provider_cache_key
    key = (settings.wecom_corp_id, settings.wecom_agent_id, settings.wecom_app_secret)
    if _wecom_provider_cache is None or _wecom_provider_cache_key != key:
        _wecom_provider_cache = WeComProvider(
            corp_id=settings.wecom_corp_id or "",
            agent_id=settings.wecom_agent_id or "",
            app_secret=settings.wecom_app_secret or "",
        )
        _wecom_provider_cache_key = key
    return _wecom_provider_cache


def _get_feishu_provider(settings: PortalSettings) -> FeishuProvider:
    """Lazily construct (and cache) the process-wide :class:`FeishuProvider`
    -- same rationale as :func:`_get_wecom_provider` (``lark_oapi``'s
    ``Client`` caches the app access token process-wide, keyed by app_id;
    reconstructing per-request would keep discarding it).

    Only ever called after ``_require_provider_enabled`` has confirmed
    ``get_sso_provider_availability()[SSO_PROVIDER_FEISHU]`` is true, so
    ``app_id``/``app_secret`` are guaranteed non-empty here and
    ``FeishuProvider.__init__``'s own ``ValueError`` guard cannot fire.
    """
    global _feishu_provider_cache, _feishu_provider_cache_key
    key = (settings.feishu_app_id, settings.feishu_app_secret)
    if _feishu_provider_cache is None or _feishu_provider_cache_key != key:
        _feishu_provider_cache = FeishuProvider(
            app_id=settings.feishu_app_id or "",
            app_secret=settings.feishu_app_secret or "",
        )
        _feishu_provider_cache_key = key
    return _feishu_provider_cache


def _get_provider(provider_id: str, settings: PortalSettings) -> SsoProvider:
    if provider_id == SSO_PROVIDER_OIDC:
        return _get_oidc_provider(settings)
    if provider_id == SSO_PROVIDER_WECOM:
        return _get_wecom_provider(settings)
    if provider_id == SSO_PROVIDER_FEISHU:
        return _get_feishu_provider(settings)
    raise UnknownSsoProviderError(f"Unknown SSO provider {provider_id!r}")


def _redirect_uri(provider_id: str, settings: PortalSettings) -> str:
    """The one place this convention is computed -- called identically from
    both the authorize-redirect builder and the callback path, so they can
    never drift apart (ground truth's explicit requirement)."""
    base = (settings.sso_redirect_base_url or "").rstrip("/")
    return f"{base}/oauth/callback/{provider_id}"


def _require_provider_enabled(provider_id: str, settings: PortalSettings) -> None:
    if provider_id not in ALL_SSO_PROVIDERS:
        raise UnknownSsoProviderError(f"Unknown SSO provider {provider_id!r}")
    if not settings.sso_redirect_base_url:
        raise SsoDisabledError("SSO redirect base URL is not configured")
    # Reuse the one sanctioned availability check (config.py) rather than
    # re-deriving "which fields must be present for this provider" a second
    # time here -- avoids the two copies drifting apart.
    availability = get_sso_provider_availability(settings)
    if not availability.get(provider_id, False):
        raise SsoDisabledError(f"SSO provider {provider_id!r} is not enabled/configured")


async def _wrap_provider_call(provider_id: str, coro):
    """Ensure only ``SsoProviderError`` (or a subclass) ever escapes a call
    into an adapter -- an adapter bug that raises some other exception type
    must not leak an unrelated stack trace (potentially containing internal
    URLs/tokens) past this module's boundary as an unhandled 500."""
    try:
        return await coro
    except SsoProviderError:
        raise
    except Exception as exc:  # noqa: BLE001 - intentional catch-all boundary
        logger.error(f"[portal][sso] provider {provider_id!r} adapter raised: {exc!r}")
        raise SsoProviderError(
            f"SSO provider {provider_id!r} adapter failed unexpectedly"
        ) from exc


# ---------------------------------------------------------------------------
# Starting a flow.
# ---------------------------------------------------------------------------


async def start_login(
    *,
    provider_id: str,
    ip: str | None = None,
    settings: PortalSettings | None = None,
) -> SsoStartResult:
    """Start a LOGIN-purpose flow (anonymous caller, no existing session)."""
    return await _start_flow(
        provider_id=provider_id,
        purpose=_FlowPurpose.LOGIN,
        link_user_id=None,
        ip=ip,
        settings=settings,
    )


async def start_link(
    *,
    provider_id: str,
    user_id: str,
    ip: str | None = None,
    settings: PortalSettings | None = None,
) -> SsoStartResult:
    """Start a LINK-purpose flow: ``user_id`` must be the caller's own,
    already-authenticated identity (the route must pass
    ``get_current_user``'s resolved ``user_id``, never a client-supplied
    field -- binding this at *start* time, not just at ``confirm_link``
    time, is what lets ``confirm_link`` later verify "the same account that
    started this" rather than "any currently-logged-in account")."""
    if not user_id:
        raise SsoServiceError("start_link requires an authenticated user_id")
    return await _start_flow(
        provider_id=provider_id,
        purpose=_FlowPurpose.LINK,
        link_user_id=user_id,
        ip=ip,
        settings=settings,
    )


async def _start_flow(
    *,
    provider_id: str,
    purpose: _FlowPurpose,
    link_user_id: str | None,
    ip: str | None,
    settings: PortalSettings | None,
) -> SsoStartResult:
    settings = settings or get_portal_settings()
    _require_provider_enabled(provider_id, settings)

    throttle = get_login_throttle()
    # `account=None`: deliberately IP-only, matching auth_routes.py's own
    # `/auth/refresh` precedent for "no natural per-target account key is
    # known yet". Using `provider_id` (or anything else shared by every
    # caller of this provider) as the second dimension here would be a real
    # bug, not a stricter throttle: LoginThrottle's "account" bucket is one
    # shared bucket per key value, so every legitimate user of a popular
    # provider would compete for the *same* handful of tokens and lock each
    # other out long before any real attacker did.
    decision = await throttle.check(ip=ip, account=None)
    if not decision.allowed:
        raise SsoRateLimitedError("Too many SSO attempts; try again later")

    provider = _get_provider(provider_id, settings)
    redirect_uri = _redirect_uri(provider_id, settings)

    auth_request = await _wrap_provider_call(
        provider_id, provider.build_authorization_request(redirect_uri=redirect_uri)
    )

    expires_at = time.monotonic() + settings.sso_state_ttl_seconds
    flow = _PendingFlow(
        provider=provider_id,
        purpose=purpose,
        nonce=auth_request.provider_state.get("nonce", ""),
        code_verifier=auth_request.provider_state.get("code_verifier", ""),
        redirect_uri=redirect_uri,
        link_user_id=link_user_id,
        expires_at=expires_at,
    )
    await _pending_flows.put(auth_request.state, flow, expires_at=expires_at)

    return SsoStartResult(
        authorize_url=auth_request.authorize_url,
        state=auth_request.state,
        state_cookie_max_age_seconds=settings.sso_state_ttl_seconds,
    )


# ---------------------------------------------------------------------------
# Completing a flow.
# ---------------------------------------------------------------------------


async def complete_callback(
    db_session: AsyncSession,
    *,
    provider_id: str,
    state: str,
    state_cookie_value: str | None,
    code: str,
    ip: str | None = None,
    user_agent: str | None = None,
    settings: PortalSettings | None = None,
) -> SsoLoginResult | SsoLinkPendingResult:
    """Resolve an OAuth callback: verify the CSRF/browser binding, resolve
    the verified identity via the provider adapter, then either log the
    caller in (LOGIN purpose) or produce a pending link confirmation (LINK
    purpose) -- never both, and never a silent account bind either way.

    Raises:
        SsoDisabledError / UnknownSsoProviderError: as ``start_*``.
        SsoRateLimitedError: callback path throttled for this IP/provider.
        InvalidSsoStateError: state missing/forged/expired/already-consumed,
            or the browser-binding cookie does not match the query state.
        SsoProviderError (or subclass): the provider adapter itself failed
            to resolve a verified identity (bad signature, failed token
            exchange, network error, ...).
        SsoAccountNotProvisionedError: (LOGIN only) no existing binding for
            this identity to an active account.
    """
    settings = settings or get_portal_settings()
    _require_provider_enabled(provider_id, settings)

    throttle = get_login_throttle()
    # `account=None` for exactly the same reason as `_start_flow` above: the
    # only key available before resolving the identity is `provider_id`,
    # which is shared by every caller of this provider and would create one
    # global bucket rather than a per-attacker one. IP-only mirrors
    # `/auth/refresh`'s existing precedent.
    decision = await throttle.check(ip=ip, account=None)
    if not decision.allowed:
        raise SsoRateLimitedError("Too many SSO callback attempts; try again later")

    # Browser-binding check FIRST -- before the pending-flow store is even
    # consulted, so a mismatched/absent cookie is rejected identically to an
    # unrecognized state (no distinguishing timing/behavior between "this
    # exact state exists server-side but for a different browser" and "this
    # state doesn't exist at all").
    if not _constant_time_str_eq(state, state_cookie_value):
        throttle.record_failure(ip=ip, account=None)
        raise InvalidSsoStateError(
            "SSO state does not match this browser's pending flow"
        )

    flow = await _pending_flows.take(state)
    if not isinstance(flow, _PendingFlow) or flow.provider != provider_id:
        throttle.record_failure(ip=ip, account=None)
        raise InvalidSsoStateError(
            "SSO state is unrecognized, expired, or already used"
        )

    provider = _get_provider(provider_id, settings)
    try:
        identity = await _wrap_provider_call(
            provider_id,
            provider.resolve_identity(
                code=code,
                redirect_uri=flow.redirect_uri,
                provider_state={
                    "nonce": flow.nonce,
                    "code_verifier": flow.code_verifier,
                },
            ),
        )
    except SsoProviderError:
        throttle.record_failure(ip=ip, account=None)
        raise

    # Defense-in-depth against a buggy/malicious adapter: never accept an
    # identity's self-reported trust_basis over the adapter's own statically
    # declared one. See sso.base module docstring.
    if identity.trust_basis != provider.trust_basis:
        throttle.record_failure(ip=ip, account=None)
        logger.error(
            f"[portal][sso] provider {provider_id!r} adapter declares "
            f"trust_basis={provider.trust_basis!r} but returned an identity "
            f"claiming {identity.trust_basis!r}; rejecting the whole callback"
        )
        raise SsoProviderError(
            f"SSO provider {provider_id!r} returned an inconsistent trust basis"
        )

    throttle.record_success(ip=ip, account=None)

    if flow.purpose is _FlowPurpose.LINK:
        return await _prepare_link(
            identity=identity, link_user_id=flow.link_user_id
        )

    return await _complete_login(
        db_session, identity=identity, ip=ip, user_agent=user_agent
    )


async def _complete_login(
    db_session: AsyncSession,
    *,
    identity: VerifiedIdentity,
    ip: str | None,
    user_agent: str | None,
) -> SsoLoginResult:
    """LOGIN-purpose resolution: the ONLY lookup this ever performs is
    ``PortalOAuthIdentity`` by ``(provider, subject)`` -- never by email,
    never a fuzzy/best-effort match. See module docstring's
    "account-takeover surface" section."""
    result = await db_session.execute(
        select(PortalOAuthIdentity).where(
            col(PortalOAuthIdentity.provider) == identity.provider,
            col(PortalOAuthIdentity.provider_subject) == identity.subject,
        )
    )
    binding = result.scalar_one_or_none()
    if binding is None:
        raise SsoAccountNotProvisionedError(
            "No local account is linked to this identity. Ask an operator "
            "for an invite, or log in locally and link this identity from "
            "account settings."
        )

    user = await db_session.get(PortalUser, binding.user_id)
    if user is None or user.status != "active":
        # Same generic rejection whether the bound user row vanished or is
        # merely disabled/still-invited -- never distinguish, same
        # enumeration-resistance rationale as session_service.AuthenticationError.
        raise SsoAccountNotProvisionedError(
            "This identity is not linked to an active account"
        )

    token_pair = await session_service.issue_token_pair(
        db_session, user_id=user.user_id, user_agent=user_agent, ip=ip
    )
    logger.info(
        f"[portal][sso] login via {identity.provider} for user {user.user_id}"
    )
    return SsoLoginResult(token_pair=token_pair, user=user)


async def _prepare_link(
    *, identity: VerifiedIdentity, link_user_id: str | None
) -> SsoLinkPendingResult:
    if not link_user_id:
        # Defensive: _start_flow never stores a LINK-purpose flow without a
        # link_user_id, so this should be unreachable in practice.
        raise SsoServiceError("LINK-purpose callback has no initiating user recorded")

    confirmation_token = secrets.token_urlsafe(32)
    expires_at = time.monotonic() + _LINK_CONFIRMATION_TTL_SECONDS
    pending = _PendingLink(
        user_id=link_user_id,
        provider=identity.provider,
        subject=identity.subject,
        email=identity.email,
        display_name=identity.display_name,
        expires_at=expires_at,
    )
    await _pending_links.put(confirmation_token, pending, expires_at=expires_at)

    return SsoLinkPendingResult(
        confirmation_token=confirmation_token,
        provider=identity.provider,
        email=identity.email,
        display_name=identity.display_name,
        expires_at=datetime.now(timezone.utc)
        + timedelta(seconds=_LINK_CONFIRMATION_TTL_SECONDS),
    )


# ---------------------------------------------------------------------------
# Step two: explicit confirmation. The mere completion of the OAuth callback
# above (_prepare_link) never wrote a PortalOAuthIdentity row -- this is the
# only function in this module that does, and only for a LINK-purpose flow.
# ---------------------------------------------------------------------------


async def confirm_link(
    db_session: AsyncSession,
    *,
    user_id: str,
    confirmation_token: str,
) -> PortalOAuthIdentity:
    """Second confirmation step: bind the identity resolved by a prior
    LINK-purpose ``complete_callback`` call to ``user_id``.

    ``user_id`` must be the caller's own currently-authenticated identity
    (the route must pass ``get_current_user``'s resolved ``user_id``, never
    a client-supplied field) -- this is what makes the confirmation
    unforgeable: even a stolen/leaked ``confirmation_token`` is useless to
    an attacker unless they are also currently authenticated as the exact
    account that originally started the link flow (checked below), and
    conversely a victim tricked into visiting a confirmation URL while
    logged into their OWN account cannot have someone else's identity bound
    to it, since the token only unlocks the specific ``user_id`` recorded
    when the flow started.

    The token is popped from the pending-link store (single-use) before any
    check below runs, so a replayed/retried token always fails here
    regardless of which check would otherwise have failed -- there is no
    window where presenting the same token twice succeeds twice.

    Raises:
        SsoLinkConfirmationError: token invalid/expired/already-consumed, or
            does not belong to ``user_id``.
        SsoIdentityAlreadyLinkedError: the identity is already bound to a
            different account (checked twice: once against the read-time
            state to fail fast, and again at commit time via the table's
            existing unique constraint to close the race between the two
            reads).
    """
    pending = await _pending_links.take(confirmation_token)
    if not isinstance(pending, _PendingLink):
        raise SsoLinkConfirmationError(
            "Link confirmation is invalid, expired, or already used"
        )

    if not hmac.compare_digest(pending.user_id, user_id):
        raise SsoLinkConfirmationError(
            "Link confirmation does not belong to this session"
        )

    existing = await db_session.execute(
        select(PortalOAuthIdentity).where(
            col(PortalOAuthIdentity.provider) == pending.provider,
            col(PortalOAuthIdentity.provider_subject) == pending.subject,
        )
    )
    row = existing.scalar_one_or_none()
    if row is not None:
        if row.user_id == user_id:
            return row  # Already linked to this same account: idempotent no-op.
        raise SsoIdentityAlreadyLinkedError(
            "This identity is already linked to a different account"
        )

    identity_row = PortalOAuthIdentity(
        user_id=user_id,
        provider=pending.provider,
        provider_subject=pending.subject,
    )
    db_session.add(identity_row)
    try:
        await db_session.commit()
    except IntegrityError:
        await db_session.rollback()
        # Someone else's confirm_link (or admin action) won the race between
        # our read above and this commit.
        raise SsoIdentityAlreadyLinkedError(
            "This identity was linked to another account concurrently"
        ) from None

    await db_session.refresh(identity_row)
    logger.info(
        f"[portal][sso] linked {pending.provider} identity to user {user_id}"
    )
    return identity_row


# ---------------------------------------------------------------------------
# Unlinking.
# ---------------------------------------------------------------------------


async def unlink(
    db_session: AsyncSession,
    *,
    user_id: str,
    provider_id: str,
) -> None:
    """Remove a linked identity, refusing if it would leave the account with
    no usable sign-in method at all (no password AND no other linked
    identity) -- a user must never be able to lock themselves out via this
    path. An admin-facing "force unlink" (e.g. to help a user who lost
    access to their IdP) is out of this file's scope; this function is only
    ever the self-service path.
    """
    result = await db_session.execute(
        select(PortalOAuthIdentity).where(
            col(PortalOAuthIdentity.user_id) == user_id,
            col(PortalOAuthIdentity.provider) == provider_id,
        )
    )
    row = result.scalar_one_or_none()
    if row is None:
        raise SsoIdentityNotLinkedError(
            f"No {provider_id} identity is linked to this account"
        )

    user = await db_session.get(PortalUser, user_id)
    if user is None:
        raise SsoServiceError("Authenticated user row vanished during unlink")

    other_count_result = await db_session.execute(
        select(func.count())
        .select_from(PortalOAuthIdentity)
        .where(
            col(PortalOAuthIdentity.user_id) == user_id,
            col(PortalOAuthIdentity.provider) != provider_id,
        )
    )
    other_count = other_count_result.scalar_one()

    has_password = bool(user.password_hash)
    if not has_password and other_count == 0:
        raise SsoCannotRemoveLastCredentialError(
            "Cannot unlink your only sign-in method; set a password or link "
            "another identity first"
        )

    await db_session.delete(row)
    await db_session.commit()
    logger.info(f"[portal][sso] unlinked {provider_id} identity from user {user_id}")


async def list_linked_providers(
    db_session: AsyncSession, *, user_id: str
) -> list[str]:
    """Return the list of provider ids currently linked to ``user_id`` --
    for an account-settings page to render "linked identities" without a
    route needing to know this table's shape directly."""
    result = await db_session.execute(
        select(PortalOAuthIdentity.provider).where(
            col(PortalOAuthIdentity.user_id) == user_id
        )
    )
    return list(result.scalars().all())


# ---------------------------------------------------------------------------
# Internals.
# ---------------------------------------------------------------------------


def _constant_time_str_eq(a: str | None, b: str | None) -> bool:
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a, b)


__all__ = [
    "SSO_STATE_COOKIE_NAME",
    "SsoServiceError",
    "SsoDisabledError",
    "UnknownSsoProviderError",
    "SsoProviderNotImplementedError",
    "SsoRateLimitedError",
    "InvalidSsoStateError",
    "SsoAccountNotProvisionedError",
    "SsoIdentityAlreadyLinkedError",
    "SsoLinkConfirmationError",
    "SsoIdentityNotLinkedError",
    "SsoCannotRemoveLastCredentialError",
    "SsoStartResult",
    "SsoLoginResult",
    "SsoLinkPendingResult",
    "start_login",
    "start_link",
    "complete_callback",
    "confirm_link",
    "unlink",
    "list_linked_providers",
]
