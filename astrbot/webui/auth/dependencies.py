"""FastAPI dependencies enforcing the webui module's own auth system.

Only ever reads the webui module's own cookies/JWT and the webui database
(via ``astrbot.webui.db.engine.get_webui_db_session``). This file must
never import ``astrbot.dashboard.*`` or the capability-wall-forbidden core
services (rule 5) -- a webui end-user JWT verified here must never be
mistakable for a core dashboard admin session's ``astrbot_dashboard_jwt``
(see ``astrbot.dashboard.api.auth.require_dashboard_user`` for what that
looks like on the core side), and vice versa: different secret, different
claim shape (``sub=uuid`` here vs ``username=str`` there), different
cookie name.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from astrbot import logger
from astrbot.webui.auth.security import (
    TokenError,
    constant_time_compare,
    verify_access_token,
)
from astrbot.webui.config import get_webui_settings
from astrbot.webui.db.engine import get_webui_db_session
from astrbot.webui.db.models import WebUiUser

SAFE_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
"""CSRF-safe HTTP methods: exempted from the double-submit check below."""


def _get_webui_jwt_secret(request: Request) -> str:
    """Fetch the webui module's own JWT signing secret from app state.

    The secret is generated/persisted once via
    ``astrbot.webui.config.get_webui_jwt_secret``, which itself needs
    ``SharedPreferences`` -- only importable from ``core_facade.py``
    (rule 5). Whichever code wires the webui router into the FastAPI app
    at startup is expected to call that function once and stash the
    result on ``request.app.state.webui_jwt_secret``, mirroring exactly
    how the core dashboard already does this for its own JWT secret
    (``request.app.state.jwt_secret``, see
    ``astrbot.dashboard.api.auth.require_dashboard_user``). This function
    itself never talks to ``SharedPreferences``.
    """
    secret = getattr(request.app.state, "webui_jwt_secret", None)
    if not isinstance(secret, str) or not secret:
        logger.error(
            "[webui] request.app.state.webui_jwt_secret is not set; the "
            "webui auth dependency cannot verify any access token until "
            "startup wiring calls get_webui_jwt_secret() and stores the "
            "result there."
        )
        raise HTTPException(status_code=401, detail="webui 认证未初始化")
    return secret


async def get_current_user(
    request: Request,
    db_session: AsyncSession = Depends(get_webui_db_session),
) -> WebUiUser:
    """Resolve the caller's ``WebUiUser`` from the ``astrbot_webui_jwt`` cookie.

    401 on any failure to authenticate (missing cookie, expired/malformed
    token, unknown ``user_id``); 403 if the resolved user's ``status`` is
    not ``'active'`` (e.g. ``'invited'`` -- has not finished accepting an
    invite/setting a password yet -- or ``'disabled'``).

    Never trusts any client-supplied ``user_id``/``username`` field: the
    only identity source is the verified JWT's ``sub`` claim, per rule 4
    ("Never trust the client for identity") and the plan's load-bearing
    insight that per-user isolation in ``ChatService`` works precisely
    because ``username``/``creator`` is a server-derived, never
    client-supplied string.
    """
    token = request.cookies.get(get_webui_settings().jwt_cookie_name)
    if not token:
        raise HTTPException(status_code=401, detail="未登录")

    secret = _get_webui_jwt_secret(request)
    try:
        payload = verify_access_token(token, secret)
    except TokenError as exc:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期") from exc

    user = await db_session.get(WebUiUser, payload.user_id)
    if user is None:
        raise HTTPException(status_code=401, detail="登录状态无效或已过期")

    if user.status != "active":
        raise HTTPException(status_code=403, detail="账号不可用")

    # Convenience for downstream handlers/logging -- never read as an
    # identity source by anything, only ever written here from the
    # already-verified JWT.
    request.state.webui_user_id = user.user_id
    request.state.webui_session_id = payload.session_id
    return user


def require_csrf(request: Request) -> None:
    """Double-submit CSRF enforcement for every unsafe HTTP method.

    The non-httpOnly ``astrbot_webui_csrf`` cookie (readable by the SPA's
    own JS, set at login time) must byte-for-byte equal the
    ``X-Webui-CSRF-Token`` request header on every state-changing request.
    GET/HEAD/OPTIONS/TRACE are exempt (the standard CSRF-safe-method
    allowlist); every other verb -- POST/PUT/PATCH/DELETE -- is enforced
    unconditionally.

    This dependency does not itself check authentication -- route owners
    must combine it with ``get_current_user`` (e.g.
    ``Depends(get_current_user)``, ``Depends(require_csrf)`` on the same
    route) wherever both are required, per the plan's "double-submit"
    CSRF contract (section 3.1) applied to "all write operations".
    """
    if request.method.upper() in SAFE_HTTP_METHODS:
        return

    settings = get_webui_settings()
    cookie_token = request.cookies.get(settings.csrf_cookie_name)
    header_token = request.headers.get(settings.csrf_header_name)
    if not constant_time_compare(cookie_token, header_token):
        raise HTTPException(status_code=403, detail="CSRF 校验失败")
