"""Operator-only admin HTTP routes for the AstrBot Portal module (Phase 3
KB-visibility CRUD; Phase 4 adds Agent Bundle CRUD; Phase 5 adds invite
issuance/listing/revocation and portal-user list/disable/enable below, same
conventions -- every new route is gated by the same
``Depends(require_system_scope)``, no exception).

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

from datetime import datetime, timezone
from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from astrbot import logger
from astrbot.dashboard.api.auth import AuthContext, require_system_scope
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalSession, PortalUser
from astrbot.portal.services import (
    agent_bundle_service,
    invite_service,
    kb_visibility_service,
)
from astrbot.portal.services.agent_bundle_service import (
    AgentBundleServiceError,
    KbNotVisibleError,
    PersonaNotFoundError,
    ProviderNotFoundError,
    TooManyKbIdsError,
)
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


class UpsertAgentBundleRequest(BaseModel):
    """Body for ``POST /admin/agent-bundles`` (create) and
    ``PUT /admin/agent-bundles/{bundle_id}`` (full-replace update).

    This is the ONE place in the whole module an operator picks a persona
    and a provider from the real core inventory -- exactly the privilege
    boundary this phase's assignment draws: operators name providers, end
    users name bundles (see ``agent_routes.py``'s module docstring). Every
    field is validated against live core state by
    ``agent_bundle_service._validate_bundle_fields`` before anything is
    written -- an operator can never save a bundle referencing a persona/
    provider that does not currently exist, or a ``kb_id`` that is not
    currently portal-visible.

    ``persona_id``/``chat_provider_id`` being ``None`` is itself meaningful
    ("this bundle does not touch that dimension" -- see
    ``PortalAgentBundle``'s own field docstrings), so the update route is a
    full replace (PUT semantics): every mutable field must be resupplied on
    every update, there is no partial-patch/``None``-means-unchanged
    contract here (see ``agent_bundle_service.admin_update``'s own
    docstring).
    """

    model_config = ConfigDict(extra="ignore")

    name: str = Field(min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=1000)
    persona_id: str | None = Field(default=None, max_length=100)
    chat_provider_id: str | None = Field(default=None, max_length=100)
    kb_ids: list[str] = Field(default_factory=list, max_length=100)
    enabled: bool = True
    sort_order: int = 0
    is_default: bool = False


class SetAgentBundleEnabledRequest(BaseModel):
    """Body for ``PATCH /admin/agent-bundles/{bundle_id}/enabled``."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool


class CreateInviteRequest(BaseModel):
    """Body for ``POST /admin/invites``.

    ``display_name`` is optional -- ``invite_service.admin_create_invite``
    derives a placeholder from the email's local-part when omitted (the
    invitee can personalize it during redemption). There is deliberately no
    ``role``/``status`` field here: every invite-created account starts as
    an ordinary ``role='user'`` account (see ``invite_service.py``'s own
    ``admin_create_invite``), matching this phase's scope of "provision an
    end user", not "provision an operator".
    """

    model_config = ConfigDict(extra="ignore")

    email: str = Field(min_length=3, max_length=255)
    display_name: str | None = Field(default=None, max_length=100)


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
# Error translation for agent-bundle CRUD. ``core_facade.
# ResourceNotFoundError`` (bundle_id doesn't exist) stays a plain 404 for the
# same "operator, not an existence-oracle concern" reason as the KB section
# above. The four ``agent_bundle_service`` validation exceptions below are
# all admin-CRUD-time input-validation failures -- distinct from the 404
# used for "bundle_id doesn't exist" -- mapped to 400 per this phase's
# upstream contract (agent_bundle_service.py's own notes_for_integrator).
# ---------------------------------------------------------------------------


def _raise_for_admin_bundle_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(
        exc,
        (PersonaNotFoundError, ProviderNotFoundError, KbNotVisibleError, TooManyKbIdsError),
    ):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, AgentBundleServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Error translation for invite CRUD (Phase 5a). Mapping follows
# ``invite_service.py``'s own module docstring, "Recommended HTTP status
# mapping" section, verbatim -- that module is the single source of truth
# for this vocabulary since it owns the exception hierarchy.
# ---------------------------------------------------------------------------


def _raise_for_admin_invite_error(exc: Exception) -> NoReturn:
    if isinstance(exc, invite_service.InviteNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(exc, invite_service.InviteAlreadyConsumedError):
        # Same 410 as the public redemption-replay case -- it is the same
        # exception type describing the same underlying fact ("this invite
        # was already used"), just reached from the admin revoke path
        # instead of the redeem path. See invite_service.py's own mapping
        # table: it is the single source of truth this file defers to.
        raise HTTPException(status_code=410, detail=str(exc)) from exc
    if isinstance(exc, invite_service.InviteEmailAlreadyRegisteredError):
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


# ---------------------------------------------------------------------------
# Agent Bundle CRUD (Phase 4). Matches the shape and conventions of the
# KB-visibility CRUD above rather than inventing a second style: static
# sub-paths declared before parametrized ones, ``PUT`` for full-replace
# update, ``PATCH`` for the narrow enable/disable toggle, ``granted_by``/
# ``updated_by`` sourced from ``AuthContext.username`` -- never from the
# request body -- exactly like ``granted_by`` above.
# ---------------------------------------------------------------------------


@router.get("/agent-bundles")
async def list_agent_bundles(
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Every ``PortalAgentBundle`` row (enabled or not), joined with live
    reference-health status -- including dangling rows whose ``persona_id``/
    ``chat_provider_id`` no longer resolves in core, or whose ``kb_ids``
    include one or more entries no longer ``PortalKbVisibility.enabled``.
    Unlike the end-user ``GET /agent-bundles`` route, this is the operator's
    full-inventory view (including ``persona_id``/``chat_provider_id``/
    ``kb_ids``) precisely so a broken/stale entry can be seen and fixed, not
    silently hidden -- see ``agent_bundle_service._admin_entry``'s own
    docstring for the exact shape.
    """
    entries = await agent_bundle_service.admin_list_all(request)
    return {"agent_bundles": entries}


@router.get("/agent-bundles/{bundle_id}")
async def get_agent_bundle(
    bundle_id: str,
    request: Request,
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """A single bundle row (enabled or not) with live status, for an admin
    detail/edit view. 404 if ``bundle_id`` does not exist."""
    try:
        return await agent_bundle_service.admin_get(request, bundle_id)
    except ResourceNotFoundError as exc:
        _raise_for_admin_bundle_error(exc)


@router.post("/agent-bundles")
async def create_agent_bundle(
    payload: UpsertAgentBundleRequest,
    request: Request,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Create a new bundle. The operator picks ``persona_id``/
    ``chat_provider_id`` from the real core inventory and ``kb_ids`` from
    the ``PortalKbVisibility`` whitelist here -- this is exactly the
    privilege boundary this phase's assignment draws (operators name
    providers, end users name bundles). 400 if ``persona_id``/
    ``chat_provider_id`` does not currently exist in core, if any ``kb_id``
    is not currently portal-visible, or if ``kb_ids`` exceeds
    ``PortalSettings.max_kb_ids_per_bundle``. ``updated_by`` is sourced from
    ``AuthContext.username`` -- the authenticated operator's own identity
    from the verified scope check -- never from the request body.
    """
    try:
        return await agent_bundle_service.admin_create(
            request,
            name=payload.name,
            description=payload.description,
            persona_id=payload.persona_id,
            chat_provider_id=payload.chat_provider_id,
            kb_ids=payload.kb_ids,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
            is_default=payload.is_default,
            updated_by=auth.username,
        )
    except (ResourceNotFoundError, AgentBundleServiceError) as exc:
        _raise_for_admin_bundle_error(exc)


@router.put("/agent-bundles/{bundle_id}")
async def update_agent_bundle(
    bundle_id: str,
    payload: UpsertAgentBundleRequest,
    request: Request,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Full-replace update of an existing bundle (PUT semantics -- see
    ``UpsertAgentBundleRequest``'s own docstring: every mutable field must
    be resupplied on every update, since ``persona_id``/``chat_provider_id``
    being ``None`` is itself meaningful and there is no spare sentinel left
    for "leave unchanged"). Same validation as create runs again here,
    against the NEW proposed values. 404 if ``bundle_id`` does not exist;
    400 for the same validation failures as create.
    """
    try:
        return await agent_bundle_service.admin_update(
            request,
            bundle_id,
            name=payload.name,
            description=payload.description,
            persona_id=payload.persona_id,
            chat_provider_id=payload.chat_provider_id,
            kb_ids=payload.kb_ids,
            enabled=payload.enabled,
            sort_order=payload.sort_order,
            is_default=payload.is_default,
            updated_by=auth.username,
        )
    except (ResourceNotFoundError, AgentBundleServiceError) as exc:
        _raise_for_admin_bundle_error(exc)


@router.patch("/agent-bundles/{bundle_id}/enabled")
async def set_agent_bundle_enabled(
    bundle_id: str,
    payload: SetAgentBundleEnabledRequest,
    request: Request,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Enable or disable a bundle without touching any other field.

    Never deletes the row (mirrors ``PortalKbVisibility.enabled``'s own
    audit-trail rationale, and there is no ``admin_delete`` for
    ``PortalAgentBundle`` -- see ``agent_bundle_service.py``'s own gaps
    section). Disabling a bundle a session already has bound leaves that
    session's already-applied persona/provider/KB state untouched; it
    simply drops out of the end-user ``GET /agent-bundles`` list and can no
    longer be freshly selected. 404 if ``bundle_id`` does not exist.
    """
    try:
        return await agent_bundle_service.admin_set_enabled(
            request, bundle_id, payload.enabled, updated_by=auth.username
        )
    except ResourceNotFoundError as exc:
        _raise_for_admin_bundle_error(exc)


# ---------------------------------------------------------------------------
# Invite CRUD (Phase 5a). All authorization/state logic lives in
# ``invite_service.py``, which -- exactly like ``kb_visibility_service.py``/
# ``agent_bundle_service.py`` before it -- performs zero authorization
# checks of its own and trusts this router's ``Depends(require_system_scope)``
# completely. ``created_by``/``revoked_by`` are always sourced from
# ``AuthContext.username`` -- never from the request body -- same
# convention as ``granted_by``/``updated_by`` above.
# ---------------------------------------------------------------------------


@router.post("/invites")
async def create_invite(
    payload: CreateInviteRequest,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Issue a one-time invite link for ``payload.email``.

    **The response's ``invite_token``/``invite_link`` fields are the ONLY
    time the plaintext token is ever returned** -- see
    ``invite_service.admin_create_invite``'s own docstring: from the next
    request onward only its PBKDF2 hash exists anywhere, and there is no
    way to recover it again, by this endpoint or anyone else. There is no
    mail infrastructure in this repository (no ``smtplib``/``aiosmtplib``
    usage exists anywhere) -- the operator UI calling this endpoint MUST
    surface this value and make explicit that delivering it to the invitee
    (chat, ticket, however they reach them) is the operator's own
    responsibility, not something this call performs.

    409 if ``email`` already belongs to an ``'active'``/``'disabled'``
    account (:class:`invite_service.InviteEmailAlreadyRegisteredError`); a
    still-``'invited'`` address is instead silently reissued a fresh invite
    against the same pre-created user (see that function's own docstring).
    400 for a malformed email.
    """
    try:
        entry, plaintext_token = await invite_service.admin_create_invite(
            email=payload.email,
            created_by=auth.username,
            display_name=payload.display_name,
        )
    except invite_service.InviteServiceError as exc:
        _raise_for_admin_invite_error(exc)
    invite_link = f"/portal/invite/accept?token={plaintext_token}"
    return {
        **entry,
        "invite_token": plaintext_token,
        "invite_link": invite_link,
    }


@router.get("/invites")
async def list_invites(
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Every invite ever issued, most recent first, each with its computed
    ``status`` (``'pending'``/``'consumed'``/``'revoked'``/``'expired'``).
    **Never** includes a plaintext token or its hash -- see
    ``invite_service._admin_entry``'s own docstring; the plaintext is only
    ever available once, from :func:`create_invite`'s own response."""
    entries = await invite_service.admin_list_invites()
    return {"invites": entries}


@router.post("/invites/{invite_id}/revoke")
async def revoke_invite(
    invite_id: str,
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Cancel an outstanding (not yet redeemed) invite. 404 if no such
    invite exists; 410 if it was already redeemed (revoking an already-
    activated account is a separate, out-of-scope "disable the user"
    operation -- see :func:`disable_portal_user` below -- not something
    "revoking the invite" could retroactively undo). Idempotent for an
    already-revoked invite."""
    try:
        return await invite_service.admin_revoke_invite(invite_id, revoked_by=auth.username)
    except invite_service.InviteServiceError as exc:
        _raise_for_admin_invite_error(exc)


# ---------------------------------------------------------------------------
# Portal-user list/disable/enable (Phase 5). Unlike the invite/kb-visibility/
# agent-bundle CRUD above, there is no dedicated service module for this --
# this assignment's ownership is exactly ``admin_routes.py``/``auth_routes.py``,
# and neither ``session_service.py`` nor ``astrbot/portal/db/models.py`` is
# this assignment's to modify, so the handful of direct ``PortalUser``/
# ``PortalSession`` queries below live inline rather than inventing a new
# service module out of scope. Every query goes through
# ``get_portal_db_session`` exactly like every other route in this package.
# ---------------------------------------------------------------------------


def _admin_user_entry(user: PortalUser) -> dict[str, Any]:
    """Operator-facing shape of one ``PortalUser`` row. Never includes
    ``password_hash`` -- mirrors ``auth_routes.py``'s own ``_user_payload``
    but additionally exposes ``auth_method``/timestamps, which only an
    operator (not the user's own ``/auth/me``) needs to see."""
    return {
        "user_id": user.user_id,
        "email": user.email,
        "display_name": user.display_name,
        "role": user.role,
        "status": user.status,
        "auth_method": user.auth_method,
        "created_at": user.created_at.isoformat(),
        "updated_at": user.updated_at.isoformat(),
    }


async def _revoke_all_sessions_for_user(db_session: AsyncSession, *, user_id: str) -> None:
    """Revoke every not-yet-revoked ``PortalSession`` row belonging to
    ``user_id``, across *every* token family.

    ``session_service.revoke_family`` (the only bulk-revoke primitive that
    module exposes) only ever revokes the ONE family behind whichever
    refresh token triggered it -- correct for replay detection, but not
    enough here: a single user may hold several independently-issued
    families at once (multiple devices/browsers, each login mints a fresh
    ``family_id`` -- see ``session_service.issue_token_pair``'s own
    docstring). Disabling an account must kill all of them, so this queries
    by ``user_id`` directly instead, mirroring ``revoke_family``'s own
    conditional-update-over-not-yet-revoked-rows shape.
    """
    now = datetime.now(timezone.utc)
    result = await db_session.execute(
        select(PortalSession).where(
            col(PortalSession.user_id) == user_id,
            col(PortalSession.revoked_at).is_(None),
        )
    )
    rows = result.scalars().all()
    for row in rows:
        row.revoked_at = now
        db_session.add(row)
    if rows:
        await db_session.commit()


@router.get("/users")
async def list_portal_users(
    db_session: AsyncSession = Depends(get_portal_db_session),
    _auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Every ``PortalUser`` row (any ``status``), most recently created
    first -- the operator's full-inventory view, including ``auth_method``/
    ``status`` so a stuck ``'invited'`` account or a disabled one is
    visible, not silently hidden."""
    result = await db_session.execute(
        select(PortalUser).order_by(col(PortalUser.created_at).desc())
    )
    users = result.scalars().all()
    return {"users": [_admin_user_entry(user) for user in users]}


@router.post("/users/{user_id}/disable")
async def disable_portal_user(
    user_id: str,
    db_session: AsyncSession = Depends(get_portal_db_session),
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Disable a portal end-user account and make that **actually** cut off
    access, not just flip a flag nothing else checks.

    Two independent effects, both required:

    1. ``PortalUser.status = 'disabled'``. This alone is already enough to
       stop any *future* request that resolves an identity via
       ``astrbot.portal.auth.dependencies.get_current_user`` -- that
       dependency re-reads the ``PortalUser`` row from the database on
       every single call (never trusts only the JWT payload) and 403s on
       any non-``'active'`` status. So an already-issued, still-unexpired
       access token stops being useful the very next time it is presented
       to any real endpoint.
    2. Revoking **every** ``PortalSession`` row for this user (see
       :func:`_revoke_all_sessions_for_user`). This is the part a flag
       alone would NOT cover: ``session_service.rotate_refresh`` never
       looks at ``PortalUser.status`` at all -- it only inspects the
       ``PortalSession`` row's own ``revoked_at``/``expires_at``/hash. Without
       this step, a disabled user could keep calling ``/auth/refresh``
       indefinitely, minting fresh access tokens forever (each one
       individually useless against ``get_current_user``, but the refresh
       family itself would stay alive and immediately usable again the
       moment -- if ever -- the account is re-enabled, which is not the
       "cut off" semantics "disable" is supposed to mean). Revoking every
       session closes that gap outright: a disabled user cannot refresh at
       all afterward, full stop.

    404 if no such user exists. Idempotent if already disabled (sessions
    are still (re-)swept every call, which is harmless -- there is nothing
    left un-revoked after the first call).
    """
    user = await db_session.get(PortalUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status != "disabled":
        user.status = "disabled"
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
    await _revoke_all_sessions_for_user(db_session, user_id=user_id)
    logger.info(f"[portal] admin {auth.username!r} disabled user {user_id}")
    return _admin_user_entry(user)


@router.post("/users/{user_id}/enable")
async def enable_portal_user(
    user_id: str,
    db_session: AsyncSession = Depends(get_portal_db_session),
    auth: AuthContext = Depends(require_system_scope),
) -> dict[str, Any]:
    """Re-enable a previously-disabled account (``status`` back to
    ``'active'``). Does not, and cannot, restore any session revoked by
    :func:`disable_portal_user` -- the user simply logs in again normally.

    409 if the account's current status is ``'invited'`` -- that is a
    fundamentally different resting state (no password has ever been set;
    see ``PortalInvite``'s own docstring) that this endpoint does not
    "fix"; the correct remedy there is re-issuing (or waiting out) an
    invite via :func:`create_invite`, not toggling this flag. 404 if no
    such user exists. Idempotent if already active.
    """
    user = await db_session.get(PortalUser, user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.status == "invited":
        raise HTTPException(
            status_code=409,
            detail="Cannot enable an account that has not accepted its invite yet",
        )
    if user.status != "active":
        user.status = "active"
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
    logger.info(f"[portal] admin {auth.username!r} re-enabled user {user_id}")
    return _admin_user_entry(user)


__all__ = ["router"]
