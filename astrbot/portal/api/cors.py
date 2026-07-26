"""CORS for the portal's end-user API, scoped to exactly that surface.

The portal SPA used to be served by this same process at ``/portal``, so
every call it made was same-origin and no CORS layer existed anywhere in
AstrBot. The SPA now ships as its own project, which an operator may deploy
either behind a reverse proxy onto one origin (recommended -- still
same-origin, nothing here activates) or on a separate origin, which is what
this module exists for.

Two properties matter more than convenience here, and both are why this is a
custom middleware rather than a bare ``app.add_middleware(CORSMiddleware,
...)``:

**1. It must not apply to the whole app.** Starlette's ``CORSMiddleware``
has no path filter, so installing it app-wide would hand the configured
origins credentialed access to every core dashboard endpoint as well. That is
not theoretical: ``astrbot/dashboard/api/auth.py`` accepts the operator's
``astrbot_dashboard_jwt`` **cookie** as proof of identity, so an operator with
a live dashboard session carries ambient authority that a permitted origin
could then drive from the browser. The end-user portal API is the only
surface that needs cross-origin access, so it is the only one that gets it.

**2. It must exclude the portal's own admin sub-tree.** ``/api/v1/portal/admin/*``
(invites, user management, KB visibility, agent bundles) is gated by the core
dashboard's ``require_system_scope``, i.e. by that same operator cookie -- not
by a portal end-user session. Those pages are served from the operator
dashboard, at the same origin as this API, so they never need CORS; granting
it would re-open exactly the hole point 1 closes, just one path deeper.

Wildcard origins are rejected at config-parse time rather than here -- see
``PortalSettings.cors_allowed_origins``.
"""

from __future__ import annotations

from fastapi import FastAPI
from starlette.middleware.cors import CORSMiddleware
from starlette.types import ASGIApp, Receive, Scope, Send

from astrbot.portal.config import PortalSettings

PORTAL_API_PREFIX = "/api/v1/portal"
"""Where ``build_api_router`` mounts ``portal_router`` (``/api/v1`` + ``/portal``)."""

PORTAL_ADMIN_API_PREFIX = "/api/v1/portal/admin"
"""Operator-only sub-tree of the above; deliberately excluded from CORS."""

CORS_ALLOW_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")

CORS_ALLOW_HEADERS = ("Content-Type", "Accept", "X-Portal-CSRF-Token")
"""``X-Portal-CSRF-Token`` is load-bearing: the SPA echoes the CSRF cookie
back in that header on every unsafe request (see
``astrbot/portal/auth/dependencies.py``'s ``require_csrf``). Omitting it from
the allowlist makes preflight fail on every write while reads keep working --
a confusing half-broken state rather than an obvious one."""


def _is_under(path: str, prefix: str) -> bool:
    """Whether ``path`` is ``prefix`` itself or something nested beneath it.

    Deliberately not a bare ``str.startswith``, which gets both boundaries
    wrong in opposite directions: it would pull ``/api/v1/portal-internal``
    into the portal surface, and it would push a hypothetical
    ``/api/v1/portal/administrators`` route out of it (matching the
    ``/api/v1/portal/admin`` exclusion and silently losing its CORS headers).
    Requiring a path separator keeps both exact.
    """
    return path == prefix or path.startswith(prefix + "/")


def is_cors_eligible_path(path: str) -> bool:
    """The end-user portal API, minus its operator-only admin sub-tree."""
    return _is_under(path, PORTAL_API_PREFIX) and not _is_under(
        path, PORTAL_ADMIN_API_PREFIX
    )


class PortalScopedCORSMiddleware:
    """Applies ``CORSMiddleware`` only to :func:`is_cors_eligible_path`.

    Everything else -- the operator dashboard API, the portal admin sub-tree,
    websockets -- reaches the inner app untouched, with no CORS headers added
    and no preflight handling, exactly as before this middleware existed.
    """

    def __init__(self, app: ASGIApp, *, allow_origins: tuple[str, ...]) -> None:
        self.app = app
        self.cors_app = CORSMiddleware(
            app,
            allow_origins=list(allow_origins),
            allow_credentials=True,
            allow_methods=list(CORS_ALLOW_METHODS),
            allow_headers=list(CORS_ALLOW_HEADERS),
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http" and is_cors_eligible_path(scope.get("path", "")):
            await self.cors_app(scope, receive, send)
            return
        await self.app(scope, receive, send)


def install_portal_cors(app: FastAPI, settings: PortalSettings) -> bool:
    """Install the CORS layer if this deployment is configured cross-origin.

    Returns whether it was installed, so the caller can log which shape the
    deployment is running in. An empty ``cors_allowed_origins`` means "same
    origin" and installs nothing at all -- not a middleware that allows
    nothing, which would still add ``Vary: Origin`` to every response and
    imply a cross-origin setup that isn't there.
    """
    if not settings.cors_allowed_origins:
        return False

    app.add_middleware(
        PortalScopedCORSMiddleware,
        allow_origins=settings.cors_allowed_origins,
    )
    return True


__all__ = [
    "PORTAL_ADMIN_API_PREFIX",
    "PORTAL_API_PREFIX",
    "PortalScopedCORSMiddleware",
    "install_portal_cors",
    "is_cors_eligible_path",
]
