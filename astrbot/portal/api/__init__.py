"""HTTP route layer for the AstrBot Portal module.

Every router defined under this package is aggregated by ``router.py`` into
a single ``portal_router`` (prefix ``/portal``) and mounted into
``astrbot.dashboard.api.router.build_api_router`` (which already applies the
``/api/v1`` prefix), so the final public paths are ``/api/v1/portal/*``.

There is no static-asset router here: the end-user SPA is a separate project
(https://github.com/AstrBotDevs/astrbot-portal) with its own deployment, and
AstrBot serves only the API.
"""

from __future__ import annotations
