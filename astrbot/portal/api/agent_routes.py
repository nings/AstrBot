"""End-user Agent Bundle selection HTTP routes for the AstrBot Portal module
(Phase 4).

Every handler here goes through :mod:`astrbot.portal.services.
agent_bundle_service` (the admin-curated bundle whitelist + persona/provider/
KB resolution layer) and never touches
:mod:`astrbot.portal.services.core_facade` (the capability wall) directly --
``agent_bundle_service`` already reaches the one core touchpoint it needs on
this route's behalf. This file must never import ``conv_mgr``/``sp``/
``persona_mgr``/``ProviderManager``/``ChatService`` or any other
capability-wall member directly (rule 5 of the module's engineering rules).

Every ``user_id`` used below comes from
:func:`astrbot.portal.auth.dependencies.get_current_user` -- never from a
request body, query string, or header (rule 4).

The headline acceptance criterion for this phase: a client-supplied
``provider_id``/``selected_provider``/``selected_model``/``persona_id`` in
the bind request body must be ignored, not honoured. This is enforced at
TWO independent layers, deliberately redundant:

1. :class:`BindAgentBundleRequest` below declares exactly one field,
   ``bundle_id``, with ``model_config = ConfigDict(extra="ignore")`` --
   pydantic silently drops any other key in the JSON body before this
   handler ever sees it. Verified, not assumed: see
   ``tests/test_portal_agent_bundle.py`` for a request built with an extra
   ``selected_provider``/``persona_id`` key, asserting the parsed model has
   no such attribute and that the resulting session binding matches the
   bundle's own persona/provider, never the injected value.
2. Even if some other request model change ever let a stray field through
   to this handler, :func:`agent_bundle_service.bind_session` itself only
   ever accepts a ``bundle_id`` positional argument -- there is no code
   path in this file that reads any other key out of ``payload`` and forwards
   it anywhere. A bundle is an opaque, server-resolved choice by construction
   (rule 6), not a set of client-suppliable overrides.

Bundle metadata exposed to end users (module-wide invariant)
-----------------------------------------------------------------------
Every response shape below is
:func:`agent_bundle_service._consumer_entry`'s narrow shape -- ``bundle_id``,
``name``, ``description``, ``sort_order``, ``is_default`` -- and nothing
else. In particular this file never surfaces ``persona_id``,
``chat_provider_id``, or ``kb_ids``: a bundle is an opaque choice by design
(this phase's own rule 6). Leaking its contents would both expose the
operator's provider/persona/KB inventory to end users and invite clients to
start naming providers directly, defeating the entire point of the bundle
abstraction. The operator-facing, full-detail shape lives only behind
``admin_routes.py``'s ``Depends(require_system_scope)`` gate.

403 vs 404 (engineering rule 8) -- DECISION: 404, always
-----------------------------------------------------------------------
Mirrors ``kb_routes.py``'s own "403 vs 404" section verbatim (see that
file's module docstring for the full rationale): every authorization/
existence failure --  "bundle_id does not exist", "bundle disabled",
"bundle currently dangling (stale persona/provider reference)", and
"session not owned by this caller" alike -- maps to a single,
indistinguishable HTTP 404 via ``core_facade.ResourceNotFoundError``. This
is ``agent_bundle_service.bind_session``'s own documented contract (see
that module's docstring, "404, never 403" section); this file does not
re-derive or override that decision, only translates the one exception
class it raises.

Unbinding: its own route, not a bind with an empty/null bundle id
-----------------------------------------------------------------------
Unlike Phase 3's KB binding (where an empty ``kb_ids`` list is itself a
perfectly ordinary bind request), a bundle selection is a single scalar
``bundle_id`` and there is no natural empty/null sentinel a client could
send through ``POST .../agent-bundle`` that would not first have to be
special-cased in :class:`BindAgentBundleRequest` (``bundle_id: str`` is
required and non-empty -- see that model's own docstring). Rather than
carve out a magic "empty string means unbind" value on the one write route
that must otherwise resolve to a real ``bundle_id`` looked up in the
database, a dedicated ``DELETE .../agent-bundle`` route calls
``agent_bundle_service.unbind_session`` directly -- the same function Phase
3's design would have reached for regardless, and the one explicitly
documented as unconditional/idempotent and NOT gated by the feature master
switch (see that function's own docstring). This is a deliberate departure
from Phase 3's own choice for the *same question*, justified by the
difference in shape between the two bindings (a list that has a natural
empty state vs. a single required identifier that does not), not an
inconsistency introduced without cause.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from astrbot.portal.auth.dependencies import get_current_user, require_csrf
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import agent_bundle_service
from astrbot.portal.services.agent_bundle_service import (
    AgentBundleSelectionDisabledError,
    AgentBundleServiceError,
)
from astrbot.portal.services.core_facade import CoreFacadeError, ResourceNotFoundError

router = APIRouter(tags=["Portal Agent Bundles"])
"""No sub-prefix of its own (matching ``kb_routes.router``'s own choice):
mounted directly by ``astrbot.portal.api.router.portal_router`` (prefix
``/portal``), so the routes below end up public at exactly
``/api/v1/portal/agent-bundles`` and
``/api/v1/portal/sessions/{session_id}/agent-bundle`` -- matching this
assignment's endpoint list verbatim. Declaring a ``/sessions/...`` path here
alongside ``kb_routes.py``'s and ``chat_routes.py``'s own ``/sessions/...``
paths is not a collision: FastAPI dispatches by full method+path, and the
three routers each only add ONE more static trailing segment
(``/knowledge-base``, ``/agent-bundle``, or nothing) after the shared
``/sessions/{session_id}`` prefix, so no two routes across the three files
are ever identical."""


# ---------------------------------------------------------------------------
# Request models. extra="ignore" so stray client fields (including,
# critically, ``provider_id``/``selected_provider``/``selected_model``/
# ``persona_id`` -- this phase's headline acceptance criterion) are silently
# dropped, matching every other request model in this module.
# ---------------------------------------------------------------------------


class BindAgentBundleRequest(BaseModel):
    """Body for ``POST /sessions/{session_id}/agent-bundle``.

    Carries a bundle id and NOTHING else that matters -- see module
    docstring's headline section. ``extra="ignore"`` means any additional
    key in the JSON body (``provider_id``, ``selected_provider``,
    ``selected_model``, ``persona_id``, or anything else) is dropped by
    pydantic before this model is even constructed; it never becomes an
    attribute on ``payload`` and is never read anywhere in this file or in
    ``agent_bundle_service.bind_session``, which only ever accepts a
    ``bundle_id`` argument.

    ``bundle_id`` is required and non-empty (``min_length=1``) -- there is
    no "empty string means unbind" special case on this route; see module
    docstring's "Unbinding" section for why that is instead its own route.
    """

    model_config = ConfigDict(extra="ignore")

    bundle_id: str = Field(min_length=1, max_length=36)


# ---------------------------------------------------------------------------
# Error translation. See module docstring's "403 vs 404" section: every
# authorization/existence failure maps to 404, never 403.
# ---------------------------------------------------------------------------


def _raise_for_bundle_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(exc, AgentBundleSelectionDisabledError):
        # A blanket operator feature-switch, not a per-resource
        # authorization decision -- see agent_bundle_service.py's own
        # docstring on this exception for why 403 (not 404) is fine here:
        # there is no existence to hide behind a whitelist-of-one switch.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, AgentBundleServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/agent-bundles")
async def list_agent_bundles(
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The enabled, currently-selectable bundles this user may pick from.

    Thin pass-through to ``agent_bundle_service.list_available``, which
    already guarantees: only ``enabled=True`` bundles are even considered,
    any bundle whose persona or provider reference has gone dangling is
    silently excluded (never shown as broken-but-selectable), and the shape
    returned per entry is the narrow, consumer-facing one (``bundle_id``,
    ``name``, ``description``, ``sort_order``, ``is_default``) -- never
    ``persona_id``, ``chat_provider_id``, ``kb_ids``, or anything else about
    the operator's provider/persona/KB inventory (module docstring's
    "Bundle metadata exposed to end users" section). This route performs no
    shaping of its own beyond wrapping the list in an object.
    """
    entries = await agent_bundle_service.list_available(current_user.user_id, request)
    return {"agent_bundles": entries}


@router.get("/sessions/{session_id}/agent-bundle")
async def get_session_agent_bundle(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The bundle currently bound to a session owned by this user, or
    ``{"agent_bundle": null}`` if nothing is currently selected.

    Ownership is checked by ``agent_bundle_service.get_session_binding`` (via
    its internal ``core_facade.get_session_agent_bindings`` call, which
    itself gates on the caller owning the session) before anything else
    happens; a missing session or one owned by someone else both raise
    ``core_facade.ResourceNotFoundError`` here, mapped to 404 (see module
    docstring's "403 vs 404" section). A session that owns no current bundle
    selection is NOT an error -- it returns ``None`` for ``agent_bundle``,
    exactly matching the operator's own default behaviour (rule 7: default
    closed, never "whatever the client last asked for").
    """
    try:
        entry = await agent_bundle_service.get_session_binding(
            current_user.user_id, request, session_id
        )
    except CoreFacadeError as exc:
        _raise_for_bundle_error(exc)
    return {"agent_bundle": entry}


@router.post("/sessions/{session_id}/agent-bundle")
async def bind_session_agent_bundle(
    session_id: str,
    payload: BindAgentBundleRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Bind exactly one bundle to a session owned by this user, by
    ``bundle_id``.

    ``payload.bundle_id`` is the ONLY value read out of the request body
    here, and it is the ONLY argument (besides the verified ``user_id`` and
    owned ``session_id``) forwarded to
    ``agent_bundle_service.bind_session`` -- see module docstring's headline
    section for the two independent layers (request-model field pruning +
    this handler never reading any other key) that make a client-supplied
    ``provider_id``/``selected_provider``/``selected_model``/``persona_id``
    a no-op, not merely "usually ignored".

    All resolution and validation happens in
    ``agent_bundle_service.bind_session`` before anything is written: the
    feature master switch, that the bundle exists and is enabled, and that
    it is currently selectable (persona/provider references, if set, still
    resolve in core right now). Any failure raises
    ``core_facade.ResourceNotFoundError`` (mapped to 404) or
    ``agent_bundle_service.AgentBundleSelectionDisabledError`` (mapped to
    403) -- see module docstring's "403 vs 404" section. The resolved
    persona/provider/KB bindings are never returned to the client; the
    response is the same narrow consumer-facing bundle shape as
    ``GET /agent-bundles``' entries.
    """
    try:
        return await agent_bundle_service.bind_session(
            current_user.user_id, request, session_id, payload.bundle_id
        )
    except (CoreFacadeError, AgentBundleServiceError) as exc:
        _raise_for_bundle_error(exc)


@router.delete("/sessions/{session_id}/agent-bundle")
async def unbind_session_agent_bundle(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Clear any bundle selection for a session owned by this user, restoring
    the operator's own defaults on every dimension (persona, provider, KB
    binding) -- see module docstring's "Unbinding" section for why this is a
    dedicated route rather than a bind with a null/empty bundle id, and
    ``agent_bundle_service.unbind_session``'s own docstring for why this is
    never gated by the feature master switch (a user must always be able to
    clear back to the operator's plain default).
    """
    try:
        await agent_bundle_service.unbind_session(
            current_user.user_id, request, session_id
        )
    except CoreFacadeError as exc:
        _raise_for_bundle_error(exc)
    return {"agent_bundle": None}


__all__ = ["router"]
