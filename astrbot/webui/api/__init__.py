"""HTTP route layer for the AstrBot WebUI module.

Every router defined under this package is aggregated by ``router.py`` into
a single ``webui_router`` (prefix ``/webui``) and mounted into
``astrbot.dashboard.api.router.build_api_router`` (which already applies the
``/api/v1`` prefix), so the final public paths are ``/api/v1/webui/*``.

``static_router.py`` is the one exception: it serves the end-user SPA's
static assets and is mounted directly onto the FastAPI app (not under
``/api/v1``), at literal path ``/webui``.
"""

from __future__ import annotations
