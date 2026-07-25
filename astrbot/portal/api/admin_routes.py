"""Operator-only admin HTTP routes for the AstrBot Portal module (Phase 3).

Every handler here is gated by ``Depends(require_system_scope)`` imported
**verbatim** from ``astrbot.dashboard.api.auth`` -- the core dashboard's own
admin scope check (``require_scope(request, "system")``). This is a
deliberate reuse, not a reimplementation: any valid operator dashboard JWT
(or a system-scoped API key) carries scopes ``['*']``, so this gate means
"a real operator", exactly matching this module's need to let an operator
curate what end users may see -- see design/portal-multiuser-plan.md
section 4, checklist item 5.

This is emphatically **not** the portal's own end-user auth
(``astrbot.portal.auth.dependencies.get_current_user``). The two token
systems are independent by design and share no verification path:

* An end-user portal JWT is signed with the portal module's own secret
  (``app.state.portal_jwt_secret``, generated/persisted separately -- see
  ``astrbot.portal.config.get_portal_jwt_secret``), carries a ``sub=<uuid>``
  claim, and is read from the ``astrbot_portal_jwt`` cookie only. It is
  never read by ``require_system_scope`` -- that dependency looks at the
  ``Authorization: Bearer`` header (or, for a system-scoped API key, an
  ``ApiKey ...``/``X-API-Key`` credential) or the *dashboard's* own
  ``DASHBOARD_JWT_COOKIE_NAME`` cookie, decoded with the *dashboard's* own
  ``request.app.state.jwt_secret`` -- a completely different signing key.
  A portal end-user JWT presented as a dashboard bearer token fails
  ``jwt.decode`` outright (wrong secret, and even if it somehow verified,
  its payload has no ``username`` claim -- see ``require_scope``'s
  ``payload.get("username")`` check) -- 401, never authorized here.
* Symmetrically, an operator's dashboard JWT is never accepted by
  ``get_current_user``: it isn't found under the ``astrbot_portal_jwt``
  cookie name, and even if a client copied it there, verifying it against
  the *portal's* secret (a different key entirely) fails, and its claim
  shape (``username``, not ``sub=<uuid>``) does not match what
  :func:`astrbot.portal.auth.security.verify_access_token` expects either.

So the two authentication paths are cryptographically and structurally
disjoint in both directions -- verified by reading both dependency chains,
not merely asserted.

No admin authorization check lives inside
:mod:`astrbot.portal.services.kb_visibility_service` itself (by design,
per that module's own docstring); every ``admin_*`` call below is the sole
enforcement point, which is exactly why every route in this file, with no
exception, must carry ``Depends(require_system_scope)``.

No CSRF dependency on any route below (unlike every write route in
``chat_routes.py``/``kb_routes.py``/``file_routes.py``, which all use
``Depends(require_csrf)``): CSRF here would protect against a browser
silently replaying the *dashboard's* own cookie-based session, and no
other dashboard admin route in this codebase (``astrbot/dashboard/api/
knowledge_bases.py`` and every other ``*_scope``-gated route checked)
applies one either -- dashboard admin auth is Bearer-JWT/API-key first,
with the cookie only a fallback (see ``require_scope``'s own body), and
introducing a CSRF contract found nowhere else in the dashboard's admin
surface would be a novel, inconsistent addition rather than matching
"surrounding style" (engineering rule 9). Flagged explicitly rather than
silently decided -- see this phase's report if a future audit wants CSRF
added dashboard-wide.

Reverse-proxy path/trust boundary -- READ THIS BEFORE DEPLOYING
-----------------------------------------------------------------------
This router is mounted with an ``/admin`` sub-prefix specifically so the
trust boundary is visible in the URL (every route below ends up at
``/api/v1/portal/admin/...``), but this file cannot, by itself, put that
path outside the publicly-reachable prefix: ``astrbot/portal/api/router.py``
(this module's own aggregate) is the only mount point this assignment owns,
and it is included into the core dashboard's ``build_api_router`` as a
single line, ``router.include_router(portal_router)`` -- a file this
assignment does NOT own and must not edit. The existing deployment
checklist (design/portal-multiuser-plan.md section 4, checklist item 6)
currently tells operators the reverse proxy may open the **entire**
``/api/v1/portal/*`` prefix to the public internet, alongside `/portal/*`
static assets, on the assumption that everything under it is already
end-user-facing and JWT/CSRF-gated for that purpose. That assumption is no
longer true now that `/api/v1/portal/admin/*` exists nested inside it.
**This is a deployment-guidance gap this phase introduces and must flag
loudly, not silently work around:** the reverse-proxy rule must be updated
to carve out `/api/v1/portal/admin/*` as an explicit exception -- blocked
from the public internet and restricted to internal/VPN access, joining
the existing internal-only list (`/api/v1/conversations*`,
`/api/v1/sessions*`, `/api/v1/session-groups*`, `/api/v1/config*`,
`/api/v1/api-keys*`, `/api/v1/plugins*`, `/api/v1/persona*`,
`/api/v1/subagents*`) -- rather than trusting `Depends(require_system_scope)`
alone as the only line of defense, which is precisely the single-layer
posture checklist item 6 exists to avoid for every *other* admin surface.
``require_system_scope`` remains correct and necessary as the
application-layer gate; it is not sufficient by itself per this module's
own defense-in-depth design, and network-layer enforcement of that same
principle must be extended here too.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from astrbot.dashboard.api.auth import AuthContext, require_system_scope
from astrbot.portal.services import kb_visibility_service
from astrbot.portal.services.core_facade import ResourceNotFoundError
from astrbot.portal.services.kb_visibility_service import KbAlreadyGrantedError

router = APIRouter(prefix="/admin", tags=["Portal Admin"])
"""Mounted by ``astrbot.portal.api.router.portal_router`` (prefix
``/portal``), itself mounted under ``/api/v1`` -- every path below ends up
public at ``/api/v1/portal/admin/...``. See module docstring's "reverse-
proxy path" section for why this nesting is a deployment-checklist gap
that must be flagged, not silently accepted."""


# ---------------------------------------------------------------------------
# Request models. extra="ignore", matching every other request model in
# this package.
# ---------------------------------------------------------------------------


class GrantKbVisibilityRequest(BaseModel):
    """Body for ``PUT /admin/kb-visibility/{kb_id}``."""

    model_config = ConfigDict(extra="ignore")

    display_name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


class UpdateKbVisibilityDisplayRequest(BaseModel):
    """Body for ``PATCH /admin/kb-visibility/{kb_id}``.

    Both fields are independently optional -- passing only one leaves the
    other untouched (``None`` means "leave unchanged"; an explicit empty
    string clears the field) -- see
    ``kb_visibility_service.admin_update_display``'s own docstring.
    """

    model_config = ConfigDict(extra="ignore")

    display_name: str | None = Field(default=None, max_length=100)
    description: str | None = Field(default=None, max_length=1000)


# ---------------------------------------------------------------------------
# Error translation. Unlike kb_routes.py, a plain 404 here is NOT an
# existence-oracle concern: the caller is an authenticated operator whose
# entire job on this page is to know which core kb_ids exist and which are
# already whitelisted -- there is no "yours vs. someone else's" distinction
# to hide from them, so ordinary REST-style 404/409 semantics apply.
# ---------------------------------------------------------------------------


def _raise_for_admin_kb_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(exc, KbAlreadyGrantedError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes. Static sub-paths (``/kb-visibility/core-kbs``) are declared
# BEFORE the ``/kb-visibility/{kb_id}`` parametrized routes so FastAPI's
# order-sensitive routing never mistakes the literal segment "core-kbs" for
# a ``kb_id`` path parameter.
# ---------------------------------------------------------------------------


@router.get("/kb-visibility")
async def list_kb_visibility(
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Every ``PortalKbVisibility`` row (enabled or not), joined with live
    core status -- including dangling rows whose ``kb_id`` no longer exists
    in core. Unlike the end-user ``GET /knowledge-bases`` route, this is
    the operator's full-inventory view precisely so a broken/stale entry
    can be seen and fixed, not silently hidden."""
    entries = await kb_visibility_service.admin_list_all(request)
    return {"kb_visibility": entries}


@router.get("/kb-visibility/core-kbs")
async def list_core_knowledge_bases(
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Every knowledge base currently loaded in core, as plain data -- the
    "browse what's available to grant" primitive for this admin page.
    Never exposed to any end-user route; this is the one place the
    operator's raw core KB inventory is intentionally surfaced."""
    kbs = await kb_visibility_service.admin_list_core_kbs(request)
    return {"core_kbs": kbs}


@router.put("/kb-visibility/{kb_id}")
async def grant_kb_visibility(
    kb_id: str,
    payload: GrantKbVisibilityRequest,
    request: Request,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Whitelist ``kb_id`` for end-user visibility, or re-enable/refresh an
    already-existing (possibly previously revoked) row for it.

    ``PUT`` (not ``POST``) because ``kb_id`` -- the caller-supplied,
    already-existing core resource identifier this row is keyed on -- is
    the URI itself, and granting an already-granted id is idempotent
    (re-caches ``kb_name`` from core's current live value and re-enables it
    rather than erroring). 404 if ``kb_id`` does not currently exist in
    core (an operator can never whitelist a KB that isn't there); 409 on
    the rare concurrent-grant race (see
    ``kb_visibility_service.KbAlreadyGrantedError``). ``granted_by`` is
    sourced from ``AuthContext.username`` -- the authenticated operator's
    own identity from the verified scope check -- never from the request
    body.
    """
    try:
        return await kb_visibility_service.admin_grant(
            request,
            kb_id,
            granted_by=auth.username,
            display_name=payload.display_name,
            description=payload.description,
        )
    except (ResourceNotFoundError, KbAlreadyGrantedError) as exc:
        _raise_for_admin_kb_error(exc)


@router.delete("/kb-visibility/{kb_id}")
async def revoke_kb_visibility(
    kb_id: str,
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Revoke end-user visibility for ``kb_id``.

    ``DELETE`` describes the operator-facing effect (this KB's grant is
    removed from the whitelist a user can select from) even though the
    underlying row is preserved with ``enabled=False`` rather than
    physically deleted -- an implementation detail kept for the audit
    trail (``granted_by``/``created_at`` history), documented on
    ``PortalKbVisibility.enabled`` and not something this route's HTTP
    verb needs to expose. 404 if no whitelist row exists for ``kb_id`` at
    all.
    """
    try:
        return await kb_visibility_service.admin_revoke(request, kb_id)
    except ResourceNotFoundError as exc:
        _raise_for_admin_kb_error(exc)


@router.patch("/kb-visibility/{kb_id}")
async def update_kb_visibility_display(
    kb_id: str,
    payload: UpdateKbVisibilityDisplayRequest,
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Update the consumer-facing ``display_name``/``description`` of an
    existing whitelist row without touching ``enabled``/``kb_name``. 404
    if no whitelist row exists for ``kb_id``."""
    try:
        return await kb_visibility_service.admin_update_display(
            request,
            kb_id,
            display_name=payload.display_name,
            description=payload.description,
        )
    except ResourceNotFoundError as exc:
        _raise_for_admin_kb_error(exc)


__all__ = ["router"]
