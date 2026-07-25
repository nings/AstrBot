"""Aggregate router for the AstrBot WebUI module's HTTP API surface.

Exports a single ``webui_router`` (prefix ``/webui``) that
``astrbot.dashboard.api.router.build_api_router`` mounts with one line
(``router.include_router(webui_router)``). ``build_api_router`` already
applies the ``/api/v1`` prefix on top, so every route declared here ends up
public at ``/api/v1/webui/*`` -- e.g. ``/webui/auth/login`` (declared in
``auth_routes.py`` under its own ``/webui/auth`` sub-prefix) becomes
``/api/v1/webui/auth/login``.

This is the *API* router only. The end-user SPA's static asset serving
(``/webui`` and ``/webui/assets/...``, no ``/api/v1`` prefix at all) is a
separate router -- ``static_router.py`` -- mounted directly onto the
FastAPI app, not through this aggregate.

``chat_routes.py`` is owned by a different, concurrently-developed
assignment in this same phase; it is imported here purely by module path
(``astrbot.webui.api.chat_routes``) and is expected to export a plain
``router: APIRouter`` symbol, exactly like every other router in this
package.
"""

from __future__ import annotations

from fastapi import APIRouter

from astrbot.webui.api.auth_routes import router as auth_router
from astrbot.webui.api.chat_routes import router as chat_router

webui_router = APIRouter(prefix="/webui", tags=["WebUI"])
webui_router.include_router(auth_router)
webui_router.include_router(chat_router)

__all__ = ["webui_router"]
