"""Auth routes for the AstrBot WebUI module: register/login/refresh/logout/me.

This router declares only the ``/auth`` sub-prefix; ``router.py`` aggregates
it (alongside the chat routes) under the module-wide ``/webui`` prefix,
which is itself aggregated under ``/api/v1`` by
``astrbot.dashboard.api.router.build_api_router``, so the final public
paths are:

    POST /api/v1/webui/auth/register
    POST /api/v1/webui/auth/login
    POST /api/v1/webui/auth/refresh
    POST /api/v1/webui/auth/logout
    GET  /api/v1/webui/auth/me

This file only ever talks to the webui module's own database
(``astrbot.webui.db.engine.get_webui_db_session``) via
``astrbot.webui.services.session_service`` -- it never imports
``astrbot.dashboard.*`` or any capability-wall-forbidden core service (rule
5): it is not the capability wall (``core_facade.py``), and none of these
endpoints need to reach ``ChatService``/``conversation_mgr``/etc. at all.

Identity is always derived from the verified ``astrbot_webui_jwt`` cookie
(see ``astrbot.webui.auth.dependencies.get_current_user``) -- never from any
client-supplied field, per rule 4.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from astrbot import logger
from astrbot.webui.auth.dependencies import get_current_user, require_csrf
from astrbot.webui.auth.rate_limit import apply_failure_delay, get_login_throttle
from astrbot.webui.auth.security import (
    TokenError,
    generate_csrf_token,
    verify_access_token,
)
from astrbot.webui.config import get_webui_settings
from astrbot.webui.db.engine import get_webui_db_session
from astrbot.webui.db.models import WebUiUser
from astrbot.webui.services import session_service

router = APIRouter(prefix="/auth", tags=["WebUI Auth"])


# ---------------------------------------------------------------------------
# Request/response bodies. Kept local to this file: the webui module has no
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


def _user_payload(user: WebUiUser) -> dict:
    """The subset of a ``WebUiUser`` row that is safe to hand back to its
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
    ``WebUiSession.ip`` audit column -- never for any authorization
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
# docstring of astrbot.webui.config for why these names/paths are load-bearing
# (never collide with the core dashboard's astrbot_dashboard_jwt).
# ---------------------------------------------------------------------------


def _set_auth_cookies(
    request: Request,
    response: Response,
    token_pair: session_service.TokenPair,
    csrf_token: str,
) -> None:
    settings = get_webui_settings()
    secure = _is_secure_request(request)

    # Access JWT: httpOnly, readable on every path, short-lived.
    response.set_cookie(
        settings.jwt_cookie_name,
        token_pair.access_token,
        max_age=settings.access_token_ttl_seconds,
        httponly=True,
        samesite="lax",
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
        samesite="lax",
        secure=secure,
        path=settings.refresh_cookie_path,
    )
    # CSRF double-submit token: deliberately NOT httpOnly -- the SPA's own JS
    # must be able to read it and echo it back in the X-Webui-CSRF-Token
    # header on every unsafe request (see auth.dependencies.require_csrf).
    # Lives as long as the refresh token so it stays valid across the whole
    # session, not just one access-token lifetime.
    response.set_cookie(
        settings.csrf_cookie_name,
        csrf_token,
        max_age=settings.refresh_token_ttl_seconds,
        httponly=False,
        samesite="lax",
        secure=secure,
        path="/",
    )


def _clear_auth_cookies(request: Request, response: Response) -> None:
    settings = get_webui_settings()
    secure = _is_secure_request(request)
    # delete_cookie must be called with the exact same path the cookie was
    # set with, or browsers will not remove it.
    response.delete_cookie(
        settings.jwt_cookie_name,
        path="/",
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    response.delete_cookie(
        settings.refresh_cookie_name,
        path=settings.refresh_cookie_path,
        httponly=True,
        samesite="lax",
        secure=secure,
    )
    response.delete_cookie(
        settings.csrf_cookie_name,
        path="/",
        httponly=False,
        samesite="lax",
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
    ``WebUiSettings.refresh_cookie_path``) and is therefore never delivered
    to this ``/auth/logout`` route by the browser at all.
    """
    settings = get_webui_settings()
    token = request.cookies.get(settings.jwt_cookie_name)
    if not token:
        return None
    secret = getattr(request.app.state, "webui_jwt_secret", None)
    if not isinstance(secret, str) or not secret:
        return None
    try:
        payload = verify_access_token(token, secret)
    except TokenError:
        return None
    return payload.session_id


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.post("/register")
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_webui_db_session),
) -> dict:
    """Self-service registration, gated by ``WebUiSettings.self_registration_open``.

    Disabled (403) once an admin flips that setting off -- Phase 5a is
    expected to replace self-registration with admin-provisioned invites,
    at which point this endpoint stops being the only way in.
    """
    settings = get_webui_settings()
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
    logger.info(f"[webui] new user registered: {user.user_id}")
    return _user_payload(user)


@router.post("/login")
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    db_session: AsyncSession = Depends(get_webui_db_session),
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
    db_session: AsyncSession = Depends(get_webui_db_session),
) -> dict:
    """Rotate the refresh token and mint a fresh access token.

    The refresh cookie is Path-scoped to exactly this endpoint (see
    ``WebUiSettings.refresh_cookie_path``), so it is only ever presented
    here. Replay of an already-consumed refresh token revokes its entire
    token family and forces the caller back to ``/login`` (all three
    cookies are cleared).
    """
    settings = get_webui_settings()
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
                "[webui] refresh token replay forced a full session teardown"
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
    db_session: AsyncSession = Depends(get_webui_db_session),
    _csrf: None = Depends(require_csrf),
) -> dict:
    """Revoke the current session (idempotent) and clear every webui cookie.

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
async def me(user: WebUiUser = Depends(get_current_user)) -> dict:
    """Return the caller's own profile, resolved solely from the verified
    ``astrbot_webui_jwt`` cookie -- never from any client-supplied id."""
    return _user_payload(user)


__all__ = ["router"]
