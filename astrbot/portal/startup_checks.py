"""Portal startup health checks (Phase 3).

This module ships exactly one check today: does portal traffic route to a
dedicated, isolated ``AstrBotConfig`` profile, or does it fall through to
the operator's default profile?

Why this check exists (see design/portal-multiuser-plan.md section 4's
security checklist, item 9, and Phase 3's deliverable list)
----------------------------------------------------------------------------
``SubAgentOrchestrator`` is a single global configuration with no per-umo
tool authorization (verified ground truth). Whatever tools/subagents an
operator has enabled globally -- including anything under
``astrbot/core/computer/`` and ``astrbot/core/tools/`` -- are reachable by
*any* tool-calling agent turn, regardless of which umo triggered it. Portal
end-user text is untrusted by construction (it is arbitrary text typed by an
anonymous or lightly-authenticated web visitor), yet it reaches that same
tool-calling agent unless the operator has routed portal traffic to a
separate ``AstrBotConfig`` profile with dangerous tools and subagents turned
off in that profile specifically.

This module cannot enforce that separation -- it has no access to, and no
business editing, the operator's ``AstrBotConfig`` profiles or the
subagent/tool allowlists inside them. What it *can* do, and the only
mitigation this phase has for a risk it cannot enforce in code, is make the
absence of that separation impossible to miss: log the routing outcome on
every single startup (success or failure, per the plan's acceptance
criterion 3 -- "the module startup log shows the umo routing health check
result"), at CRITICAL severity with an unmissable, specific message when
isolation is absent, and return a structured result so a test can assert on
the outcome without scraping log text.

Capability wall / dependency-injection note
----------------------------------------------------------------------------
The live ``UmopConfigRouter`` instance lives at
``core_lifecycle.umop_config_router`` (``astrbot/core/core_lifecycle.py``,
around line 166). This module deliberately does **not** import
``astrbot.core.core_lifecycle`` to reach it -- that module transitively pulls
in most of AstrBot's core wiring (``ProviderManager``, ``PluginManager``,
``SubAgentOrchestrator``, ...) just to hand back one attribute, which is far
more surface than a pure startup check needs and is exactly the kind of
needless core coupling the portal's capability wall exists to prevent (see
``astrbot/portal/services/core_facade.py``'s module docstring and
``tests/test_portal_capability_wall.py``). Instead, the router is taken by
**dependency injection**: :func:`check_umo_routing` accepts anything shaped
like ``UmopConfigRouter`` (see :class:`UmopConfigRouterLike`) as a plain
argument. The caller -- wherever the integrator wires this into app startup
(``astrbot/dashboard/api/app.py``, not owned by this module) -- is expected
to already hold a reference to the live ``core_lifecycle`` object and pass
``core_lifecycle.umop_config_router`` straight through, e.g.::

    from astrbot.portal.startup_checks import check_umo_routing

    check_umo_routing(core_lifecycle.umop_config_router)

This keeps ``startup_checks.py`` importing nothing from
``astrbot.core`` except :class:`astrbot.core.platform.message_type.MessageType`
(a standalone, dependency-free enum used only to build the representative
umo string in the exact shape real portal umos take -- see
:data:`PORTAL_REPRESENTATIVE_UMO` below). Nothing this module imports is on
the capability wall's forbidden list
(``astrbot.dashboard.services.chat_service``,
``astrbot.core.conversation_mgr``, ``astrbot.core.persona_mgr``,
``astrbot.dashboard.services.conversation_service``,
``astrbot.dashboard.services.session_management_service``,
``astrbot.dashboard.services.api_key_service``,
``astrbot.core.subagent_orchestrator``, or
``astrbot.core.utils.shared_preferences``), and this module never imports
``astrbot.core.core_lifecycle`` either, so no indirect exception is needed.

The representative umo
----------------------------------------------------------------------------
Every real portal umo has the exact shape built at
``astrbot/dashboard/services/chat_service.py:312``::

    webchat:{MessageType.FRIEND_MESSAGE.value}:webchat!{creator}!{thread_id}

where ``creator`` is the authenticated portal ``user_id`` and ``thread_id``
is a per-conversation identifier. ``UmopConfigRouter.get_conf_id_for_umop``
resolves a route by matching each of the three ``:``-separated segments
against the operator's configured patterns with ``fnmatch`` (empty segments
in a pattern also match anything -- see
``astrbot/core/umop_config_router.py``'s ``_is_umo_match``). A deployment
that isolates portal traffic will therefore set a route whose pattern covers
*all* webchat-portal umos (e.g. ``webchat:FriendMessage:webchat!*!*`` or
something broader), not one keyed to a single real user's exact identity --
so a fixed placeholder ``creator``/``thread_id`` resolves through that
pattern-matching exactly as any real portal umo would. This is why a single
representative umo, built once at import time with placeholder identifiers,
is sufficient to answer "does portal traffic, as a class, have a dedicated
route" without needing a live end-user session to test against.

(The one case this cannot detect: an operator who routed one specific real
user's exact umo to an isolated profile and left every other portal umo
unrouted. That is not "portal traffic is isolated" in any meaningful sense
-- every *other* user would still fall through to the default profile -- so
this check correctly reports "not isolated" for that deployment, which is
the safe, conservative direction to be wrong in.)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from astrbot import logger
from astrbot.core.platform.message_type import MessageType

# ---------------------------------------------------------------------------
# The representative umo. Built once, module-level, from the exact template
# astrbot/dashboard/services/chat_service.py:312 uses for real portal umos.
# ---------------------------------------------------------------------------

PORTAL_UMO_PLATFORM_ID = "webchat"
PORTAL_UMO_MESSAGE_TYPE = MessageType.FRIEND_MESSAGE.value
PORTAL_UMO_PLACEHOLDER_CREATOR = "__portal_startup_check__"
PORTAL_UMO_PLACEHOLDER_THREAD_ID = "__portal_startup_check__"


def build_representative_portal_umo(
    creator: str = PORTAL_UMO_PLACEHOLDER_CREATOR,
    thread_id: str = PORTAL_UMO_PLACEHOLDER_THREAD_ID,
) -> str:
    """Build a umo in the exact shape real portal traffic uses.

    Matches ``astrbot/dashboard/services/chat_service.py:312``'s
    ``build_webchat_unified_msg_origin`` byte-for-byte in template (not
    imported from there -- that module sits behind the capability wall for
    unrelated reasons, and duplicating one f-string is far cheaper than
    taking a dependency on it). Only the ``creator``/``thread_id`` values
    differ from a real umo; see this module's docstring for why placeholder
    values are representative enough for this check's purpose.
    """
    return f"{PORTAL_UMO_PLATFORM_ID}:{PORTAL_UMO_MESSAGE_TYPE}:{PORTAL_UMO_PLATFORM_ID}!{creator}!{thread_id}"


PORTAL_REPRESENTATIVE_UMO = build_representative_portal_umo()
"""Module-level default representative umo, used by :func:`check_umo_routing`
when the caller does not supply its own."""


# ---------------------------------------------------------------------------
# Structural type for the router dependency. Deliberately NOT
# ``astrbot.core.umop_config_router.UmopConfigRouter`` itself -- importing
# that class would be harmless per the import-linter contracts (it is not on
# the forbidden list), but this module has no need for anything beyond the
# one method it calls, and a ``Protocol`` keeps that dependency exactly as
# narrow as the injection this module's assignment calls for. Any object
# with this method -- the real ``UmopConfigRouter``, or a test fake -- works.
# ---------------------------------------------------------------------------


class UmopConfigRouterLike(Protocol):
    def get_conf_id_for_umop(self, umo: str) -> str | None: ...


# ---------------------------------------------------------------------------
# Result type + the stable, assertable log substring.
# ---------------------------------------------------------------------------

CRITICAL_LOG_MARKER = "PORTAL_UMO_ROUTING_NOT_ISOLATED"
"""Stable substring guaranteed to appear in the CRITICAL log line whenever
the check fails. The plan's Phase 3 acceptance criterion 3 requires a test
to inject an unconfigured-routing scenario and assert the startup log
contains a CRITICAL warning string -- assert on this constant, not on the
full (deliberately verbose, may be reworded) message."""

INFO_LOG_MARKER = "PORTAL_UMO_ROUTING_ISOLATED"
"""Stable substring in the INFO log line on success, so a test (or a log
scraper / alerting rule) can assert the check ran and passed just as
reliably as it can assert a failure. The plan's acceptance criterion is that
the check's result appears in the log on *every* start, not only on
failure -- this marker exists on both branches for that reason."""


@dataclass(frozen=True)
class UmoRoutingCheckResult:
    """Structured outcome of :func:`check_umo_routing`, independent of the
    log line, so tests (and any future programmatic startup gate) can
    assert on data rather than parsing log text."""

    umo: str
    """The representative umo that was resolved."""

    resolved_conf_id: str | None
    """The ``AstrBotConfig`` profile id ``UmopConfigRouter`` resolved
    ``umo`` to, or ``None`` if no explicit route matched (i.e. portal
    traffic falls through to the default profile)."""

    is_isolated: bool
    """``True`` iff ``resolved_conf_id`` is not ``None``. Note this only
    proves an explicit route exists -- it cannot prove the *target*
    profile actually has dangerous tools/subagents disabled, since this
    module has no access to profile contents. See the module docstring's
    'cannot enforce, can only make loud' framing."""

    @property
    def ok(self) -> bool:
        """Alias for ``is_isolated``, for callers that prefer a pass/fail
        read without reaching for the more precisely-named field."""
        return self.is_isolated


class PortalUmoRoutingUnisolatedError(RuntimeError):
    """Raised by :func:`check_umo_routing` only when called with
    ``refuse_to_start=True`` and the check fails.

    Deliberately a distinct exception type (not a bare ``RuntimeError``) so
    the integrator's startup code can catch this specifically -- e.g. to
    log-and-exit with a clean message -- without accidentally swallowing an
    unrelated ``RuntimeError`` raised elsewhere during startup.
    """


# ---------------------------------------------------------------------------
# The check itself.
# ---------------------------------------------------------------------------


def check_umo_routing(
    umop_config_router: UmopConfigRouterLike,
    *,
    representative_umo: str = PORTAL_REPRESENTATIVE_UMO,
    refuse_to_start: bool = False,
) -> UmoRoutingCheckResult:
    """Resolve ``representative_umo`` through ``umop_config_router`` and log
    + return the isolation outcome.

    Always logs exactly one line -- INFO on success, CRITICAL on failure --
    so the check's result is visible in the startup log on every start, per
    the plan's Phase 3 acceptance criterion 3, not only when it fails.

    Args:
        umop_config_router: Anything shaped like ``UmopConfigRouter`` (see
            :class:`UmopConfigRouterLike`). Pass the live
            ``core_lifecycle.umop_config_router`` instance; see this
            module's docstring for why it must be injected rather than
            imported here.
        representative_umo: Defaults to :data:`PORTAL_REPRESENTATIVE_UMO`.
            Overridable for tests that want to exercise a specific pattern
            match, or for a deployment with a non-default portal platform
            id (not currently a thing, but the hook costs nothing).
        refuse_to_start: Operator-configurable "fail closed instead of
            warn" mode. **Defaults to ``False``** -- deliberately, because
            silently breaking an already-running operator's startup on
            upgrade (turning a loud warning most operators would act on
            into a hard outage) is a worse failure mode than the warning
            itself for a brand-new mitigation being introduced into an
            existing deployment. Deployments that would rather fail closed
            than run mis-routed should pass ``True`` explicitly. This
            module does not read that flag from anywhere itself (it has no
            settings source of its own -- see ``notes_for_integrator`` in
            this phase's report for exactly how the integrator should wire
            an operator-facing knob for it).

    Returns:
        A :class:`UmoRoutingCheckResult` describing the outcome. Always
        returned, even in ``refuse_to_start=True`` failure mode -- the
        exception is raised *after* logging, so a caller catching
        :class:`PortalUmoRoutingUnisolatedError` still has the result
        available via the exception's context if it wants to inspect it,
        though in the normal case the caller simply lets the exception
        propagate and the process exits.

    Raises:
        PortalUmoRoutingUnisolatedError: Only when ``refuse_to_start`` is
            ``True`` and no explicit route was found for
            ``representative_umo``.
    """
    resolved_conf_id = umop_config_router.get_conf_id_for_umop(representative_umo)
    is_isolated = resolved_conf_id is not None
    result = UmoRoutingCheckResult(
        umo=representative_umo,
        resolved_conf_id=resolved_conf_id,
        is_isolated=is_isolated,
    )

    if is_isolated:
        logger.info(
            f"[portal] {INFO_LOG_MARKER}: umo routing health check passed -- "
            f"portal traffic (representative umo '{representative_umo}') "
            f"resolves to dedicated AstrBotConfig profile '{resolved_conf_id}'. "
            "This check cannot itself verify that profile has dangerous "
            "tools/subagents disabled -- confirm that manually in the "
            f"'{resolved_conf_id}' profile's configuration."
        )
        return result

    logger.critical(
        f"[portal] {CRITICAL_LOG_MARKER}: umo routing health check FAILED -- "
        f"portal traffic (representative umo '{representative_umo}') has NO "
        "explicit route in UmopConfigRouter and falls through to the "
        "DEFAULT AstrBotConfig profile. SubAgentOrchestrator and its tool "
        "allowlist are global with no per-umo authorization, so untrusted "
        "portal end-user text is reaching a tool-calling agent under "
        "whatever tools/subagents the operator enabled globally -- including "
        "anything under astrbot/core/computer/ and astrbot/core/tools/. "
        "ACTION REQUIRED: create a dedicated AstrBotConfig profile with "
        "dangerous tools and subagents disabled, then route portal traffic "
        "to it, e.g. "
        f"await umop_config_router.update_route('{PORTAL_UMO_PLATFORM_ID}:"
        f"{PORTAL_UMO_MESSAGE_TYPE}:{PORTAL_UMO_PLATFORM_ID}!*!*', "
        "'<isolated-profile-id>') (or the equivalent dashboard umo-routing "
        "admin UI), scoped broadly enough to cover every portal umo, not "
        "just one user."
    )

    if refuse_to_start:
        raise PortalUmoRoutingUnisolatedError(
            f"{CRITICAL_LOG_MARKER}: refuse_to_start is enabled and portal "
            "traffic has no dedicated, isolated AstrBotConfig routing. "
            "Refusing to start. See the preceding CRITICAL log line for "
            "what to configure, or disable refuse_to_start to start anyway "
            "with only the warning."
        )

    return result


__all__ = [
    "PORTAL_UMO_PLATFORM_ID",
    "PORTAL_UMO_MESSAGE_TYPE",
    "PORTAL_REPRESENTATIVE_UMO",
    "CRITICAL_LOG_MARKER",
    "INFO_LOG_MARKER",
    "UmopConfigRouterLike",
    "UmoRoutingCheckResult",
    "PortalUmoRoutingUnisolatedError",
    "build_representative_portal_umo",
    "check_umo_routing",
]
