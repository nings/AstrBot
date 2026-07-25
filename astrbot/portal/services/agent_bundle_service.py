"""Portal-side Agent Bundle whitelist + session-binding orchestration
(Phase 4).

This file does **not** import ``ChatService``, ``conv_mgr``, ``sp``,
``persona_mgr``, ``ProviderManager``, or ``kb_mgr`` directly -- per the
capability wall (see ``core_facade.py``'s module docstring), every core
touchpoint this module needs (does a persona/provider id still exist right
now; write the resolved persona/provider/kb bindings onto a session) is
reached exclusively through ``core_facade.persona_exists`` /
``core_facade.provider_exists`` / ``core_facade.set_session_persona`` /
``core_facade.set_session_provider`` / ``core_facade.set_session_kb_config``
/ ``core_facade.set_session_bundle_id`` / ``core_facade.
clear_session_bundle_id`` / ``core_facade.get_session_agent_bindings``. This
module owns everything upstream and downstream of those calls, mirroring
the exact split ``kb_visibility_service.py`` (Phase 3) already established
for the same shape of problem (a portal-side authorization/curation table
gating a core resource) -- ``PortalAgentBundle`` (``db/models.py``) is that
table for this phase, and its own docstring is the load-bearing contract
this module implements against; read it first.

**What "dangling" means, and why persona/provider are NOT treated the same
as KB ids here:**

A bundle's ``persona_id``/``chat_provider_id``/``kb_ids_json`` can all go
stale after the bundle was created (operator deletes a persona, removes a
provider, revokes a KB's ``PortalKbVisibility``) -- see
``PortalAgentBundle``'s own docstring for the full "dangling reference"
discussion and the ground truth backing each surface's own graceful
fallback in core. This module treats the three dimensions differently on
purpose, because they are not symmetric:

* ``persona_id``/``chat_provider_id`` are *single scalar* references. If
  one has gone dangling, there is no partial/reduced version of "the
  persona" or "the provider" to fall back to that is still recognizably
  *this bundle's* choice -- silently substituting some other value would
  misrepresent what the end user thinks they selected. A bundle with a
  dangling persona or provider is therefore **not selectable at all**
  (excluded from :func:`list_available`, and :func:`bind_session` refuses
  it outright) until an operator fixes it. This is what "a dangling bundle
  must never be selectable by an end user" (this phase's own acceptance
  criterion) means concretely.
* ``kb_ids_json`` is a *list*. A single stale entry within an otherwise
  healthy list does not invalidate the other entries, and core's own
  ``retrieve_knowledge_base`` already tolerates a reduced KB set
  gracefully (it is not this module inventing new fallback behaviour).
  Per ``PortalAgentBundle``'s docstring (**"Neither check is optional"**),
  every id in ``kb_ids_json`` is validated against current
  ``PortalKbVisibility`` (``enabled=True``) at TWO different times, with
  two DIFFERENT behaviours:

  1. **Create/update time** (:func:`admin_create`/:func:`admin_update`):
     reject the whole request immediately if any requested ``kb_id`` is
     not currently visible -- the admin gets a clear, immediate error
     instead of a silently-truncated bundle.
  2. **Apply/bind time** (:func:`bind_session`): silently drop any
     ``kb_id`` that is no longer ``enabled=True`` (logging a warning) and
     apply the rest -- visibility can be revoked at any time after the
     bundle was saved, and refusing the *entire* bind over one stale KB
     reference would be a worse outcome than a session that ends up with
     one fewer knowledge base than the bundle currently lists.

  A stale KB reference alone therefore does NOT make a bundle
  "unselectable" the way a dangling persona/provider does -- it only shows
  up as a per-bundle ``dangling_kb_ids``/``dangling`` flag in the
  *operator's* view (:func:`admin_list_all`), never as an end-user-visible
  rejection.

**All-or-nothing apply, and why it means "validate everything, then write
everything" rather than a single database transaction:**
:func:`bind_session` resolves and validates persona, provider, and KB
visibility BEFORE issuing a single write. The actual writes
(``core_facade.set_session_persona``/``set_session_provider``/
``set_session_kb_config``/``set_session_bundle_id``) land in two
independent storage backends (``ConversationManager``'s SQL table and
``SharedPreferences``' own store) that cannot be wrapped in one atomic
database transaction anyway -- so "all-or-nothing" here means "every
precondition is confirmed true before the first write happens", not a
two-phase commit. ``set_session_bundle_id`` is deliberately the LAST write
in the sequence (see its own docstring in ``core_facade.py``): a session's
bookkeeping never claims a bundle whose persona/provider/KB state was not,
in fact, just successfully applied.

**Every ``bind_session``/``unbind_session`` call fully determines every
dimension, never carries a previous bundle's leftover value:** switching
from bundle A (sets a persona) to bundle B (``persona_id=None``, i.e. "this
preset doesn't set one") must not leave bundle A's persona silently in
effect forever -- that would be a hybrid state that was never any single
operator-configured combination (this phase's own rule: "make sure it
cannot leave the session in a state that is neither the bundle's nor the
operator's"). Concretely, this is why :func:`bind_session` always calls
``core_facade.set_session_persona``/``set_session_provider`` with the
bundle's OWN field value (``None`` included) rather than only calling them
when the bundle sets something -- ``core_facade``'s own functions already
resolve ``None`` to "the operator's default"/"no per-umo preference"
deterministically (see their docstrings), so calling them unconditionally
on every bind, and again identically on every :func:`unbind_session`, is
what actually guarantees this.

**404, never 403, for "can't have this bundle" (mirrors
``kb_visibility_service``'s own precedent exactly):** ``bind_session``
raises ``core_facade.ResourceNotFoundError`` uniformly for "bundle_id does
not exist", "bundle exists but ``enabled=False``", and "bundle exists and
is enabled but is currently dangling" -- the same generic 404 for all
three, so a caller can never use the response to enumerate which
bundle_ids exist, which are disabled, or which are currently broken. See
``kb_visibility_service.py``'s own module docstring ("403 vs 404") for the
full rationale, reused here verbatim.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any

from fastapi import Request
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalAgentBundle, PortalKbVisibility
from astrbot.portal.services import core_facade
from astrbot.portal.services.core_facade import ResourceNotFoundError

# ---------------------------------------------------------------------------
# Exceptions. `ResourceNotFoundError` is deliberately NOT redefined here for
# the "can't have this bundle" family of failures -- see module docstring's
# "404, never 403" section. The classes below are for failure modes that
# genuinely are NOT an authorization/existence question (the feature-level
# master switch, and admin-CRUD-time input validation, where the caller is
# a trusted operator and there is no existence-oracle concern to hide).
# ---------------------------------------------------------------------------


class AgentBundleServiceError(Exception):
    """Base class for all errors raised by this module (other than
    authorization/existence failures, which raise ``core_facade.
    ResourceNotFoundError`` -- see module docstring)."""


class AgentBundleSelectionDisabledError(AgentBundleServiceError):
    """``PortalSettings.agent_bundle_selection_enabled`` is ``False``.

    Mirrors ``kb_visibility_service.KbSelectionDisabledError`` exactly: an
    operator feature switch, not a per-resource authorization decision.
    Callers should map this to a 4xx that is clearly NOT "not found"/"not
    authorized" (403/409 is fine). Deliberately never raised by
    :func:`unbind_session` -- see that function's own docstring.
    """


class TooManyKbIdsError(AgentBundleServiceError):
    """(Admin create/update only) requested ``kb_ids`` exceeds
    ``PortalSettings.max_kb_ids_per_bundle``."""


class PersonaNotFoundError(AgentBundleServiceError):
    """(Admin create/update only) the given ``persona_id`` does not
    currently resolve to a real persona in core."""


class ProviderNotFoundError(AgentBundleServiceError):
    """(Admin create/update only) the given ``chat_provider_id`` does not
    currently resolve to a real provider instance in core."""


class KbNotVisibleError(AgentBundleServiceError):
    """(Admin create/update only) one or more requested ``kb_ids`` is not a
    currently-enabled ``PortalKbVisibility`` entry. Rejected immediately at
    create/update time rather than silently truncated -- see module
    docstring's KB two-point-validation section, point 1."""


# ---------------------------------------------------------------------------
# Shared KB-visibility helpers. Deliberately duplicated (not imported) from
# ``kb_visibility_service._visible_kb_ids``: that helper is module-private,
# and the query is eight lines against this module's own permitted import
# (``PortalKbVisibility``) -- reaching into another service module's private
# helper would couple the two services' internals together for no benefit.
# ---------------------------------------------------------------------------


async def _visible_kb_id_set(kb_ids: Sequence[str]) -> set[str]:
    """The subset of ``kb_ids`` that are currently enabled
    ``PortalKbVisibility`` entries. One query for the whole batch."""
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


def _parse_kb_ids(kb_ids_json: str) -> list[str]:
    try:
        parsed = json.loads(kb_ids_json or "[]")
    except (ValueError, TypeError):
        return []
    if not isinstance(parsed, list):
        return []
    return [str(kb_id) for kb_id in parsed]


async def _bundle_health(request: Request, bundle: PortalAgentBundle) -> dict[str, Any]:
    """Live-reference health for one bundle -- the single source of truth
    both :func:`list_available`/:func:`bind_session` (end-user selectability)
    and :func:`admin_list_all` (operator dangling status) resolve against,
    so the two views can never disagree about what "dangling" means.

    ``selectable`` is ``persona_ok and provider_ok`` ONLY -- a stale KB
    reference never makes a bundle unselectable by itself (module
    docstring: KB staleness is handled by silent per-id dropping at apply
    time, not by blocking the whole bundle).
    """
    persona_ok = bundle.persona_id is None or await core_facade.persona_exists(
        request, bundle.persona_id
    )
    provider_ok = bundle.chat_provider_id is None or await core_facade.provider_exists(
        request, bundle.chat_provider_id
    )
    kb_ids = _parse_kb_ids(bundle.kb_ids_json)
    visible_ids = await _visible_kb_id_set(kb_ids) if kb_ids else set()
    visible_kb_ids = [kb_id for kb_id in kb_ids if kb_id in visible_ids]
    dangling_kb_ids = [kb_id for kb_id in kb_ids if kb_id not in visible_ids]
    return {
        "persona_ok": persona_ok,
        "provider_ok": provider_ok,
        "kb_ids": kb_ids,
        "visible_kb_ids": visible_kb_ids,
        "dangling_kb_ids": dangling_kb_ids,
        "selectable": persona_ok and provider_ok,
    }


async def _validate_bundle_fields(
    request: Request,
    *,
    persona_id: str | None,
    chat_provider_id: str | None,
    kb_ids: Sequence[str],
) -> list[str]:
    """Shared create/update validation (module docstring's KB
    two-point-validation, point 1 -- plus the same immediate-error
    treatment extended to persona/provider for admin UX). Returns the
    deduplicated, order-preserved ``kb_id`` list to persist. Raises
    immediately on the first violation found; never silently truncates.
    """
    if persona_id is not None and not await core_facade.persona_exists(request, persona_id):
        raise PersonaNotFoundError(f"Persona {persona_id!r} does not currently exist")
    if chat_provider_id is not None and not await core_facade.provider_exists(
        request, chat_provider_id
    ):
        raise ProviderNotFoundError(
            f"Provider {chat_provider_id!r} does not currently exist"
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for kb_id in kb_ids:
        kb_id = str(kb_id)
        if kb_id not in seen:
            seen.add(kb_id)
            deduped.append(kb_id)

    settings = get_portal_settings()
    if len(deduped) > settings.max_kb_ids_per_bundle:
        raise TooManyKbIdsError(
            f"At most {settings.max_kb_ids_per_bundle} knowledge base(s) may be "
            "bound to a bundle at once"
        )

    if deduped:
        visible_ids = await _visible_kb_id_set(deduped)
        invalid = [kb_id for kb_id in deduped if kb_id not in visible_ids]
        if invalid:
            raise KbNotVisibleError(
                f"Knowledge base(s) not currently visible/enabled: {invalid}"
            )

    return deduped


# ---------------------------------------------------------------------------
# End-user-facing.
# ---------------------------------------------------------------------------


def _consumer_entry(bundle: PortalAgentBundle) -> dict[str, Any]:
    """Shape a single bundle for the end-user picker/selection-confirmation.

    Deliberately narrow: NEVER ``persona_id``/``chat_provider_id``/
    ``kb_ids`` -- a bundle is a deliberately opaque choice from the
    client's side (this phase's own rule 6). This is the ENTIRE
    end-user-visible surface for a bundle, used identically by
    :func:`list_available`, :func:`bind_session`'s return value, and
    :func:`get_session_binding` -- there must never be a second, richer
    shape that leaks more to some other end-user-facing call site.
    """
    return {
        "bundle_id": bundle.bundle_id,
        "name": bundle.name,
        "description": bundle.description,
        "sort_order": bundle.sort_order,
        "is_default": bundle.is_default,
    }


async def list_available(user_id: str, request: Request) -> list[dict[str, Any]]:
    """The enabled, currently-selectable bundles ``user_id`` may choose from.

    Never exposes a disabled row, and never exposes a dangling one either
    (module docstring's "must never be selectable" rule) -- only rows that
    are both ``enabled=True`` and pass :func:`_bundle_health`'s
    ``selectable`` check are returned. ``user_id`` is accepted for call-site
    symmetry with every other facade/service function (no per-user
    variation in Phase 4 -- every end user sees the same operator-curated
    list), mirroring ``kb_visibility_service.list_visible``'s own contract.
    """
    del user_id
    settings = get_portal_settings()
    if not settings.agent_bundle_selection_enabled:
        return []

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle)
            .where(col(PortalAgentBundle.enabled).is_(True))
            .order_by(col(PortalAgentBundle.sort_order), col(PortalAgentBundle.name))
        )
        rows = list(result.scalars().all())

    available: list[dict[str, Any]] = []
    for bundle in rows:
        health = await _bundle_health(request, bundle)
        if not health["selectable"]:
            continue
        available.append(_consumer_entry(bundle))
    return available


async def get_session_binding(
    user_id: str, request: Request, session_id: str
) -> dict[str, Any] | None:
    """The bundle currently selected for an owned session, in the same
    consumer-facing shape as :func:`list_available`'s entries -- or
    ``None`` if no bundle is currently selected (including when the
    bookkeeping points at a bundle that has since been deleted/disabled;
    that is treated identically to "nothing selected", never as an error,
    since core_facade's own ``get_session_agent_bindings`` never validates
    the bundle_id it read back).
    """
    bindings = await core_facade.get_session_agent_bindings(user_id, request, session_id)
    bundle_id = bindings.get("bundle_id")
    if not bundle_id:
        return None

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).where(col(PortalAgentBundle.bundle_id) == bundle_id)
        )
        bundle = result.scalar_one_or_none()
    if bundle is None:
        return None
    return _consumer_entry(bundle)


async def bind_session(
    user_id: str, request: Request, session_id: str, bundle_id: str
) -> dict[str, Any]:
    """Resolve, validate, and apply ``bundle_id`` to an owned session.

    Order of checks, each strictly before the next, and strictly before
    anything is written to the session:

    1. ``PortalSettings.agent_bundle_selection_enabled`` -- the feature's
       master switch (:class:`AgentBundleSelectionDisabledError` if off).
    2. The bundle must exist and be ``enabled=True``
       (``core_facade.ResourceNotFoundError`` otherwise).
    3. The bundle must currently be *selectable*
       (:func:`_bundle_health`'s ``selectable`` -- persona/provider, if
       set, must still resolve; see module docstring for why KB staleness
       alone does not fail this check). Also ``core_facade.
       ResourceNotFoundError`` -- see module docstring's "404, never 403".

    Only once every check above has passed are the actual writes issued,
    via ``core_facade``, in this order: persona, provider, KB config,
    bundle-id bookkeeping (last -- see ``core_facade.
    set_session_bundle_id``'s own docstring for why). ``kb_ids`` passed to
    ``set_session_kb_config`` is the bundle's ``visible_kb_ids`` (any
    ``kb_id`` no longer ``enabled=True`` in ``PortalKbVisibility`` is
    silently dropped, logged as a warning -- module docstring, point 2 of
    the KB two-point validation), always at ``PortalSettings.
    default_kb_top_k`` (``PortalAgentBundle`` carries no per-bundle
    ``top_k`` of its own).

    Returns the same narrow consumer-facing shape as
    :func:`list_available`'s entries -- never the resolved persona_id/
    chat_provider_id/kb_ids (rule 6: the client never learns what a bundle
    actually resolved to, only that it was applied).
    """
    settings = get_portal_settings()
    if not settings.agent_bundle_selection_enabled:
        raise AgentBundleSelectionDisabledError("Agent bundle selection is disabled")

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).where(
                col(PortalAgentBundle.bundle_id) == str(bundle_id)
            )
        )
        bundle = result.scalar_one_or_none()
    if bundle is None or not bundle.enabled:
        raise ResourceNotFoundError("Agent bundle not found")

    health = await _bundle_health(request, bundle)
    if not health["selectable"]:
        logger.warning(
            f"[portal] agent bundle {bundle.bundle_id} is dangling "
            f"(persona_ok={health['persona_ok']}, provider_ok={health['provider_ok']}); "
            f"refusing selection for user {user_id}"
        )
        raise ResourceNotFoundError("Agent bundle not found")

    if health["dangling_kb_ids"]:
        logger.warning(
            f"[portal] agent bundle {bundle.bundle_id} references kb_id(s) no "
            f"longer portal-visible, dropping at apply time: "
            f"{health['dangling_kb_ids']}"
        )

    # The cap that matters is how many knowledge bases can end up bound to a
    # session, so it is enforced here -- at the moment a session binding is
    # written -- and not only at bundle authoring time.
    #
    # `max_kb_ids_per_bundle` merely *defaults* to `max_kb_ids_per_session`; the
    # two are independent fields, so an operator who tightens the session cap
    # without also tightening the bundle cap would otherwise let a bundle
    # authored under the old, looser limit apply more corpora than a direct
    # bind permits. Clamping keeps that invariant true from either path.
    applied_kb_ids = list(health["visible_kb_ids"])
    if len(applied_kb_ids) > settings.max_kb_ids_per_session:
        logger.warning(
            f"[portal] agent bundle {bundle.bundle_id} carries "
            f"{len(applied_kb_ids)} visible knowledge base(s), above the "
            f"current per-session cap of {settings.max_kb_ids_per_session}; "
            f"applying the first {settings.max_kb_ids_per_session} and dropping "
            f"the rest. Lower max_kb_ids_per_bundle or edit the bundle to "
            f"remove the ambiguity."
        )
        applied_kb_ids = applied_kb_ids[: settings.max_kb_ids_per_session]

    await core_facade.set_session_persona(user_id, request, session_id, bundle.persona_id)
    await core_facade.set_session_provider(
        user_id, request, session_id, bundle.chat_provider_id
    )
    await core_facade.set_session_kb_config(
        user_id, request, session_id, applied_kb_ids, settings.default_kb_top_k
    )
    await core_facade.set_session_bundle_id(user_id, request, session_id, bundle.bundle_id)

    logger.info(
        f"[portal] user {user_id} bound session {session_id} to agent bundle "
        f"{bundle.bundle_id}"
    )
    return _consumer_entry(bundle)


async def unbind_session(user_id: str, request: Request, session_id: str) -> None:
    """Explicitly clear any bundle selection for an owned session,
    restoring the operator's own defaults on every dimension: the
    session's conversation persona resets to the operator's currently
    configured default persona, the per-umo provider preference is
    removed entirely, and the session's KB binding is reset to the
    explicit empty ``{"kb_ids": []}`` state.

    NOT gated by ``agent_bundle_selection_enabled``: disabling that master
    switch only stops NEW selections from being made (see
    ``PortalSettings.agent_bundle_selection_enabled``'s own docstring) --
    a user must always be able to clear back to the operator's plain
    default regardless of whether the picker feature itself is currently
    turned on. Also intentionally does not check whether a bundle is
    currently bound at all: this call is idempotent and unconditional,
    applying the exact same "no bundle" state every time it is called,
    exactly mirroring :func:`bind_session`'s own "every apply fully
    determines every dimension from scratch" contract (module docstring).
    """
    await core_facade.set_session_persona(user_id, request, session_id, None)
    await core_facade.set_session_provider(user_id, request, session_id, None)
    await core_facade.set_session_kb_config(
        user_id, request, session_id, [], get_portal_settings().default_kb_top_k
    )
    await core_facade.clear_session_bundle_id(user_id, request, session_id)
    logger.info(f"[portal] user {user_id} unbound agent bundle for session {session_id}")


# ---------------------------------------------------------------------------
# Admin-side CRUD. Operator-only: every function below trusts its caller
# (the admin routes, gated by ``require_system_scope`` imported verbatim
# from ``astrbot.dashboard.api.auth`` -- see this phase's engineering rules)
# to have already authenticated a real operator. None of these functions
# perform any authorization check of their own -- mirrors
# ``kb_visibility_service.py``'s own admin-side section exactly.
# ---------------------------------------------------------------------------


def _admin_entry(bundle: PortalAgentBundle, health: dict[str, Any]) -> dict[str, Any]:
    """Shape one bundle row for the operator's management page.

    Unlike :func:`_consumer_entry`, this DOES surface ``persona_id``/
    ``chat_provider_id``/``kb_ids`` (the operator configured them, they are
    not a secret from the operator) plus explicit dangling flags -- an
    operator needs to see and fix a broken reference; an end user must
    never be offered the same row at all (see :func:`list_available`).
    """
    return {
        "bundle_id": bundle.bundle_id,
        "name": bundle.name,
        "description": bundle.description,
        "persona_id": bundle.persona_id,
        "chat_provider_id": bundle.chat_provider_id,
        "kb_ids": health["kb_ids"],
        "enabled": bundle.enabled,
        "sort_order": bundle.sort_order,
        "is_default": bundle.is_default,
        "updated_by": bundle.updated_by,
        "created_at": bundle.created_at.isoformat(),
        "updated_at": bundle.updated_at.isoformat(),
        "persona_dangling": bundle.persona_id is not None and not health["persona_ok"],
        "provider_dangling": bundle.chat_provider_id is not None
        and not health["provider_ok"],
        "dangling_kb_ids": health["dangling_kb_ids"],
        "dangling": (not health["selectable"]) or bool(health["dangling_kb_ids"]),
    }


async def admin_list_all(request: Request) -> list[dict[str, Any]]:
    """Every ``PortalAgentBundle`` row (enabled or not), joined with live
    reference-health status -- including dangling rows (see
    :func:`_admin_entry`), unlike :func:`list_available`."""
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).order_by(
                col(PortalAgentBundle.sort_order), col(PortalAgentBundle.name)
            )
        )
        rows = list(result.scalars().all())
    entries: list[dict[str, Any]] = []
    for bundle in rows:
        health = await _bundle_health(request, bundle)
        entries.append(_admin_entry(bundle, health))
    return entries


async def admin_get(request: Request, bundle_id: str) -> dict[str, Any]:
    """A single bundle row (enabled or not) with live status, for an admin
    detail view. ``core_facade.ResourceNotFoundError`` if it does not
    exist."""
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).where(
                col(PortalAgentBundle.bundle_id) == str(bundle_id)
            )
        )
        bundle = result.scalar_one_or_none()
    if bundle is None:
        raise ResourceNotFoundError("Agent bundle not found")
    health = await _bundle_health(request, bundle)
    return _admin_entry(bundle, health)


async def admin_create(
    request: Request,
    *,
    name: str,
    description: str | None,
    persona_id: str | None,
    chat_provider_id: str | None,
    kb_ids: Sequence[str],
    enabled: bool = True,
    sort_order: int = 0,
    is_default: bool = False,
    updated_by: str | None,
) -> dict[str, Any]:
    """Create a new ``PortalAgentBundle`` row.

    Validates ``persona_id``/``chat_provider_id``/``kb_ids`` BEFORE writing
    anything (:func:`_validate_bundle_fields`) -- an operator can never
    save a bundle referencing a persona/provider that does not currently
    exist, or a ``kb_id`` that is not currently portal-visible; see module
    docstring's KB two-point-validation, point 1.
    """
    deduped_kb_ids = await _validate_bundle_fields(
        request,
        persona_id=persona_id,
        chat_provider_id=chat_provider_id,
        kb_ids=kb_ids,
    )
    bundle = PortalAgentBundle(
        name=name,
        description=description,
        persona_id=persona_id,
        chat_provider_id=chat_provider_id,
        kb_ids_json=json.dumps(deduped_kb_ids),
        enabled=enabled,
        sort_order=sort_order,
        is_default=is_default,
        updated_by=updated_by,
    )
    async with portal_async_sessionmaker() as session:
        session.add(bundle)
        await session.commit()
        await session.refresh(bundle)

    logger.info(
        f"[portal] admin {updated_by!r} created agent bundle {bundle.bundle_id} "
        f"({name!r})"
    )
    health = await _bundle_health(request, bundle)
    return _admin_entry(bundle, health)


async def admin_update(
    request: Request,
    bundle_id: str,
    *,
    name: str,
    description: str | None,
    persona_id: str | None,
    chat_provider_id: str | None,
    kb_ids: Sequence[str],
    enabled: bool,
    sort_order: int,
    is_default: bool,
    updated_by: str | None,
) -> dict[str, Any]:
    """Full-replace update of an existing bundle (PUT semantics, not PATCH):
    every mutable field is always overwritten by the caller-supplied value.

    Deliberately NOT a partial-update-with-``None``-means-unchanged
    contract (unlike ``kb_visibility_service.admin_update_display``):
    ``persona_id``/``chat_provider_id`` being ``None`` is itself a
    meaningful value on this model ("this bundle does not touch this
    dimension" -- see ``PortalAgentBundle``'s own field docstrings), so
    there is no spare sentinel value left over to also mean "leave
    unchanged" without an explicit unset-marker parameter. The admin route
    is expected to always submit the complete bundle (the natural shape of
    an edit form that already loaded the current values), exactly like a
    REST ``PUT``.

    Same create-time validation as :func:`admin_create` runs again here,
    against the NEW proposed values -- an update can just as easily
    introduce a dangling reference as a create can.
    """
    deduped_kb_ids = await _validate_bundle_fields(
        request,
        persona_id=persona_id,
        chat_provider_id=chat_provider_id,
        kb_ids=kb_ids,
    )
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).where(
                col(PortalAgentBundle.bundle_id) == str(bundle_id)
            )
        )
        bundle = result.scalar_one_or_none()
        if bundle is None:
            raise ResourceNotFoundError("Agent bundle not found")
        bundle.name = name
        bundle.description = description
        bundle.persona_id = persona_id
        bundle.chat_provider_id = chat_provider_id
        bundle.kb_ids_json = json.dumps(deduped_kb_ids)
        bundle.enabled = enabled
        bundle.sort_order = sort_order
        bundle.is_default = is_default
        bundle.updated_by = updated_by
        session.add(bundle)
        await session.commit()
        await session.refresh(bundle)

    logger.info(f"[portal] admin {updated_by!r} updated agent bundle {bundle.bundle_id}")
    health = await _bundle_health(request, bundle)
    return _admin_entry(bundle, health)


async def admin_set_enabled(
    request: Request, bundle_id: str, enabled: bool, *, updated_by: str | None
) -> dict[str, Any]:
    """Flip a bundle's ``enabled`` flag.

    Never deletes the row -- mirrors ``PortalKbVisibility.enabled``'s own
    docstring rationale (audit trail: ``created_at``/``updated_by``
    history survives a disable). Disabling a bundle a session already has
    bound leaves that session's already-applied persona/provider/KB state
    untouched (module docstring: disabling only affects new selections);
    it simply drops out of :func:`list_available` and can no longer be
    freshly selected via :func:`bind_session`.
    """
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAgentBundle).where(
                col(PortalAgentBundle.bundle_id) == str(bundle_id)
            )
        )
        bundle = result.scalar_one_or_none()
        if bundle is None:
            raise ResourceNotFoundError("Agent bundle not found")
        bundle.enabled = enabled
        bundle.updated_by = updated_by
        session.add(bundle)
        await session.commit()
        await session.refresh(bundle)

    logger.info(
        f"[portal] admin {updated_by!r} set agent bundle {bundle.bundle_id} "
        f"enabled={enabled}"
    )
    health = await _bundle_health(request, bundle)
    return _admin_entry(bundle, health)


__all__ = [
    "AgentBundleServiceError",
    "AgentBundleSelectionDisabledError",
    "TooManyKbIdsError",
    "PersonaNotFoundError",
    "ProviderNotFoundError",
    "KbNotVisibleError",
    "list_available",
    "get_session_binding",
    "bind_session",
    "unbind_session",
    "admin_list_all",
    "admin_get",
    "admin_create",
    "admin_update",
    "admin_set_enabled",
]
