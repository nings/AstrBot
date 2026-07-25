"""End-user knowledge-base visibility + session-binding HTTP routes for the
AstrBot Portal module (Phase 3).

Every handler here goes through :mod:`astrbot.portal.services.
kb_visibility_service` (the admin-curated whitelist + count/top_k
validation layer) and, for the read-back route, directly through
:mod:`astrbot.portal.services.core_facade` (the capability wall). This file
must never import ``kb_mgr``/``core_lifecycle``/``ChatService`` or any other
capability-wall member directly (rule 5 of the module's engineering rules);
it does not need to -- both services already reach the one core touchpoint
they need on this route's behalf.

Every ``user_id`` used below comes from
:func:`astrbot.portal.auth.dependencies.get_current_user` -- never from a
request body, query string, or header (rule 4).

403 vs 404 (engineering rule 8) -- DECISION: 404, always
-----------------------------------------------------------------------
The plan's Phase 3 acceptance criterion 1 states that binding a
non-whitelisted ``kb_id`` should return 403. This file deliberately does
**not** follow that literal wording, and instead maps every authorization/
existence failure raised by ``kb_visibility_service``/``core_facade`` --
"never whitelisted", "whitelisted but revoked", "whitelisted but the KB was
since deleted from core", and "session not owned by this caller" alike --
to a single, indistinguishable HTTP 404. Reasons, in order of weight:

1. **Consistency with the rest of this module.** Every other portal route
   (``chat_routes.py``, ``file_routes.py``) already maps
   ``core_facade.ResourceNotFoundError`` to 404, specifically so a response
   can never be used as an existence oracle (module docstring invariant 2
   in ``core_facade.py``). Making the KB bind path the one 403 exception
   would reintroduce exactly the asymmetry the rest of the module goes out
   of its way to avoid, for no consistent security benefit.
2. **``kb_visibility_service.py`` already made this call.** Its module
   docstring (see that file) explains at length why it raises the exact
   same ``core_facade.ResourceNotFoundError`` class for every KB
   authorization failure, not a distinct "Forbidden" type. This route file
   has no separate exception to map to 403 even if it wanted to -- doing so
   would mean adding a parallel, redundant classification step here whose
   only purpose would be to contradict the service layer's own documented
   reasoning.
3. **KBs are not the caller's own resource.** Unlike a session or an
   attachment (owned by the caller, so "yours vs. someone else's" is a
   meaningful distinction a 403 could honestly represent), a knowledge base
   is a global operator resource the caller has never owned in the first
   place. A 403 here could only ever mean "exists in core but not on your
   whitelist" -- letting a caller enumerate the operator's non-whitelisted
   KB inventory one guess at a time by bind-probing kb_ids, which is
   exactly the whitelist's own job to prevent (see
   ``kb_visibility_service.list_visible``'s "never leak the operator's
   full KB inventory" contract).

Tests written against these routes must assert 404, not 403, for every
authorization-failure case.

Unbind = bind with an empty list (no separate DELETE route)
-----------------------------------------------------------------------
There is exactly one write route, ``POST .../sessions/{id}/knowledge-base``,
and it accepts an empty ``kb_ids`` list as a completely ordinary, valid
request: ``kb_visibility_service.bind_session`` skips both the whitelist
and live-existence checks when the deduplicated id list is empty and simply
writes ``{"kb_ids": [], "top_k": ...}`` via ``core_facade.
set_session_kb_config`` -- the exact same explicit "no knowledge base"
shape the default-open-trap fix (``core_facade.py`` invariant 4) already
writes at session creation. A dedicated ``DELETE`` route would do nothing
this one request shape doesn't already do correctly, and would double the
number of places a client-triggered write into ``kb_config`` could happen
from. Chosen for consistency and to keep exactly one audited write path
into a session's KB binding, mirroring ``core_facade.set_session_kb_config``'s
own single-writer design.
"""

from __future__ import annotations

from typing import Any, NoReturn

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

from astrbot.portal.auth.dependencies import get_current_user, require_csrf
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import core_facade, kb_visibility_service
from astrbot.portal.services.core_facade import CoreFacadeError, ResourceNotFoundError
from astrbot.portal.services.kb_visibility_service import (
    InvalidTopKError,
    KbSelectionDisabledError,
    KbVisibilityServiceError,
    TooManyKbIdsError,
)

router = APIRouter(tags=["Portal Knowledge Bases"])
"""No sub-prefix of its own (unlike ``chat_routes.router``'s ``/chat``):
mounted directly by ``astrbot.portal.api.router.portal_router`` (prefix
``/portal``), so the two routes below end up public at exactly
``/api/v1/portal/knowledge-bases`` and
``/api/v1/portal/sessions/{session_id}/knowledge-base`` -- matching this
assignment's endpoint list verbatim. Declaring a ``/sessions/...`` path
here alongside ``chat_routes.py``'s own ``/chat/sessions/...`` paths is
not a collision: the two routers are mounted at different sub-trees
(no prefix here vs ``/chat`` there), so the full paths never overlap."""


# ---------------------------------------------------------------------------
# Request models. extra="ignore" so stray client fields are silently
# dropped, matching every other request model in this module (chat_routes.py).
# ---------------------------------------------------------------------------


class BindKnowledgeBaseRequest(BaseModel):
    """Body for ``POST /sessions/{session_id}/knowledge-base``.

    ``kb_ids`` may legitimately be empty (see module docstring: this is how
    a session is unbound). The upper bound of 100 here is a generous,
    purely defensive sanity cap against an absurdly large payload -- the
    real, authoritative limit is
    ``PortalSettings.max_kb_ids_per_session`` (default 3), enforced by
    ``kb_visibility_service.bind_session`` and reported as
    :class:`~astrbot.portal.services.kb_visibility_service.TooManyKbIdsError`
    (-> HTTP 400) below, not by this model.
    """

    model_config = ConfigDict(extra="ignore")

    kb_ids: list[str] = Field(default_factory=list, max_length=100)
    top_k: int | None = None


# ---------------------------------------------------------------------------
# Error translation. See module docstring's "403 vs 404" section: every
# authorization/existence failure maps to 404, never 403.
# ---------------------------------------------------------------------------


def _raise_for_kb_error(exc: Exception) -> NoReturn:
    if isinstance(exc, ResourceNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc) or "Not found") from exc
    if isinstance(exc, KbSelectionDisabledError):
        # A blanket operator feature-switch, not a per-resource
        # authorization decision -- see kb_visibility_service.py's own
        # docstring on this exception for why 403 (not 404) is fine here:
        # there is no existence to hide behind a whitelist-of-one switch.
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    if isinstance(exc, (TooManyKbIdsError, InvalidTopKError)):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if isinstance(exc, KbVisibilityServiceError):
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    raise HTTPException(status_code=400, detail=str(exc)) from exc


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@router.get("/knowledge-bases")
async def list_knowledge_bases(
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The enabled whitelist entries this user may choose from.

    Thin pass-through to ``kb_visibility_service.list_visible``, which
    already guarantees: only ``enabled=True`` whitelist rows are even
    considered, any row whose ``kb_id`` no longer exists in core is
    silently dropped (never shown as broken-but-visible), and the shape
    returned per entry is the narrow, consumer-facing one (``kb_id``,
    ``display_name``, ``description``, doc/chunk counts, ``available``) --
    never the operator's raw ``kb_name`` cache, ``granted_by``, or any
    KB outside the whitelist. This route performs no shaping of its own
    beyond wrapping the list in an object.
    """
    entries = await kb_visibility_service.list_visible(current_user.user_id, request)
    return {"knowledge_bases": entries}


@router.get("/sessions/{session_id}/knowledge-base")
async def get_session_knowledge_base(
    session_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> dict[str, Any]:
    """The current ``kb_config`` binding for a session owned by this user.

    Ownership is checked by ``core_facade.get_session_kb_config`` (via its
    internal ``_owned_session_row`` gate) before anything else happens; a
    missing session or one owned by someone else both raise
    ``core_facade.ResourceNotFoundError`` here, mapped to 404 (see module
    docstring's "403 vs 404" section). Always returns a well-shaped
    ``{"kb_ids": [...], "top_k": ...}`` -- never ``None`` -- since the
    facade backfills an explicit empty binding for any session that
    predates Phase 3 (the default-open-trap fix, ``core_facade.py``
    invariant 4).
    """
    try:
        return await core_facade.get_session_kb_config(
            current_user.user_id, request, session_id
        )
    except CoreFacadeError as exc:
        _raise_for_kb_error(exc)


@router.post("/sessions/{session_id}/knowledge-base")
async def bind_session_knowledge_base(
    session_id: str,
    payload: BindKnowledgeBaseRequest,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Bind (or, with an empty ``kb_ids``, unbind -- see module docstring)
    a set of knowledge bases to a session owned by this user.

    All validation happens in ``kb_visibility_service.bind_session`` before
    anything is written: the feature master switch, the
    ``max_kb_ids_per_session`` count cap, ``top_k`` range validation
    (rejected outright when out of range, never silently clamped -- see
    that function's own docstring), and -- critically -- that EVERY
    requested id is both an enabled portal whitelist entry AND still
    exists in core right now. Any single id failing either check rejects
    the WHOLE request; this route never binds a partial subset of what was
    requested. See module docstring's "403 vs 404" section for why every
    one of those authorization/existence failures surfaces as 404.
    """
    try:
        return await kb_visibility_service.bind_session(
            current_user.user_id,
            request,
            session_id,
            payload.kb_ids,
            payload.top_k,
        )
    except (CoreFacadeError, KbVisibilityServiceError) as exc:
        _raise_for_kb_error(exc)


__all__ = ["router"]
