"""Aggregate router for the AstrBot Portal module's HTTP API surface.

Exports a single ``portal_router`` (prefix ``/portal``) that
``astrbot.dashboard.api.router.build_api_router`` mounts with one line
(``router.include_router(portal_router)``). ``build_api_router`` already
applies the ``/api/v1`` prefix on top, so every route declared here ends up
public at ``/api/v1/portal/*`` -- e.g. ``/portal/auth/login`` (declared in
``auth_routes.py`` under its own ``/portal/auth`` sub-prefix) becomes
``/api/v1/portal/auth/login``.

This is the *API* router only. The end-user SPA's static asset serving
(``/portal`` and ``/portal/assets/...``, no ``/api/v1`` prefix at all) is a
separate router -- ``static_router.py`` -- mounted directly onto the
FastAPI app, not through this aggregate.

``chat_routes.py`` and ``file_routes.py`` are each owned by a different,
concurrently-developed assignment in this same phase; they are imported
here purely by module path (``astrbot.portal.api.chat_routes`` /
``astrbot.portal.api.file_routes``) and are each expected to export a
plain ``router: APIRouter`` symbol, exactly like every other router in
this package. ``file_routes.py`` (Phase 2) is the sole sanctioned
replacement for core's own ``/api/v1/files/*`` endpoints, which must never
be exposed to the portal SPA (see design/portal-multiuser-plan.md section
4, checklist item 4).

``kb_routes.py`` (Phase 3, end-user KB visibility + session binding,
``Depends(get_current_user)``) and ``admin_routes.py`` (Phase 3, operator
KB-visibility CRUD, ``Depends(require_system_scope)`` imported verbatim
from ``astrbot.dashboard.api.auth``) are mounted the same way. See
``admin_routes.py``'s own module docstring for an explicit, loudly-flagged
deployment-checklist gap: mounting an operator-only router here means its
paths inherit the ``/api/v1/portal/*`` prefix design/portal-multiuser-
plan.md section 4 checklist item 6 currently tells operators to open to
the public internet, so that checklist's reverse-proxy rule must be
amended to carve out ``/api/v1/portal/admin/*`` as an internal/VPN-only
exception -- this file has no other mount point available to it (the
dashboard's own ``build_api_router`` includes only this single
``portal_router``, and is not owned by this assignment).
"""

from __future__ import annotations

from fastapi import APIRouter

from astrbot.portal.api.admin_routes import router as admin_router
from astrbot.portal.api.auth_routes import router as auth_router
from astrbot.portal.api.chat_routes import router as chat_router
from astrbot.portal.api.file_routes import router as file_router
from astrbot.portal.api.kb_routes import router as kb_router

portal_router = APIRouter(prefix="/portal", tags=["Portal"])
portal_router.include_router(auth_router)
portal_router.include_router(chat_router)
portal_router.include_router(file_router)
portal_router.include_router(kb_router)
portal_router.include_router(admin_router)

__all__ = ["portal_router"]
