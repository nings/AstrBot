"""Guardrail tests for the portal module's Phase 3 umo-routing startup
health check (``astrbot.portal.startup_checks``).

Covers the plan's Phase 3 acceptance criterion 3 verbatim: "the module
startup log shows the umo routing health check result; when a dedicated
isolation profile is deliberately left unconfigured, the log shows a
CRITICAL warning (test case injects the unconfigured scenario and asserts
the log contains the warning string)". Per this task's brief, the
acceptance criterion is explicitly about the *log*, so every failure/success
assertion below goes through ``caplog`` against the module's own stable,
documented log markers (``CRITICAL_LOG_MARKER``/``INFO_LOG_MARKER``) rather
than only checking the structured return value -- and separately confirms
the structured :class:`UmoRoutingCheckResult` is correct too, since a test
that only scraped the log could pass even if the returned data were wrong.

``check_umo_routing`` takes its ``UmopConfigRouter`` dependency purely by
duck-typed injection (see ``startup_checks.py``'s own module docstring on
why it avoids importing ``astrbot.core.core_lifecycle``), so every test
below supplies a tiny fake implementing just
``get_conf_id_for_umop(umo: str) -> str | None`` -- no real core wiring,
database, or app instance is needed for this module at all.
"""

from __future__ import annotations

import logging

import pytest

from astrbot.dashboard.services.chat_service import build_thread_unified_msg_origin
from astrbot.portal.startup_checks import (
    CRITICAL_LOG_MARKER,
    INFO_LOG_MARKER,
    PORTAL_REPRESENTATIVE_UMO,
    PortalUmoRoutingUnisolatedError,
    UmoRoutingCheckResult,
    build_representative_portal_umo,
    check_umo_routing,
)


class FakeUmopConfigRouter:
    """Duck-typed stand-in for ``astrbot.core.umop_config_router
    .UmopConfigRouter`` -- implements only the one method
    ``check_umo_routing`` calls (``UmopConfigRouterLike``), backed by a
    plain dict of exact-umo -> conf_id routes (fnmatch pattern-matching
    itself is the real router's own concern, not something this startup
    check exercises -- it only needs *a* result for one representative
    umo, matching or not)."""

    def __init__(self, routes: dict[str, str] | None = None) -> None:
        self.routes = dict(routes or {})
        self.calls: list[str] = []

    def get_conf_id_for_umop(self, umo: str) -> str | None:
        self.calls.append(umo)
        return self.routes.get(umo)


# ---------------------------------------------------------------------------
# 11. No dedicated route -> failure reported AND a CRITICAL log line with a
#     stable, assertable substring.
# ---------------------------------------------------------------------------


def test_missing_route_reports_failure_and_logs_critical(caplog: pytest.LogCaptureFixture):
    router = FakeUmopConfigRouter(routes={})  # no route configured at all

    with caplog.at_level(logging.CRITICAL, logger="astrbot"):
        result = check_umo_routing(router)

    assert isinstance(result, UmoRoutingCheckResult)
    assert result.is_isolated is False
    assert result.ok is False
    assert result.resolved_conf_id is None
    assert result.umo == PORTAL_REPRESENTATIVE_UMO

    assert CRITICAL_LOG_MARKER in caplog.text, (
        "the plan's acceptance criterion is explicitly about the log -- a "
        "missing isolation route must produce a CRITICAL log line "
        "containing this stable, documented marker substring"
    )
    assert any(record.levelno == logging.CRITICAL for record in caplog.records)
    # The success marker must NOT also appear -- these are meant to be
    # mutually exclusive, distinguishable-by-substring outcomes.
    assert INFO_LOG_MARKER not in caplog.text


def test_missing_route_critical_log_names_actionable_remediation(
    caplog: pytest.LogCaptureFixture,
):
    """Beyond the bare marker, the CRITICAL line should be genuinely
    actionable for an operator reading a startup log cold -- it names the
    concrete risk (SubAgentOrchestrator/tool exposure) and a copy-pasteable
    remediation call, not just "something is wrong"."""
    router = FakeUmopConfigRouter(routes={})

    with caplog.at_level(logging.CRITICAL, logger="astrbot"):
        check_umo_routing(router)

    assert "SubAgentOrchestrator" in caplog.text
    assert "update_route" in caplog.text


# ---------------------------------------------------------------------------
# 12. A dedicated route -> success reported AND the resolved profile is
#     logged too -- the result must appear on every start, not only on
#     failure.
# ---------------------------------------------------------------------------


def test_dedicated_route_reports_success_and_logs_resolution(
    caplog: pytest.LogCaptureFixture,
):
    router = FakeUmopConfigRouter(routes={PORTAL_REPRESENTATIVE_UMO: "portal-isolated-profile"})

    with caplog.at_level(logging.INFO, logger="astrbot"):
        result = check_umo_routing(router)

    assert result.is_isolated is True
    assert result.ok is True
    assert result.resolved_conf_id == "portal-isolated-profile"
    assert result.umo == PORTAL_REPRESENTATIVE_UMO

    assert INFO_LOG_MARKER in caplog.text, (
        "the check's result must be visible in the log on EVERY start, per "
        "the plan's acceptance criterion -- not only when it fails"
    )
    assert "portal-isolated-profile" in caplog.text, (
        "the resolved profile id itself must be named in the log line, not "
        "just a bare pass/fail"
    )
    assert any(record.levelno == logging.INFO for record in caplog.records)
    # The failure marker must NOT also appear on the success path.
    assert CRITICAL_LOG_MARKER not in caplog.text
    assert not any(record.levelno == logging.CRITICAL for record in caplog.records)


# ---------------------------------------------------------------------------
# 13. refuse_to_start is off by default; when enabled, it raises on failure
#     (and never raises on success even if enabled).
# ---------------------------------------------------------------------------


def test_refuse_to_start_defaults_off_and_does_not_raise_on_missing_route():
    router = FakeUmopConfigRouter(routes={})
    # Must not raise -- refuse_to_start's default is False.
    result = check_umo_routing(router)
    assert result.is_isolated is False


def test_refuse_to_start_true_raises_on_missing_route(caplog: pytest.LogCaptureFixture):
    router = FakeUmopConfigRouter(routes={})

    with caplog.at_level(logging.CRITICAL, logger="astrbot"):
        with pytest.raises(PortalUmoRoutingUnisolatedError) as exc_info:
            check_umo_routing(router, refuse_to_start=True)

    assert CRITICAL_LOG_MARKER in str(exc_info.value)
    # The CRITICAL log line must have been emitted BEFORE the raise, not
    # skipped in favor of the exception alone.
    assert CRITICAL_LOG_MARKER in caplog.text


def test_refuse_to_start_true_does_not_raise_when_isolated():
    router = FakeUmopConfigRouter(routes={PORTAL_REPRESENTATIVE_UMO: "isolated-profile"})
    # Even with the strict flag on, a passing check must never raise.
    result = check_umo_routing(router, refuse_to_start=True)
    assert result.is_isolated is True


# ---------------------------------------------------------------------------
# Extra: confirm the representative umo really does have the exact shape
# real portal traffic uses, rather than merely asserting the module's own
# docstring claims it does. build_thread_unified_msg_origin (dashboard's
# chat_service.py) is the one other place in the codebase that builds this
# same template; startup_checks.py deliberately does NOT import it (module
# docstring: avoiding a core_lifecycle-adjacent dependency), so this test is
# the only thing pinning the two implementations stay byte-for-byte
# identical.
# ---------------------------------------------------------------------------


def test_representative_umo_matches_real_webchat_umo_template():
    expected = build_thread_unified_msg_origin(
        "__portal_startup_check__", "__portal_startup_check__"
    )
    assert PORTAL_REPRESENTATIVE_UMO == expected
    assert build_representative_portal_umo("some-user", "some-thread") == (
        build_thread_unified_msg_origin("some-user", "some-thread")
    )


def test_representative_umo_uses_custom_creator_and_thread_id_when_given():
    custom = build_representative_portal_umo(creator="alice", thread_id="thread-1")
    assert custom == "webchat:FriendMessage:webchat!alice!thread-1"
    assert custom != PORTAL_REPRESENTATIVE_UMO


# ---------------------------------------------------------------------------
# A representative umo that DOES match via a wildcard-style route (as a real
# deployment would configure, per the module's own docstring on why a
# fixed-placeholder umo is representative enough) is still detected as
# isolated -- this isn't exercised above only because FakeUmopConfigRouter
# does exact-key lookup, not fnmatch; this test proves the check's own logic
# has no opinion on how the router internally matched, only on what it
# returned.
# ---------------------------------------------------------------------------


def test_check_is_indifferent_to_how_the_router_matched_only_to_the_result():
    router = FakeUmopConfigRouter(routes={PORTAL_REPRESENTATIVE_UMO: "wildcard-matched-profile"})
    result = check_umo_routing(router)
    assert result.resolved_conf_id == "wildcard-matched-profile"
    assert router.calls == [PORTAL_REPRESENTATIVE_UMO]
