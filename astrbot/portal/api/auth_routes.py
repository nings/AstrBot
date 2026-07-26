"""Auth routes for the AstrBot Portal module: register/login/refresh/logout/me.

This router declares only the ``/auth`` sub-prefix; ``router.py`` aggregates
it (alongside the chat routes) under the module-wide ``/portal`` prefix,
which is itself aggregated under ``/api/v1`` by
``astrbot.dashboard.api.router.build_api_router``, so the final public
paths are:

    POST /api/v1/portal/auth/register
    POST /api/v1/portal/auth/login
    POST /api/v1/portal/auth/refresh
    POST /api/v1/portal/auth/logout
    GET  /api/v1/portal/auth/me

Phase 5 adds two more identity paths on top of the five above, both built
entirely on the primitives already established here (cookies, CSRF,
throttling) rather than a second token path:

    GET  /api/v1/portal/auth/sso/providers               (public)
    GET  /api/v1/portal/auth/sso/{provider}/start         (public, 302 to IdP)
    GET  /api/v1/portal/auth/sso/{provider}/callback      (public)
    POST /api/v1/portal/auth/sso/{provider}/link          (authenticated + CSRF)
    POST /api/v1/portal/auth/sso/{provider}/link/confirm  (authenticated + CSRF)
    POST /api/v1/portal/auth/sso/{provider}/unlink        (authenticated + CSRF)
    GET  /api/v1/portal/auth/sso/linked                   (authenticated)
    POST /api/v1/portal/auth/invite/redeem                (public, rate-limited)

All SSO business logic (state/nonce/PKCE, provider adapters, account-
linking/takeover guards) lives in ``astrbot.portal.services.sso_service`` --
this file only ever wires HTTP request/response shapes (query params,
cookies, JSON bodies, status codes) to that module's functions and to
``astrbot.portal.services.invite_service`` for invite redemption. Neither
service module is owned by this assignment; their exception vocabularies
and recommended HTTP mappings are treated as the single source of truth
(see each module's own docstring) and mirrored here, not re-derived.

This file only ever talks to the portal module's own database
(``astrbot.portal.db.engine.get_portal_db_session``) via
``astrbot.portal.services.session_service``/``sso_service``/``invite_service``
-- it never imports ``astrbot.dashboard.*`` or any capability-wall-forbidden
core service (rule 5): it is not the capability wall (``core_facade.py``),
and none of these endpoints need to reach ``ChatService``/
``conversation_mgr``/etc. at all.

Identity is always derived from the verified ``astrbot_portal_jwt`` cookie
(see ``astrbot.portal.auth.dependencies.get_current_user``) -- never from any
client-supplied field, per rule 4. For the SSO link/confirm/unlink routes
below, this means ``user_id`` is always ``get_current_user``'s resolved
``PortalUser.user_id``, never anything read from the request body -- the
whole unforgeability property ``sso_service.confirm_link`` relies on
(binding a link confirmation to "the same account that started the flow")
depends on that being true.
"""

from __future__ import annotations

from typing import Literal, NoReturn
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict
from sqlalchemy.ext.asyncio import AsyncSession

from astrbot import logger
from astrbot.portal.auth.dependencies import get_current_user, require_csrf
from astrbot.portal.auth.rate_limit import apply_failure_delay, get_login_throttle
from astrbot.portal.auth.security import (
    TokenError,
    generate_csrf_token,
    verify_access_token,
)
from astrbot.portal.config import get_portal_settings, get_sso_provider_availability
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import invite_service, session_service, sso_service
from astrbot.portal.services.sso.base import SsoProviderError, SsoTemporaryError

router = APIRouter(prefix="/auth", tags=["Portal Auth"])


# ---------------------------------------------------------------------------
# Request/response bodies. Kept local to this file: the portal module has no
# shared schemas module in this assignment's file ownership, and these
# shapes are only ever consumed by these five endpoints.
# ---------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    email: str
    password: str
    display_name: str | None = None


class LoginRequest(BaseModel):
    email: str
    password: str


class SsoLinkConfirmRequest(BaseModel):
    """Body for ``POST /auth/sso/{provider}/link/confirm``.

    Deliberately the only field: the account being linked TO is always
    ``get_current_user``'s resolved identity (never a body field -- see
    module docstring), and the provider/subject being linked were already
    fixed server-side back when the callback resolved
    ``confirmation_token`` in the first place.
    """

    model_config = ConfigDict(extra="ignore")

    confirmation_token: str


class InviteRedeemRequest(BaseModel):
    """Body for ``POST /auth/invite/redeem``.

    No ``email`` field, by design -- see
    ``invite_service.redeem_invite``'s own "Email binding" docstring
    section: which account this token activates is bound entirely to the
    token itself, and letting the invitee supply/change an email as part of
    redemption would be exactly the account-linking-by-attacker-supplied-
    identity takeover shape rule 5 warns about.
    """

    model_config = ConfigDict(extra="ignore")

    token: str
    password: str
    display_name: str | None = None


def _user_payload(user: PortalUser) -> dict:
    """The subset of a ``PortalUser`` row that is safe to hand back to its
    own owner. Never includes ``password_hash``."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
    }


# ---------------------------------------------------------------------------
# Request metadata helpers.
# ---------------------------------------------------------------------------


def _client_ip(request: Request) -> str | None:
    """Best-effort caller IP for rate limiting/session bookkeeping.

    Prefers the first hop of ``X-Forwarded-For`` (the common case when
    AstrBot sits behind a reverse proxy) and falls back to the direct peer
    address. This is used only for throttling keys and the
    ``PortalSession.ip`` audit column -- never for any authorization
    decision -- so a spoofed header at worst pollutes rate-limit bucketing,
    it never grants access.
    """
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        first = forwarded.split(",", 1)[0].strip()
        if first:
            return first
    if request.client is not None:
        return request.client.host
    return None


def _is_secure_request(request: Request) -> bool:
    """Whether this request arrived over a transport that justifies the
    ``Secure`` cookie flag: true HTTPS, or a reverse proxy that says so via
    ``X-Forwarded-Proto``. Only plain, unproxied HTTP is treated as
    insecure (per this module's cookie contract: "Secure when not plain
    http")."""
    if request.url.scheme == "https":
        return True
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    return forwarded_proto.split(",", 1)[0].strip().lower() == "https"


# ---------------------------------------------------------------------------
# Cookie helpers. Three cookies, three different shapes -- see module
# docstring of astrbot.portal.config for why these names/paths are load-bearing
# (never collide with the core dashboard's astrbot_dashboard_jwt).
# ---------------------------------------------------------------------------


def _cookie_security(request: Request) -> tuple[Literal["lax", "none"], bool]:
    """The ``(samesite, secure)`` pair every portal cookie in this module must
    be written (and deleted) with.

    Two deployment shapes, two answers -- see
    ``PortalSettings.cross_origin_cookies`` for the full rationale:

    * **Same-origin** (default, incl. the recommended reverse-proxy setup):
      ``SameSite=Lax``, ``Secure`` whenever the transport justifies it.
    * **Cross-origin SPA**: ``SameSite=None``, which browsers only honour
      together with ``Secure`` -- so ``Secure`` is forced on regardless of
      what this particular request's transport looks like. ``PortalSettings``
      already refuses to start in this mode unless every allowed origin is
      HTTPS, so forcing the flag here cannot produce a cookie that a
      correctly-configured deployment then drops; what it *does* prevent is a
      misconfigured proxy (one that forgets ``X-Forwarded-Proto``) silently
      emitting ``SameSite=None`` without ``Secure``, which every browser
      rejects outright and which would look like "login does nothing".

    Centralised because these two flags previously appeared as hardcoded
    literals at eleven separate call sites across this file, including the
    SSO callback paths -- any one of them missed during a change is a session
    that silently stops working in exactly one flow.
    """
    if get_portal_settings().cross_origin_cookies:
        return "none", True
    return "lax", _is_secure_request(request)


def _set_auth_cookies(
    request: Request,
    response: Response,
    token_pair: session_service.TokenPair,
    csrf_token: str,
) -> None:
    settings = get_portal_settings()
    samesite, secure = _cookie_security(request)

    # Access JWT: httpOnly, readable on every path, short-lived.
    response.set_cookie(
        settings.jwt_cookie_name,
        token_pair.access_token,
        max_age=settings.access_token_ttl_seconds,
        httponly=True,
        samesite=samesite,
        secure=secure,
        path="/",
    )
    # Refresh token: httpOnly, Path scoped to ONLY the refresh endpoint so it
    # is never sent (and never exposed to XSS reading other request/response
    # traffic) on any other route.
    response.set_cookie(
        settings.refresh_cookie_name,
        token_pair.refresh_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=True,
        samesite=samesite,
        secure=secure,
        path=settings.refresh_cookie_path,
    )
    # CSRF double-submit token: deliberately NOT httpOnly -- the SPA's own JS
    # must be able to read it and echo it back in the X-Portal-CSRF-Token
    # header on every unsafe request (see auth.dependencies.require_csrf).
    # Lives as long as the refresh token so it stays valid across the whole
    # session, not just one access-token lifetime.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=False,
        samesite=samesite,
        secure=secure,
        path="/",
    )


def _clear_auth_cookies(request: Request, response: Response) -> None:
    settings = get_portal_settings()
    samesite, secure = _cookie_security(request)
    # delete_cookie must be called with the exact same path the cookie was
    # set with, or browsers will not remove it.
    response.delete_cookie(
        settings.jwt_cookie_name,
        path="/",
        httponly=True,
        samesite=samesite,
        secure=secure,
    )
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        httponly=True,
        samesite=samesite,
        secure=secure,
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        httponly=False,
        samesite=samesite,
        secure=secure,
    )


def _current_session_id_best_effort(request: Request) -> str | None:
    """Best-effort ``session_id`` extraction from the access JWT cookie, for
    logout's session-revocation step.

    Deliberately never raises: at logout time the access token may already
    be expired/missing (e.g. the tab was left open past its 15-minute
    lifetime), in which case there is nothing to revoke by this path and
    logout falls back to just clearing cookies. This is intentionally
    lenient, unlike ``get_current_user`` (used everywhere a route needs an
    authenticated identity, where a hard 401 is the correct behavior).

    Reads the session_id from the access JWT cookie (``Path=/``) rather
    than the refresh cookie on purpose: the refresh cookie is Path-scoped
    to ONLY the ``/auth/refresh`` route (see
    ``PortalSettings.refresh_cookie_path``) and is therefore never delivered
    to this ``/auth/logout`` route by the browser at all.
    """
    settings = get_portal_settings()
    token = request.cookies.get(settings.jwt_cookie_name)
    if not token:
        return None
    secret = getattr(request.app.state, "portal_jwt_secret", None)
    if not isinstance(secret, str) or not secret:
        return None
    try:
        payload = verify_access_token(token, secret)
    except TokenError:
        return None
    return payload.session_id


# ---------------------------------------------------------------------------
# SSO helpers (Phase 5).
# ---------------------------------------------------------------------------

_SSO_NEXT_COOKIE_NAME = "astrbot_portal_sso_next"
"""Carries a validated post-login redirect target from ``/sso/{provider}/start``
to ``/sso/{provider}/callback`` across the OAuth round trip. NOT part of
``sso_service``'s own state (that module's ``state`` value is an opaque
string it generates and verifies itself; there is no way to smuggle
application data through it without editing a file this assignment does not
own), so this is a small, separate, single-purpose cookie of this file's
own. Never trusted blindly on read: validated again at write time (`/start`)
AND at read time (`/callback`) by :func:`_validate_redirect_target` -- see
that function's docstring for why "validated once, trusted forever" is not
good enough here."""

_DEFAULT_SAFE_REDIRECT_PREFIX = "/portal"
"""Fallback allowlisted destination space when the SPA's serving base cannot
be derived from configuration -- the path this SPA used to be served at, back
when AstrBot hosted it itself. Only reachable if ``sso_redirect_base_url`` is
unset, which independently disables SSO entirely (see
``sso_service._require_provider_enabled``), so in practice this is a
belt-and-braces default rather than a live code path."""


def _safe_redirect_prefix() -> str:
    """The allowlisted destination space for a post-SSO-login redirect: the
    path component of the SPA's own public base URL.

    Derived rather than hardcoded because the portal SPA is now a separate
    project with its own deployment, and it is no longer necessarily served
    at ``/portal``. A deployment that serves it at the origin root yields
    ``""`` here, which correctly means "any path on the SPA's origin is a
    legitimate destination" -- the open-redirect guards in
    :func:`_validate_redirect_target` (no scheme, no netloc, no
    scheme-relative ``//``) are what actually keep the browser on-origin, and
    they hold regardless of how long this prefix is.

    Single source of truth with the frontend: the SPA builds the ``next``
    value it sends here by prefixing its route path with its own Vite
    ``base`` (see ``src/api/config.ts``'s ``toBasePrefixedPath``). Point
    ``sso_redirect_base_url`` at the SPA's real public URL and the two agree
    by construction.
    """
    base_url = get_portal_settings().sso_redirect_base_url
    if not base_url:
        return _DEFAULT_SAFE_REDIRECT_PREFIX
    return urlsplit(base_url).path.rstrip("/")


def _validate_redirect_target(candidate: str | None) -> str | None:
    """Return ``candidate`` unchanged if it is safe to redirect the browser
    to after SSO login; ``None`` otherwise (callers must fall back to a
    fixed default, never to the raw unchecked value).

    This is the mitigation for the open-redirect surface an SSO callback
    otherwise creates: if a "come back here after login" destination is
    ever taken from a request parameter (or, as here, a cookie whose value
    was itself derived from one) and handed to a redirect response
    unchecked, an attacker can craft a link that authenticates the victim
    legitimately and then bounces their browser to an attacker-controlled
    site, which is often enough for a convincing phishing follow-up. The
    allowlist is the SPA's own serving base (see
    :func:`_safe_redirect_prefix`), not a regex trying to distinguish
    "looks like our domain" from "looks like an attacker's domain" -- the
    only endorsed destinations are pages that SPA itself serves, so anything
    else is simply out of scope, not a judgment call.
    """
    if not isinstance(candidate, str) or not candidate:
        return None
    # Must be an absolute path on this origin, always -- this is the check
    # that actually prevents an open redirect, and it holds even when the
    # prefix below is empty (SPA served at the origin root).
    if not candidate.startswith("/"):
        return None
    # A bare prefix check (``startswith("/portal")``) would also accept
    # "/portalx" or "/portal.evil.com" -- still same-origin paths (a
    # leading single "/" can never be read by a browser as anything but a
    # path on the current origin, so neither is actually an open-redirect
    # escape), but not real pages this SPA serves either. Require an exact
    # match or a genuine path boundary right after the prefix so the
    # allowlist means what it says.
    prefix = _safe_redirect_prefix()
    if prefix and candidate != prefix and not candidate.startswith(prefix + "/"):
        return None
    # Reject scheme-relative ("//evil.com/...") and backslash tricks some
    # browsers/proxies still normalize into a scheme-relative URL, plus raw
    # control characters that could be used to smuggle a second URL past a
    # naive prefix check.
    if candidate.startswith("//") or candidate.startswith("/\\"):
        return None
    if any(ch in candidate for ch in ("\\", "\r", "\n", "\t")):
        return None
    parsed = urlsplit(candidate)
    if parsed.scheme or parsed.netloc:
        return None
    return candidate


def _sso_callback_cookie_path(provider: str) -> str:
    """The one and only path the SSO state (and next-redirect) cookies are
    ever scoped to: this provider's own callback route, and nothing else.
    Computed identically wherever a cookie is set or read for these two
    purposes so the two can never drift apart -- mirrors
    ``sso_service._redirect_uri``'s own "single convention, both call
    sites" rationale."""
    return f"/api/v1/portal/auth/sso/{provider}/callback"


def _raise_for_sso_error(exc: Exception) -> NoReturn:
    """Translate every exception ``sso_service``/``sso.base`` document as
    able to propagate out of ``start_login``/``start_link``/
    ``complete_callback``/``confirm_link``/``unlink`` into an HTTP response,
    following ``sso_service.py``'s own recommended mapping verbatim (see its
    module docstring / ``notes_for_integrator`` -- that module owns the
    exception vocabulary and is the single source of truth for this
    mapping, not re-derived here).
    """
    if isinstance(exc, sso_service.SsoRateLimitedError):
        raise HTTPException(status_code=429, detail="请求过于频繁，请稍后再试") from exc
    if isinstance(
        exc,
        (
            sso_service.SsoDisabledError,
            sso_service.UnknownSsoProviderError,
            sso_service.SsoProviderNotImplementedError,
        ),
    ):
        # Deliberately one generic 404 for all three: "not configured",
        # "not a real provider id", and "no adapter yet" all mean the same
        # thing to an outside caller -- this login method is not available
        # -- and distinguishing them would only help someone probing for
        # which providers exist/are half-configured.
        raise HTTPException(status_code=404, detail="该登录方式当前不可用") from exc
    if isinstance(exc, sso_service.InvalidSsoStateError):
        # Acceptance criterion 3: a missing/unknown/expired/already-consumed
        # state must all collapse to this one generic, non-distinguishing
        # response -- exactly what InvalidSsoStateError already guarantees
        # by construction (see its own docstring).
        raise HTTPException(status_code=400, detail="登录状态无效或已过期，请重新登录") from exc
    if isinstance(exc, sso_service.SsoAccountNotProvisionedError):
        raise HTTPException(
            status_code=403,
            detail="该身份尚未关联任何账号，请先使用管理员邀请注册，或登录后在账号设置中绑定该登录方式",
        ) from exc
    if isinstance(exc, sso_service.SsoIdentityAlreadyLinkedError):
        raise HTTPException(status_code=409, detail="该登录方式已绑定到另一个账号") from exc
    if isinstance(exc, sso_service.SsoLinkConfirmationError):
        raise HTTPException(status_code=400, detail="绑定确认无效或已过期，请重新发起绑定") from exc
    if isinstance(exc, sso_service.SsoIdentityNotLinkedError):
        raise HTTPException(status_code=404, detail="尚未绑定该登录方式") from exc
    if isinstance(exc, sso_service.SsoCannotRemoveLastCredentialError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, SsoTemporaryError):
        raise HTTPException(status_code=503, detail="登录服务暂时不可用，请稍后再试") from exc
    if isinstance(exc, SsoProviderError):
        # Covers SsoDiscoveryError/SsoIdentityVerificationError and any
        # other adapter-layer verification failure -- never echo the
        # underlying adapter exception's message (it may contain internal
        # endpoint URLs/response fragments); a flat generic message only.
        raise HTTPException(status_code=400, detail="身份验证失败") from exc
    raise HTTPException(status_code=400, detail="请求失败") from exc


def _raise_for_invite_error(exc: Exception) -> NoReturn:
    """Translate every ``invite_service`` exception into HTTP, following
    that module's own module-docstring "Recommended HTTP status mapping"
    verbatim -- it is the single source of truth for this vocabulary."""
    if isinstance(exc, invite_service.InviteRateLimitedError):
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    if isinstance(exc, invite_service.InviteInvalidError):
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            invite_service.InviteAlreadyConsumedError,
            invite_service.InviteRevokedError,
            invite_service.InviteExpiredError,
        ),
    ):
        # Acceptance criterion 2: a replayed (already-consumed) token -> 410.
        # Revoked/expired share the same status for the same reason
        # invite_service.py's own docstring gives: all three describe a
        # link that used to be valid at this URL and no longer is.
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    if isinstance(exc, invite_service.InviteEmailMismatchError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, invite_service.InvitePasswordPolicyError):
        # Safe to surface the ValueError-derived message verbatim -- mirrors
        # how PasswordPolicyError is already handled elsewhere in this
        # module's own register-flow error translation.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, invite_service.InviteRedemptionConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/register")
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Self-service registration, gated by ``PortalSettings.self_registration_open``.

    Disabled (403) once an admin flips that setting off -- Phase 5a is
    expected to replace self-registration with admin-provisioned invites,
    at which point this endpoint stops being the only way in.
    """
    settings = get_portal_settings()
    if not settings.self_registration_open:
        raise HTTPException(status_code=403, detail="自助注册当前已关闭")

    ip = _client_ip(request)
    throttle = get_login_throttle()
    decision = await throttle.check(ip=ip, account=payload.email)
    if not decision.allowed:
        raise HTTPException(
            status_code=429, detail="请求过于频繁，请稍后再试"
        )

    try:
        user = await session_service.create_user(
            db_session,
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except session_service.InvalidEmailError as exc:
        raise HTTPException(status_code=400, detail="邮箱格式不正确") from exc
    except session_service.PasswordPolicyError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except session_service.EmailAlreadyRegisteredError as exc:
        raise HTTPException(status_code=409, detail="该邮箱已被注册") from exc

    token_pair = await session_service.issue_token_pair(
        db_session,
        user_id=user.user_id,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    csrf_token = generate_csrf_token()
    _set_auth_cookies(request, response, token_pair, csrf_token)
    logger.info(f"[portal] new user registered: {user.user_id}")
    return _user_payload(user)


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Local-auth login.

    Failure responses are deliberately identical (same status code, same
    generic message) whether the email does not exist, the password is
    wrong, or the account is disabled -- never reveal which case applied
    (classic user-enumeration defense; see
    ``session_service.AuthenticationError``'s docstring).
    """
    ip = _client_ip(request)
    throttle = get_login_throttle()
    decision = await throttle.check(ip=ip, account=payload.email)
    if not decision.allowed:
        # Deliberately the same generic reason regardless of which
        # dimension (ip/account, bucket/lockout) tripped.
        raise HTTPException(
            status_code=429, detail="尝试次数过多，请稍后再试"
        )

    try:
        # authenticate_user already applies the fixed failure delay
        # internally (see its docstring) -- do not call
        # apply_failure_delay() again here, that would double the sleep.
        user = await session_service.authenticate_user(
            db_session, email=payload.email, password=payload.password
        )
    except session_service.AuthenticationError as exc:
        throttle.record_failure(ip=ip, account=payload.email)
        raise HTTPException(status_code=401, detail="邮箱或密码错误") from exc
    except session_service.InvalidEmailError as exc:
        # Malformed email can never match any account; same generic
        # message and same failure bookkeeping as a wrong password, so a
        # malformed-vs-valid-but-wrong email is not a distinguishable
        # signal either.
        throttle.record_failure(ip=ip, account=payload.email)
        await apply_failure_delay()
        raise HTTPException(status_code=401, detail="邮箱或密码错误") from exc

    throttle.record_success(ip=ip, account=payload.email)

    token_pair = await session_service.issue_token_pair(
        db_session,
        user_id=user.user_id,
        user_agent=request.headers.get("user-agent"),
        ip=ip,
    )
    csrf_token = generate_csrf_token()
    _set_auth_cookies(request, response, token_pair, csrf_token)
    return _user_payload(user)


@router.post("/refresh")
async def refresh(
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Rotate the refresh token and mint a fresh access token.

    The refresh cookie is Path-scoped to exactly this endpoint (see
    ``PortalSettings.refresh_cookie_path``), so it is only ever presented
    here. Replay of an already-consumed refresh token revokes its entire
    token family and forces the caller back to ``/login`` (all three
    cookies are cleared).
    """
    settings = get_portal_settings()
    ip = _client_ip(request)
    throttle = get_login_throttle()
    decision = await throttle.check(ip=ip, account=None)
    if not decision.allowed:
        raise HTTPException(
            status_code=429, detail="请求过于频繁，请稍后再试"
        )

    refresh_token = request.cookies.get(settings.refresh_cookie_name)
    if not refresh_token:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期")

    try:
        token_pair = await session_service.rotate_refresh(
            db_session,
            refresh_token=refresh_token,
            user_agent=request.headers.get("user-agent"),
            ip=ip,
        )
    except session_service.InvalidRefreshTokenError as exc:
        # Covers both plain invalidity and RefreshReplayDetectedError (a
        # subclass) -- either way the caller must fully re-authenticate, so
        # clear every cookie rather than leaving a half-valid state behind.
        _clear_auth_cookies(request, response)
        if isinstance(exc, session_service.RefreshReplayDetectedError):
            logger.warning(
                "[portal] refresh token replay forced a full session teardown"
            )
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc

    csrf_token = generate_csrf_token()
    _set_auth_cookies(request, response, token_pair, csrf_token)
    return {
        "success": True,
        "access_expires_at": token_pair.access_expires_at.isoformat(),
    }


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_portal_db_session),
    _csrf: None = Depends(require_csrf),
) -> dict:
    """Revoke the current session (idempotent) and clear every portal cookie.

    Requires the CSRF double-submit match (this is precisely the
    already-authenticated, state-changing case ``require_csrf``'s docstring
    calls out) -- login/register/refresh are exempt: at that point in the
    flow there is either no prior session to protect (login/register) or
    the refresh cookie's own Path-scoping + SameSite=Lax already blocks
    cross-site delivery for a POST.

    Revokes by ``session_id`` (read from the access JWT cookie -- see
    ``_current_session_id_best_effort``), NOT via
    ``session_service.logout(refresh_token=...)``: the refresh cookie's
    Path is scoped to only ``/auth/refresh``, so it is never present on
    this request in the first place.
    """
    session_id = _current_session_id_best_effort(request)
    if session_id:
        await session_service.revoke_session(db_session, session_id=session_id)
    _clear_auth_cookies(request, response)
    return {"success": True}


@router.get("/me")
async def me(user: PortalUser = Depends(get_current_user)) -> dict:
    """Return the caller's own profile, resolved solely from the verified
    ``astrbot_portal_jwt`` cookie -- never from any client-supplied id."""
    return _user_payload(user)


# ---------------------------------------------------------------------------
# SSO routes (Phase 5). Availability/start/callback are public (no prior
# session exists yet for a login attempt); link/link-confirm/unlink/linked
# require an authenticated caller, and every state-changing one of those
# four also requires the CSRF double-submit match, same as every other
# authenticated write route in this module.
# ---------------------------------------------------------------------------


@router.get("/sso/providers")
async def sso_providers() -> dict:
    """Which SSO providers are currently enabled -- for rendering login
    buttons. Serializes ``get_sso_provider_availability()`` verbatim
    (booleans only); never a raw ``PortalSettings`` dump, which would leak
    ``oidc_client_secret``/``wecom_app_secret``/``feishu_app_secret``. No
    internal endpoint URLs are exposed either -- only the public
    ``{provider}/start`` path shape a client can already derive from this
    router's own prefix."""
    return {"providers": get_sso_provider_availability(get_portal_settings())}


@router.get("/sso/linked")
async def sso_linked(
    user: PortalUser = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Which providers are currently linked to the caller's own account --
    not explicitly enumerated in this phase's route list, but a direct,
    read-only, already-implemented service call
    (``sso_service.list_linked_providers``) an account-settings "linked
    identities" page needs in order to render its unlink buttons/self-
    lockout warning sensibly; adding it here rather than leaving the SPA to
    infer link state from ``/me`` (which does not carry it). Declared here,
    before the ``/sso/{provider}/...`` parametrized routes below, matching
    this package's own stated convention (see ``admin_routes.py``'s module
    docstring) of static sub-paths first -- there is no actual collision
    (every parametrized route below has two path segments after ``/sso/``,
    this one has exactly one), but keeping the ordering consistent avoids
    ever having to reason about it again if a route is added later.
    """
    providers = await sso_service.list_linked_providers(db_session, user_id=user.user_id)
    return {"linked_providers": providers}


@router.get("/sso/{provider}/start")
async def sso_start(
    provider: str,
    request: Request,
    next: str | None = None,  # noqa: A002 - query param name, mirrors the public API shape
) -> Response:
    """Begin a LOGIN-purpose SSO flow: redirect the browser to the IdP.

    ``next`` (optional): where to send the browser after a successful
    login, validated against :func:`_validate_redirect_target` -- an
    invalid/absent value silently falls back to the SPA's own root
    (``/portal``), it never fails the request. The validated value (if any)
    is stashed in a short-lived, httpOnly cookie scoped to exactly this
    provider's callback path -- ``state`` itself is an opaque value entirely
    owned/verified by ``sso_service`` with no room to carry extra
    application data, so this is a small, separate cookie for that one
    purpose (see ``_SSO_NEXT_COOKIE_NAME``'s own docstring).
    """
    ip = _client_ip(request)
    try:
        result = await sso_service.start_login(provider_id=provider, ip=ip)
    except (
        sso_service.SsoDisabledError,
        sso_service.UnknownSsoProviderError,
        sso_service.SsoProviderNotImplementedError,
        sso_service.SsoRateLimitedError,
        SsoProviderError,
    ) as exc:
        _raise_for_sso_error(exc)

    # Same (samesite, secure) policy as the session cookies, and for the same
    # reason rather than merely for consistency: the SPA's own
    # `OAuthCallbackPage` reads these back by calling `/sso/{provider}/callback`
    # over XHR (see that route's docstring -- the IdP's redirect_uri points at
    # the SPA page, not at this API). In a cross-origin deployment that is a
    # cross-site XHR, which `SameSite=Lax` would not attach the state cookie
    # to -- every SSO login would fail as "state expired".
    samesite, secure = _cookie_security(request)
    callback_path = _sso_callback_cookie_path(provider)
    redirect_response = Response(status_code=302, headers={"Location": result.authorize_url})
    redirect_response.set_cookie(
        sso_service.SSO_STATE_COOKIE_NAME,
        result.state,
        max_age=result.state_cookie_max_age_seconds,
        httponly=True,
        samesite=samesite,
        secure=secure,
        path=callback_path,
    )
    safe_next = _validate_redirect_target(next)
    if safe_next is not None:
        redirect_response.set_cookie(
            _SSO_NEXT_COOKIE_NAME,
            safe_next,
            max_age=result.state_cookie_max_age_seconds,
            httponly=True,
            samesite=samesite,
            secure=secure,
            path=callback_path,
        )
    return redirect_response


@router.get("/sso/{provider}/callback")
async def sso_callback(
    provider: str,
    request: Request,
    response: Response,
    code: str | None = None,
    state: str | None = None,
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Complete an SSO flow (LOGIN or LINK -- which one is resolved
    entirely server-side by ``sso_service`` from the pending flow it stored
    at ``/start`` time, never taken from anything on this request).

    Returns JSON rather than issuing an HTTP redirect itself: the SPA's own
    ``OAuthCallbackPage`` calls this endpoint (it is what the IdP's
    ``redirect_uri`` actually points at, per ``sso_service._redirect_uri``'s
    convention -- an SPA page, not this API route) and performs any further
    client-side navigation itself. This sidesteps the open-redirect surface
    for the common case entirely rather than mitigating it after the fact;
    the one place this file still hands back a redirect *target* (the
    validated ``redirect_to`` field below, sourced from the ``next`` cookie
    set at ``/start``) is still validated against the same allowlist before
    being echoed, never trusted just because it came from our own cookie.

    A missing ``code``/``state`` is rejected identically to every other
    "this didn't work" case below (acceptance criterion 3): no
    distinguishing response for "you sent nothing" vs. "you sent an
    unrecognized/expired/already-used state".
    """
    ip = _client_ip(request)
    state_cookie_value = request.cookies.get(sso_service.SSO_STATE_COOKIE_NAME)
    # Cookies are single-purpose and single-use regardless of outcome below
    # -- always clear both on the way out so a retried/refreshed callback
    # request can never present a stale state/next pairing a second time.
    callback_path = _sso_callback_cookie_path(provider)
    # Must match exactly what `sso_start` wrote them with, or the browser
    # keeps the originals and a stale state/next pairing survives to be
    # replayed.
    samesite, secure = _cookie_security(request)

    def _clear_sso_cookies() -> None:
        response.delete_cookie(
            sso_service.SSO_STATE_COOKIE_NAME,
            path=callback_path,
            httponly=True,
            samesite=samesite,
            secure=secure,
        )
        response.delete_cookie(
            _SSO_NEXT_COOKIE_NAME,
            path=callback_path,
            httponly=True,
            samesite=samesite,
            secure=secure,
        )

    if not code or not state:
        _clear_sso_cookies()
        raise HTTPException(status_code=400, detail="登录状态无效或已过期，请重新登录")

    next_cookie = request.cookies.get(_SSO_NEXT_COOKIE_NAME)

    try:
        result = await sso_service.complete_callback(
            db_session,
            provider_id=provider,
            state=state,
            state_cookie_value=state_cookie_value,
            code=code,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except (sso_service.SsoServiceError, SsoProviderError) as exc:
        _clear_sso_cookies()
        _raise_for_sso_error(exc)

    _clear_sso_cookies()

    if isinstance(result, sso_service.SsoLoginResult):
        csrf_token = generate_csrf_token()
        _set_auth_cookies(request, response, result.token_pair, csrf_token)
        # Falls back to the SPA's own root, which is "" when it is served at
        # the origin root -- normalized to "/" so the SPA always receives a
        # navigable path rather than an empty string.
        redirect_to = _validate_redirect_target(next_cookie) or (
            _safe_redirect_prefix() or "/"
        )
        return {
            "result": "login",
            "user": _user_payload(result.user),
            "redirect_to": redirect_to,
        }

    # SsoLinkPendingResult: no binding has been written yet -- the SPA must
    # still call /sso/{provider}/link/confirm while authenticated as the
    # same account that started the flow (see confirm_link's own docstring
    # for why that ownership check is what makes this unforgeable).
    return {
        "result": "link_pending",
        "provider": result.provider,
        "email": result.email,
        "display_name": result.display_name,
        "confirmation_token": result.confirmation_token,
        "expires_at": result.expires_at.isoformat(),
    }


@router.post("/sso/{provider}/link")
async def sso_link_start(
    provider: str,
    request: Request,
    response: Response,
    user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict:
    """Begin a LINK-purpose SSO flow for the currently-authenticated
    account. ``POST`` (not a plain link/redirect like ``/start``) precisely
    because this is an authenticated, state-initiating action that must go
    through the CSRF double-submit check -- the SPA is expected to call
    this via ``fetch``/XHR and then navigate the browser to the returned
    ``authorize_url`` itself, exactly mirroring how ``/start`` sets its own
    state cookie, just on a JSON response instead of a redirect response.
    """
    ip = _client_ip(request)
    try:
        result = await sso_service.start_link(
            provider_id=provider, user_id=user.user_id, ip=ip
        )
    except (sso_service.SsoServiceError, SsoProviderError) as exc:
        _raise_for_sso_error(exc)

    link_samesite, link_secure = _cookie_security(request)
    response.set_cookie(
        sso_service.SSO_STATE_COOKIE_NAME,
        result.state,
        max_age=result.state_cookie_max_age_seconds,
        httponly=True,
        samesite=link_samesite,
        secure=link_secure,
        path=_sso_callback_cookie_path(provider),
    )
    return {"authorize_url": result.authorize_url}


@router.post("/sso/{provider}/link/confirm")
async def sso_link_confirm(
    provider: str,
    payload: SsoLinkConfirmRequest,
    user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """The explicit second-confirmation step (acceptance criterion 4): the
    callback above never wrote a binding for a LINK-purpose flow, only an
    opaque ``confirmation_token``. This is the one call that actually binds
    a ``PortalOAuthIdentity`` row to ``user.user_id`` -- the caller's own
    verified identity from ``get_current_user``, never a client-supplied
    field, which is exactly what makes ``confirmation_token`` alone
    (without also being logged in as the right account) useless to an
    attacker. Unconfirmed (token never presented, or presented by/for the
    wrong account) means no binding ever takes effect -- there is no
    fallback path that links on callback completion alone.
    """
    try:
        identity = await sso_service.confirm_link(
            db_session,
            user_id=user.user_id,
            confirmation_token=payload.confirmation_token,
        )
    except sso_service.SsoServiceError as exc:
        _raise_for_sso_error(exc)
    return {"success": True, "provider": identity.provider}


@router.post("/sso/{provider}/unlink")
async def sso_unlink(
    provider: str,
    user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
    db_session: AsyncSession = Depends(get_portal_db_session),
) -> dict:
    """Remove a linked identity. The self-lockout guard
    (``SsoCannotRemoveLastCredentialError`` -> 409) lives entirely inside
    ``sso_service.unlink`` -- this route adds no additional check, it just
    surfaces that refusal as the recommended status code."""
    try:
        await sso_service.unlink(db_session, user_id=user.user_id, provider_id=provider)
    except sso_service.SsoServiceError as exc:
        _raise_for_sso_error(exc)
    return {"success": True}


# ---------------------------------------------------------------------------
# Invite redemption (Phase 5a).
# ---------------------------------------------------------------------------


@router.post("/invite/redeem")
async def redeem_invite(
    payload: InviteRedeemRequest,
    request: Request,
    response: Response,
) -> dict:
    """Redeem a one-time admin-issued invite link: set a password, activate
    the pre-created account, and log the invitee straight in.

    Rate-limited entirely inside ``invite_service.redeem_invite`` itself
    (keyed by the invite_id parsed from the token, plus IP) -- this route
    deliberately does NOT call ``get_login_throttle().check(...)`` again
    with the same account key first, per that function's own
    ``notes_for_integrator``: doing so would double-consume the same
    bucket. A replayed (already-consumed) token returns 410 (acceptance
    criterion 2); an unknown/malformed token and a wrong secret for a real
    one both return the same generic 401 (see
    ``invite_service.InviteInvalidError``'s own docstring for why that
    collapsing is deliberate, not an oversight).
    """
    ip = _client_ip(request)
    try:
        result = await invite_service.redeem_invite(
            token=payload.token,
            password=payload.password,
            display_name=payload.display_name,
            ip=ip,
            user_agent=request.headers.get("user-agent"),
        )
    except invite_service.InviteServiceError as exc:
        _raise_for_invite_error(exc)

    csrf_token = generate_csrf_token()
    _set_auth_cookies(request, response, result.token_pair, csrf_token)
    logger.info(f"[portal] invite redeemed, new active user: {result.user_id}")
    return {
        "user_id": result.user_id,
        "email": result.email,
        "display_name": result.display_name,
        "role": "user",
        "status": "active",
    }


__all__ = ["router"]
