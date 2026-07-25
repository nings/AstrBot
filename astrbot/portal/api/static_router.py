"""Static-asset router for the AstrBot Portal end-user SPA.

Serves the second, physically independent Vite build (the plan's
``dash-portal.html`` / ``portal-main.ts`` multi-page entry) at ``/portal`` and
its assets (``/portal/assets/...``). Mounted directly onto the FastAPI app --
NOT through ``build_api_router``/``portal_router`` -- because it must live
outside the ``/api/v1`` namespace entirely, at the bare ``/portal`` path the
plan's frontend routing expects.

Ordering is load-bearing: ``astrbot/dashboard/api/app.py`` already has a
greedy, catch-all static route (``GET /{static_path:path}``, see
``static_files.py``) registered last so it can serve the *operator*
dashboard's SPA and its own client-side routes. This router MUST be
``include_router``-ed on the FastAPI app strictly BEFORE that catch-all, or
every request under ``/portal/*`` would be swallowed by the operator
dashboard's static handler instead (whose ``resolve_static_file`` would
simply 404 on paths it doesn't recognize, since ``dashboard_static_folder``
holds the operator dist, not this one).

Dist directory resolution mirrors ``astrbot.core.utils.io.get_bundled_dashboard_dist_path``'s
convention (a "bundled" copy shipped inside the repo/package, with an
override under the data directory), but deliberately skips that helper's
version-compatibility/auto-download machinery -- that machinery lives in
``update_service.py``/``io.py`` and is out of this file's ownership and out
of Phase 1's scope. Phase 1 only needs: "does an ``index.html`` exist at
this path", nothing more elaborate.

Expected dist paths (see ``get_portal_dist_dir`` below):

* ``<astrbot data dir>/dist-portal/`` -- operator-provided override, checked
  first (same precedence idea as the operator dashboard's ``data/dist``).
* ``<repo>/astrbot/dashboard/dist-portal/`` -- the bundled build output,
  checked second. **This is the exact path the frontend build for the
  end-user SPA must emit to** (i.e. its Vite ``build.outDir``), so that a
  fresh checkout with no operator override works out of the box.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from astrbot.core.utils.astrbot_path import get_astrbot_data_path, get_astrbot_path

router = APIRouter(prefix="/portal", include_in_schema=False)

PORTAL_DIST_DIRNAME = "dist-portal"

# Vite names the emitted HTML document after its rollup input, and the
# end-user SPA's input is `portal.html` (see dashboard/vite.portal.config.ts),
# so this dist has no `index.html` at all. Every entry-document lookup below
# must go through this constant -- hardcoding "index.html" makes the dist
# unresolvable and 404s the whole SPA even when it is built and packaged
# correctly.
PORTAL_ENTRY_FILENAME = "portal.html"

NOT_FOUND_MESSAGE = (
    "404 Not found. The AstrBot Portal (end-user chat) static assets were "
    "not found. Build the portal frontend (npm run build:portal) and place its "
    f"output at data/{PORTAL_DIST_DIRNAME}/ or "
    f"astrbot/dashboard/{PORTAL_DIST_DIRNAME}/."
)


def get_bundled_portal_dist_path() -> Path:
    """The in-repo bundled build output location for the end-user SPA.

    Mirrors ``astrbot.core.utils.io.get_bundled_dashboard_dist_path``'s
    shape exactly (``<astrbot root>/astrbot/dashboard/<dirname>``), just
    with ``dist-portal`` instead of ``dist`` -- the two SPAs' build outputs
    live as sibling directories under the same package.
    """
    return Path(get_astrbot_path()) / "astrbot" / "dashboard" / PORTAL_DIST_DIRNAME


def get_portal_dist_dir() -> Path | None:
    """Resolve the directory to serve the end-user SPA's static assets from.

    Priority:

    1. ``<astrbot data dir>/dist-portal/`` -- lets an operator drop in a
       locally-built or hot-patched copy without touching the package
       install, same idea as the operator dashboard's ``data/dist``
       override.
    2. ``<repo>/astrbot/dashboard/dist-portal/`` -- the bundled build output
       shipped alongside the operator dashboard's own ``dist/``.

    Returns ``None`` if neither location has a ``portal.html`` -- callers
    must treat that as "the SPA is not built yet" and 404 gracefully
    (never crash the whole dashboard app over a missing frontend build).
    """
    data_dir_override = Path(get_astrbot_data_path()) / PORTAL_DIST_DIRNAME
    if (data_dir_override / PORTAL_ENTRY_FILENAME).is_file():
        return data_dir_override

    bundled = get_bundled_portal_dist_path()
    if (bundled / PORTAL_ENTRY_FILENAME).is_file():
        return bundled

    return None


def _not_found_response() -> PlainTextResponse:
    return PlainTextResponse(NOT_FOUND_MESSAGE, status_code=404)


def _looks_like_asset_request(requested_path: str) -> bool:
    """Heuristic: does the last path segment look like a filename (has a
    dot), as opposed to a client-side route (e.g. ``chat/123``)?

    Used only to decide whether a *missing* file should 404 (a genuinely
    missing JS/CSS/image asset must never silently become an HTML
    response) versus fall back to ``index.html`` (an unrecognized
    client-side route, which the SPA's own router -- hash-based per the
    plan, so this fallback is mostly a defensive backstop for any future
    history-mode routing -- should get a chance to handle).
    """
    last_segment = requested_path.rsplit("/", 1)[-1]
    return "." in last_segment


def _resolve_static_file(static_root: Path, requested_path: str) -> Path | None:
    """Safely resolve ``requested_path`` against ``static_root``.

    Rejects path traversal (``..``) and absolute-looking segments the same
    way ``astrbot.dashboard.services.static_file_service.StaticFileService``
    does, since this router is exposed to the same untrusted-client-path
    surface.
    """
    if not requested_path:
        return None
    path_parts = requested_path.replace("\\", "/").split("/")
    if requested_path.startswith(("/", "\\")) or ".." in path_parts:
        return None

    resolved_root = static_root.resolve()
    target_file = (resolved_root / requested_path).resolve()
    try:
        target_file.relative_to(resolved_root)
    except ValueError:
        return None

    if target_file.is_file():
        return target_file
    return None


async def serve_portal_index(request: Request):
    del request  # unused; index resolution takes no per-request input
    dist_dir = get_portal_dist_dir()
    if dist_dir is None:
        return _not_found_response()
    return FileResponse(dist_dir / PORTAL_ENTRY_FILENAME)


async def serve_portal_static_file(request: Request, static_path: str):
    # Defense in depth: this router is only ever mounted at /portal, so an
    # /api/... path should never reach here, but guard against it anyway in
    # case of a future mounting change.
    if request.url.path.startswith("/api"):
        raise HTTPException(status_code=404)

    dist_dir = get_portal_dist_dir()
    if dist_dir is None:
        return _not_found_response()

    file_path = _resolve_static_file(dist_dir, static_path)
    if file_path is not None:
        return FileResponse(file_path)

    # SPA history fallback: an unrecognized, non-asset-looking client-side
    # route falls back to the entry document so the SPA's own router can take
    # over. A path that looks like a real asset (has a file extension) and
    # still was not found is a genuine 404, never masked as HTML.
    if not _looks_like_asset_request(static_path):
        return FileResponse(dist_dir / PORTAL_ENTRY_FILENAME)

    return _not_found_response()


router.add_api_route("", serve_portal_index, methods=["GET"])
router.add_api_route("/", serve_portal_index, methods=["GET"])
router.add_api_route("/{static_path:path}", serve_portal_static_file, methods=["GET"])

__all__ = ["router", "get_portal_dist_dir", "get_bundled_portal_dist_path"]
