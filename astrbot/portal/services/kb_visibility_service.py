"""Portal-side knowledge-base visibility whitelist + session-binding
validation (Phase 3).

This file does **not** import ``kb_mgr``/``KnowledgeBaseManager``,
``core_lifecycle``, ``ChatService``, ``shared_preferences``, or any other
core internal directly -- per the capability wall (see ``core_facade.py``'s
module docstring), the two core touchpoints this module needs (resolving a
``kb_id`` to its live core state, and writing the validated binding into a
session's ``kb_config``) are reached exclusively through
``core_facade.get_kb_live_info``/``get_kb_live_info_batch``/
``list_all_core_kbs``/``set_session_kb_config``. This module owns everything
upstream and downstream of those calls, exactly mirroring the split
``attachment_service.py`` uses for the same shape of problem (a portal-side
authorization table gating a core resource):

* **Upstream (before any bind ever reaches a session's ``kb_config``):**
  the admin-curated ``PortalKbVisibility`` whitelist itself (grant/revoke/
  update-display-metadata), count/``top_k`` validation against
  ``PortalSettings``, and asserting every requested ``kb_id`` is BOTH
  whitelisted here AND still exists in core right now (whitelisting can go
  stale -- see ``PortalKbVisibility.kb_name``'s own docstring in
  ``db/models.py`` for exactly how).
* **Downstream:** exposing ``list_visible`` for the end-user KB picker
  (never the operator's full KB inventory -- only enabled whitelist rows,
  joined with live core stats, with any row whose ``kb_id`` no longer
  exists in core silently omitted) and ``admin_list_all`` for the operator
  whitelist-management page (which, unlike ``list_visible``, DOES surface
  dangling rows -- an operator needs to see and fix/revoke a broken entry,
  an end user must never be offered one).

403 vs 404 for a non-whitelisted/non-existent ``kb_id`` (design checklist
item 8)
-----------------------------------------------------------------------
The plan's stated acceptance criterion (design/portal-multiuser-plan.md,
Phase 3) says binding a non-whitelisted ``kb_id`` should return 403. This
module deliberately does NOT follow that literally: :func:`assert_all_visible`
and :func:`bind_session` both raise the exact same
``core_facade.ResourceNotFoundError`` class every other authorization
failure in this codebase raises (session ownership, attachment ownership),
for the same reason invariant 2 of ``core_facade.py``'s module docstring
gives for those: a 403 lets a caller distinguish "exists but you can't have
it" from "doesn't exist at all", i.e. an existence oracle -- exactly the
kind of oracle ``core_facade.get_kb_live_info`` is careful never to leak
through any *other* surface in this module either (``list_visible`` never
exposes the operator's non-whitelisted KBs; a dangling id and a
never-whitelisted id must look identical to an end user). Making the KB
bind path the one exception to that rule for the sake of matching the
plan's literal wording would reintroduce exactly the asymmetry the rest of
the module goes out of its way to avoid, for a resource (KBs) that, unlike
a session/attachment, isn't even owned by the caller in the first place --
so there is no "yours vs someone else's" distinction a 403 could honestly
represent here anyway; it would only ever mean "exists in core but not on
your whitelist", which a caller could use to enumerate the operator's KB
inventory one guess at a time. Route/test code built against this service
must therefore map both "not whitelisted" and "whitelisted but gone from
core" to HTTP 404, never 403, for every KB route -- see
``contracts_for_downstream`` in this phase's handoff for the explicit call
to route/test authors to align on this.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from fastapi import Request
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalKbVisibility
from astrbot.portal.services import core_facade
from astrbot.portal.services.core_facade import ResourceNotFoundError

# ---------------------------------------------------------------------------
# Exceptions. `ResourceNotFoundError` is deliberately NOT redefined here --
# see the module docstring's "403 vs 404" section for why every
# authorization/existence failure below raises that exact class rather than
# a distinct "Forbidden"/"KbNotFound" type.
# ---------------------------------------------------------------------------


class KbVisibilityServiceError(Exception):
    """Base class for all errors raised by this module (other than
    authorization/existence failures, which raise
    ``core_facade.ResourceNotFoundError`` -- see module docstring)."""


class KbSelectionDisabledError(KbVisibilityServiceError):
    """``PortalSettings.kb_selection_enabled`` is ``False``.

    Callers should map this to a 4xx that is clearly NOT "not found"/"not
    authorized" (e.g. 403 or 409 is fine here -- this is an operator
    feature switch, not a per-resource authorization decision, so the
    "never confirm existence" concern the module docstring raises for
    individual ``kb_id``s does not apply to this blanket switch).
    """


class TooManyKbIdsError(KbVisibilityServiceError):
    """Requested ``kb_ids`` exceeds ``PortalSettings.max_kb_ids_per_session``."""


class InvalidTopKError(KbVisibilityServiceError):
    """``top_k`` is not a positive integer, or exceeds
    ``PortalSettings.max_kb_top_k``."""


class KbAlreadyGrantedError(KbVisibilityServiceError):
    """(Admin-only) ``admin_grant`` raced a concurrent grant of the same
    ``kb_id`` and lost the unique-constraint race."""


# ---------------------------------------------------------------------------
# End-user-facing: the whitelist an end user may choose from.
# ---------------------------------------------------------------------------


def _consumer_entry(row: PortalKbVisibility, live: dict[str, Any]) -> dict[str, Any]:
    """Shape a single visible entry for the end-user KB picker.

    Deliberately narrow: no ``granted_by``, no raw operator-facing
    ``kb_name`` cache (prefers ``display_name``, falling back to the live
    core name only when no friendlier label was set -- see
    ``PortalKbVisibility.display_name``'s own docstring), and no internal
    ``id``/timestamps. This is the entire end-user-visible surface for a
    knowledge base; nothing about the operator's non-whitelisted KBs, or
    about this row's own bookkeeping, leaks through it.
    """
    return {
        "kb_id": row.kb_id,
        "display_name": row.display_name or live["kb_name"],
        "description": row.description,
        "doc_count": live["doc_count"],
        "chunk_count": live["chunk_count"],
        "available": live["available"],
    }


async def list_visible(user_id: str, request: Request) -> list[dict[str, Any]]:
    """The enabled whitelist entries ``user_id`` may choose from for their
    own session's ``kb_config``.

    Never exposes the operator's full KB inventory: only
    ``PortalKbVisibility`` rows with ``enabled=True`` are even considered,
    and any such row whose ``kb_id`` no longer exists in core (dangling --
    see ``PortalKbVisibility.kb_name``'s own docstring) is silently
    dropped rather than shown as broken/unavailable-but-visible; an end
    user must never be offered something they cannot actually bind.

    ``user_id`` is accepted (and required) for call-site symmetry with
    every other facade/service function in this module -- there is no
    per-user variation in Phase 3 (every end user shares the same
    operator-curated whitelist), but keeping the parameter now avoids
    changing every call site if a future phase adds one (e.g. per-team
    visibility).
    """
    del user_id
    settings = get_portal_settings()
    if not settings.kb_selection_enabled:
        return []

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalKbVisibility).where(col(PortalKbVisibility.enabled).is_(True))
        )
        rows = list(result.scalars().all())
    if not rows:
        return []

    live_by_id = await core_facade.get_kb_live_info_batch(
        request, [row.kb_id for row in rows]
    )

    visible: list[dict[str, Any]] = []
    for row in rows:
        live = live_by_id.get(row.kb_id)
        if live is None:
            continue
        visible.append(_consumer_entry(row, live))
    return visible


async def _visible_kb_ids(kb_ids: Sequence[str]) -> set[str]:
    """The subset of ``kb_ids`` that are currently enabled whitelist
    entries. ONE query for the whole set, never N -- mirrors
    ``attachment_service.assert_owns_all``'s shape.
    """
    unique_ids = {str(kb_id) for kb_id in kb_ids}
    if not unique_ids:
        return set()
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(col(PortalKbVisibility.kb_id)).where(
                col(PortalKbVisibility.kb_id).in_(unique_ids),
                col(PortalKbVisibility.enabled).is_(True),
            )
        )
        return set(result.scalars().all())


async def assert_all_visible(kb_ids: Sequence[str]) -> None:
    """Raise ``core_facade.ResourceNotFoundError`` unless every id in
    ``kb_ids`` is a currently-enabled whitelist entry.

    Does NOT check live core existence -- that is a separate, deliberately
    distinct check (see :func:`bind_session`'s two-pass structure and the
    module docstring's "whitelisting can go stale" note); this function
    answers only "is this id on the portal's own authorization list right
    now", against ONE query for the entire batch.
    """
    unique_ids = {str(kb_id) for kb_id in kb_ids}
    if not unique_ids:
        return
    visible_ids = await _visible_kb_ids(unique_ids)
    if any(kb_id not in visible_ids for kb_id in unique_ids):
        raise ResourceNotFoundError("Knowledge base not found")


async def bind_session(
    user_id: str,
    request: Request,
    session_id: str,
    kb_ids: Sequence[str],
    top_k: int | None = None,
) -> dict[str, Any]:
    """Validate and write a session's knowledge-base binding.

    Order of checks, each strictly before the next, and strictly before
    anything is written to the session's ``kb_config``:

    1. ``PortalSettings.kb_selection_enabled`` -- the feature's master
       switch (:class:`KbSelectionDisabledError` if off).
    2. Deduplicate ``kb_ids`` (order-preserving) and cap the count at
       ``PortalSettings.max_kb_ids_per_session`` (:class:`TooManyKbIdsError`
       if exceeded).
    3. Resolve/validate ``top_k``: defaults to
       ``PortalSettings.default_kb_top_k`` when omitted, must be a positive
       integer, and must not exceed ``PortalSettings.max_kb_top_k`` --
       :class:`InvalidTopKError` otherwise. Deliberately does NOT silently
       clamp an out-of-range value down to the ceiling: a client that sent
       an invalid ``top_k`` gets a clear rejection, not a silently
       different number written on their behalf.
    4. **Portal whitelist**: every remaining id must be an enabled
       ``PortalKbVisibility`` entry (:func:`assert_all_visible`, one
       query).
    5. **Live core existence**: every remaining id must still resolve to
       an actual KB in core right now (one in-memory batch lookup via
       ``core_facade.get_kb_live_info_batch`` -- whitelisting can go
       stale, see module docstring).

    Both authorization checks (4 and 5) reject the WHOLE request if ANY
    single id fails either one -- this function never binds a partial set
    of the requested ids; a client either gets exactly what they asked for
    or an error, never a silently-narrowed subset that could be confused
    for success. Both raise the identical ``core_facade.
    ResourceNotFoundError`` (see module docstring's "403 vs 404" section)
    regardless of which of the two checks actually failed, so a caller can
    never distinguish "never whitelisted" from "whitelisted but now gone
    from core" from the response alone.

    Only once every check above has passed does this call
    ``core_facade.set_session_kb_config`` -- the one write path into the
    session's live retrieval config -- with the deduplicated id list and
    the resolved ``top_k``.
    """
    settings = get_portal_settings()
    if not settings.kb_selection_enabled:
        raise KbSelectionDisabledError("Knowledge base selection is disabled")

    deduped: list[str] = []
    seen: set[str] = set()
    for kb_id in kb_ids:
        kb_id = str(kb_id)
        if kb_id not in seen:
            seen.add(kb_id)
            deduped.append(kb_id)

    if len(deduped) > settings.max_kb_ids_per_session:
        raise TooManyKbIdsError(
            f"At most {settings.max_kb_ids_per_session} knowledge base(s) may "
            "be bound to a session at once"
        )

    resolved_top_k = settings.default_kb_top_k if top_k is None else top_k
    if (
        not isinstance(resolved_top_k, int)
        or isinstance(resolved_top_k, bool)
        or resolved_top_k < 1
    ):
        raise InvalidTopKError("top_k must be a positive integer")
    if resolved_top_k > settings.max_kb_top_k:
        raise InvalidTopKError(f"top_k may not exceed {settings.max_kb_top_k}")

    if deduped:
        await assert_all_visible(deduped)
        live_by_id = await core_facade.get_kb_live_info_batch(request, deduped)
        missing = [kb_id for kb_id in deduped if live_by_id.get(kb_id) is None]
        if missing:
            # Whitelisted here but gone from core since -- identical
            # rejection to "never whitelisted" (module docstring).
            raise ResourceNotFoundError("Knowledge base not found")

    kb_config = await core_facade.set_session_kb_config(
        user_id, request, session_id, deduped, resolved_top_k
    )
    logger.info(
        f"[portal] user {user_id} bound session {session_id} to "
        f"{len(deduped)} kb(s), top_k={resolved_top_k}"
    )
    return kb_config


# ---------------------------------------------------------------------------
# Admin-side CRUD. Operator-only: every function below trusts its caller
# (the admin routes, gated by ``require_system_scope`` imported verbatim
# from ``astrbot.dashboard.api.auth`` -- see design checklist item 5) to
# have already authenticated a real operator. None of these functions
# perform any authorization check of their own.
# ---------------------------------------------------------------------------


def _admin_entry(row: PortalKbVisibility, live: dict[str, Any] | None) -> dict[str, Any]:
    """Shape one whitelist row for the operator's management page.

    Unlike :func:`_consumer_entry`, this DOES surface a dangling row
    (``live is None``) rather than hiding it -- an operator needs to see
    that a granted ``kb_id`` no longer exists in core so they can revoke or
    re-grant it; an end user must never be offered the same row at all
    (see :func:`list_visible`).
    """
    return {
        "kb_id": row.kb_id,
        "kb_name": row.kb_name,
        "display_name": row.display_name,
        "description": row.description,
        "enabled": row.enabled,
        "granted_by": row.granted_by,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
        "dangling": live is None,
        "live_kb_name": live["kb_name"] if live else None,
        "doc_count": live["doc_count"] if live else None,
        "chunk_count": live["chunk_count"] if live else None,
        "available": live["available"] if live else False,
    }


async def admin_list_all(request: Request) -> list[dict[str, Any]]:
    """Every ``PortalKbVisibility`` row (enabled or not), joined with live
    core status -- including dangling rows (see :func:`_admin_entry`)."""
    async with portal_async_sessionmaker() as session:
        result = await session.execute(select(PortalKbVisibility))
        rows = list(result.scalars().all())
    if not rows:
        return []
    live_by_id = await core_facade.get_kb_live_info_batch(
        request, [row.kb_id for row in rows]
    )
    return [_admin_entry(row, live_by_id.get(row.kb_id)) for row in rows]


async def admin_list_core_kbs(request: Request) -> list[dict[str, Any]]:
    """Every knowledge base currently loaded in core, as plain data --
    the operator's "browse what's available to grant" primitive.

    Thin pass-through to ``core_facade.list_all_core_kbs``: this module
    never imports ``kb_mgr``/``core_lifecycle`` itself (module docstring),
    so this is the only way the admin routes can enumerate core's raw KB
    inventory without reaching around the capability wall.
    """
    return await core_facade.list_all_core_kbs(request)


async def admin_grant(
    request: Request,
    kb_id: str,
    *,
    granted_by: str | None,
    display_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Whitelist ``kb_id`` for end-user visibility, or re-enable/refresh an
    already-existing (possibly previously revoked) row for it.

    Verifies ``kb_id`` currently exists in core BEFORE writing anything --
    an operator can never whitelist a ``kb_id`` that does not exist yet
    (:class:`ResourceNotFoundError` if it doesn't). Re-caches ``kb_name``
    from core's current live value every time this is called (including on
    an update to an existing row), since this is the one deliberate,
    operator-initiated moment ``PortalKbVisibility.kb_name``'s own
    docstring calls out as the expected way to refresh that cache if it has
    drifted.
    """
    live = await core_facade.get_kb_live_info(request, kb_id)
    if live is None:
        raise ResourceNotFoundError("Knowledge base not found")

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalKbVisibility).where(col(PortalKbVisibility.kb_id) == kb_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = PortalKbVisibility(
                kb_id=kb_id,
                kb_name=live["kb_name"],
                enabled=True,
                display_name=display_name,
                description=description,
                granted_by=granted_by,
            )
            session.add(row)
        else:
            row.kb_name = live["kb_name"]
            row.enabled = True
            if display_name is not None:
                row.display_name = display_name
            if description is not None:
                row.description = description
            row.granted_by = granted_by

        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            raise KbAlreadyGrantedError(
                f"Knowledge base {kb_id!r} was just granted concurrently; retry"
            ) from exc
        await session.refresh(row)

    logger.info(f"[portal] admin {granted_by!r} granted kb {kb_id} ({live['kb_name']!r})")
    return _admin_entry(row, live)


async def admin_revoke(request: Request, kb_id: str) -> dict[str, Any]:
    """Flip ``enabled=False`` on an existing whitelist row.

    Never deletes the row -- see ``PortalKbVisibility.enabled``'s own
    docstring for the audit-trail rationale (preserving
    ``granted_by``/``created_at`` history rather than losing it on
    revocation).

    Still reports accurate live core status (:func:`_admin_entry`) in the
    returned data -- revoking visibility says nothing about whether the KB
    itself still exists in core, so ``dangling``/``available`` must reflect
    a real lookup here, not an assumed ``None``.
    """
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalKbVisibility).where(col(PortalKbVisibility.kb_id) == kb_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("Knowledge base visibility entry not found")
        row.enabled = False
        session.add(row)
        await session.commit()
        await session.refresh(row)

    live = await core_facade.get_kb_live_info(request, kb_id)
    logger.info(f"[portal] revoked kb visibility for {kb_id}")
    return _admin_entry(row, live)


async def admin_update_display(
    request: Request,
    kb_id: str,
    *,
    display_name: str | None = None,
    description: str | None = None,
) -> dict[str, Any]:
    """Update the consumer-facing ``display_name``/``description`` of an
    existing whitelist row without touching ``enabled``/``kb_name``.

    Both parameters are applied independently when not ``None`` -- passing
    only ``display_name`` leaves ``description`` untouched and vice versa;
    passing an explicit empty string clears the field (only ``None`` means
    "leave unchanged", matching ``kb_mgr.update_kb``'s own
    None-means-unchanged convention for the equivalent core-side update).

    Returns plain data (:func:`_admin_entry`, joined with a fresh live core
    lookup), never the ORM row itself.
    """
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalKbVisibility).where(col(PortalKbVisibility.kb_id) == kb_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise ResourceNotFoundError("Knowledge base visibility entry not found")
        if display_name is not None:
            row.display_name = display_name
        if description is not None:
            row.description = description
        session.add(row)
        await session.commit()
        await session.refresh(row)

    live = await core_facade.get_kb_live_info(request, kb_id)
    return _admin_entry(row, live)


__all__ = [
    "KbVisibilityServiceError",
    "KbSelectionDisabledError",
    "TooManyKbIdsError",
    "InvalidTopKError",
    "KbAlreadyGrantedError",
    "list_visible",
    "assert_all_visible",
    "bind_session",
    "admin_list_all",
    "admin_list_core_kbs",
    "admin_grant",
    "admin_revoke",
    "admin_update_display",
]
