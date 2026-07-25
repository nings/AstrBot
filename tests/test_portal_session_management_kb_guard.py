"""Regression tests for the two Phase 3 blockers in core's session-rule route,
and for the registration design that closes them without inverting the
core-to-portal dependency.

The blockers, both in ``astrbot.dashboard.services.session_management_service``
and both reachable with only ``require_data_scope`` -- a narrower tier than the
``require_system_scope`` the portal requires for its own KB admin CRUD:

1. ``update_session_rule`` (``POST /api/v1/sessions/rules``) wrote ``kb_config``
   verbatim with no whitelist, count or top_k validation, so a data-scoped
   credential could bind any kb_id -- including operator-internal corpora -- to
   any known portal session.
2. ``delete_session_rule``/``batch_delete_session_rule`` removed the key
   outright. ``retrieve_knowledge_base``
   (``astrbot/core/tools/knowledge_base_tools.py``) reads a *missing*
   ``kb_config`` as "use the operator's global ``kb_names``", so deleting it
   re-opened the default-open KB leak on demand.

How they are closed matters as much as that they are. Core exposes a
``SessionRuleGuard`` registration hook and asks its registered guards; the
portal supplies one at startup (``astrbot/portal/session_rule_guard.py``,
registered from ``app.py``'s lifespan). Two properties follow, and this suite
pins both:

* **Core imports nothing from the portal.** A deployment that does not run the
  portal registers no guard and behaves exactly as it did before Phase 3.
* **Identification is exact, not by platform.** The operator's own dashboard
  test chat also lives on the ``webchat`` platform, so a platform-id check
  would over-match and silently take away the operator's ability to configure
  ``kb_config`` for their own sessions. The guard matches on whether the umo's
  creator is a real ``PortalUser``.

Follows the established convention of exercising the real process-wide
``astrbot.core.sp`` singleton rather than mocking it, with a fresh uuid in every
umo so tests cannot collide on the shared keyspace.

---

**Phase 4 extension.** The guard (still named ``PortalKbConfigGuard`` --
unchanged from Phase 3 for import-compatibility reasons documented in its own
module docstring) now also claims two more keys reachable through the same
route:

3. ``session_service_config`` -- its ``persona_id`` sub-key is a
   **force-applied** persona (``PersonaManager.resolve_selected_persona``,
   ``astrbot/core/persona_mgr.py``, reads it *before* falling back to the
   conversation's own persona). This is the exact surface Phase 4's Agent
   Bundle feature would otherwise be shadowed through: a data-scope caller
   could force an arbitrary persona onto a Portal session regardless of
   whatever bundle the end user selected (which applies its persona via
   ``conv_mgr.update_conversation``, a different, lower-priority surface).
4. ``provider_perf_chat_completion`` -- the per-umo chat-provider preference
   (``ProviderManager.get_using_provider`` reads it before the operator's own
   ``default_provider_id``). This is the "spend-redirection bypass": left
   open, a data-scope caller could point a Portal session's spend at any
   provider configured on the instance, entirely bypassing the Agent
   Bundle's own resolution.

Per-key ``reset`` semantics differ (see ``session_rule_guard.py``'s own
module docstring for the full rationale):

* ``session_service_config`` is refused WHOLESALE at write time -- the
  ``SessionRuleGuard`` protocol never hands ``owns`` the value being
  written, so there is no way to claim the key "only when the write
  touches persona_id"; claiming it at all necessarily refuses every write
  through this route for this key, including one that only meant to touch
  some other sub-key. ``reset`` (the delete path), however, IS surgical: it
  removes only the ``persona_id`` sub-key, leaving
  ``llm_enabled``/``tts_enabled``/every other sub-key untouched.
* ``provider_perf_chat_completion`` -- absence is SAFE (``get_using_provider``
  already falls through to the operator's own default), so ``reset``
  performs a plain removal, mirroring ``kb_config``'s Phase-3 write-refusal
  shape but NOT its "reset writes an explicit safe value" shape.

``session_rule_guard.py``'s ``reset()`` for both new keys calls
``core_facade.reset_session_persona_override``/``core_facade.
clear_chat_provider_preference_for_umo`` -- both now implemented in
``core_facade.py`` (added during Phase 4 verification, alongside the
existing ``write_default_kb_config_for_umo``), so the delete-path tests for
these two keys (``test_delete_session_rule_session_service_config_resets_persona_only``
and ``test_delete_session_rule_provider_perf_chat_completion_is_cleared``)
exercise the real reset behaviour end-to-end rather than being xfailed.
"""

from __future__ import annotations

import uuid
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.core import sp
from astrbot.dashboard.services import session_management_service as sms
from astrbot.dashboard.services.session_management_service import (
    SessionManagementService,
    SessionManagementServiceError,
)
from astrbot.portal import session_rule_guard
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.models import PortalBaseModel, PortalUser
from astrbot.portal.services import core_facade

_MISSING = object()
"""Sentinel for "this sp key was never written", distinct from any falsy
``kb_config`` value a test might otherwise mistake for absence."""


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-rule-guard-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    maker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield maker
    finally:
        await engine.dispose()


async def _insert_portal_user(maker, user_id: str) -> None:
    async with maker() as session:
        session.add(
            PortalUser(
                user_id=user_id,
                email=f"{user_id}@example.com",
                password_hash="x",
                display_name=user_id,
                auth_method="local",
                role="user",
                status="active",
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def portal_user(portal_sessionmaker, monkeypatch) -> str:
    """A real PortalUser, with the guard and facade pointed at the temp DB."""
    user_id = f"pu-{uuid.uuid4().hex}"
    await _insert_portal_user(portal_sessionmaker, user_id)
    monkeypatch.setattr(
        session_rule_guard, "portal_async_sessionmaker", portal_sessionmaker
    )
    monkeypatch.setattr(core_facade, "portal_async_sessionmaker", portal_sessionmaker)
    return user_id


@pytest_asyncio.fixture
async def registered_guard(portal_user: str):
    """Register the portal's guard exactly as app.py's lifespan does, and drop
    it afterwards so no other test inherits process-wide state."""
    sms.clear_session_rule_guards()
    session_rule_guard.register(sms.register_session_rule_guard)
    try:
        yield portal_user
    finally:
        sms.clear_session_rule_guards()


@pytest.fixture
def no_guard():
    """No guard registered -- a deployment not running the portal."""
    sms.clear_session_rule_guards()
    yield
    sms.clear_session_rule_guards()


@pytest.fixture
def service() -> SessionManagementService:
    # The update/delete/batch-delete rule methods operate purely against the
    # module-level `sp` singleton and an explicit `umos` list, so neither
    # collaborator is touched.
    return SessionManagementService(core_lifecycle=MagicMock(), db_helper=MagicMock())


def _portal_umo(user_id: str) -> str:
    """A umo shaped like a real portal session's, with a real portal user as
    its creator -- see chat_service's umo builder."""
    return f"webchat:FriendMessage:webchat!{user_id}!{uuid.uuid4().hex}"


def _operator_testchat_umo() -> str:
    """A WebChat umo whose creator is NOT a portal user: the operator's own
    built-in test chat. Shares the platform id with portal sessions, which is
    precisely why a platform-id check is not good enough."""
    return (
        f"webchat:FriendMessage:webchat!operator-{uuid.uuid4().hex}!{uuid.uuid4().hex}"
    )


def _non_portal_umo() -> str:
    return f"aiocqhttp:GroupMessage:aiocqhttp!{uuid.uuid4().hex}!{uuid.uuid4().hex}"


async def _get_kb_config(umo: str):
    return await sp.session_get(umo, "kb_config", default=_MISSING)


def _default_kb_config() -> dict:
    return {"kb_ids": [], "top_k": get_portal_settings().default_kb_top_k}


_ATTACKER_BINDING = {"kb_ids": ["kb-not-whitelisted"], "top_k": 999}


# ---------------------------------------------------------------------------
# Identification
# ---------------------------------------------------------------------------


def test_extract_portal_user_id_parses_only_webchat_umos():
    uid = "abc123"
    assert (
        session_rule_guard.extract_portal_user_id(
            f"webchat:FriendMessage:webchat!{uid}!t1"
        )
        == uid
    )
    assert session_rule_guard.extract_portal_user_id(_non_portal_umo()) is None
    assert (
        session_rule_guard.extract_portal_user_id("telegram:FriendMessage:123") is None
    )
    assert session_rule_guard.extract_portal_user_id("webchat:FriendMessage:nope") is None
    assert session_rule_guard.extract_portal_user_id("") is None


@pytest.mark.asyncio
async def test_guard_claims_kb_config_only_for_real_portal_users(portal_user: str):
    guard = session_rule_guard.PortalKbConfigGuard()

    assert await guard.owns(_portal_umo(portal_user), "kb_config") is True
    # A different rule key on the same umo is not ours.
    assert await guard.owns(_portal_umo(portal_user), "session_plugin_config") is False


@pytest.mark.asyncio
async def test_guard_does_not_claim_operator_testchat_sessions(portal_user: str):
    """The regression a platform-id check would have introduced.

    The operator's dashboard test chat is on the ``webchat`` platform too. If
    the guard matched on platform alone it would refuse the operator's own
    ``kb_config`` writes -- removing functionality they had before Phase 3, for
    a session no end user can reach.
    """
    guard = session_rule_guard.PortalKbConfigGuard()
    assert await guard.owns(_operator_testchat_umo(), "kb_config") is False
    assert await guard.owns(_non_portal_umo(), "kb_config") is False


# ---------------------------------------------------------------------------
# Blocker 1: unvalidated writes are refused
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_session_rule_refuses_kb_config_for_portal_session(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    baseline = await _get_kb_config(umo)

    with pytest.raises(SessionManagementServiceError):
        await service.update_session_rule(
            {"umo": umo, "rule_key": "kb_config", "rule_value": _ATTACKER_BINDING}
        )

    assert await _get_kb_config(umo) == baseline, (
        "a refused write must not have mutated sp at all"
    )


@pytest.mark.asyncio
async def test_update_session_rule_still_allows_operator_testchat_kb_config(
    service: SessionManagementService, registered_guard: str
):
    """Operators keep control of their own sessions -- the point of matching on
    the creator rather than the platform."""
    umo = _operator_testchat_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "kb_config",
            "rule_value": {"kb_ids": ["kb-x"], "top_k": 3},
        }
    )
    assert await _get_kb_config(umo) == {"kb_ids": ["kb-x"], "top_k": 3}


@pytest.mark.asyncio
async def test_update_session_rule_unaffected_for_other_platforms(
    service: SessionManagementService, registered_guard: str
):
    umo = _non_portal_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "kb_config",
            "rule_value": {"kb_ids": ["kb-y"], "top_k": 2},
        }
    )
    assert await _get_kb_config(umo) == {"kb_ids": ["kb-y"], "top_k": 2}


# ---------------------------------------------------------------------------
# Blocker 2: deletes write the safe binding instead of removing the key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_rule_writes_empty_binding_not_absence(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    await sp.session_put(umo, "kb_config", {"kb_ids": ["kb-a"], "top_k": 4})

    await service.delete_session_rule({"umo": umo, "rule_key": "kb_config"})

    assert await _get_kb_config(umo) == _default_kb_config(), (
        "the key must hold an explicit empty binding; absence would fall "
        "through to the operator's global kb_names"
    )


@pytest.mark.asyncio
async def test_delete_all_rules_reinstates_the_safe_binding(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    await sp.session_put(umo, "kb_config", {"kb_ids": ["kb-a"], "top_k": 4})

    # No rule_key -> the "clear every preference for this umo" branch.
    await service.delete_session_rule({"umo": umo})

    assert await _get_kb_config(umo) == _default_kb_config()


@pytest.mark.asyncio
async def test_batch_delete_targeted_key_mixed_umos(
    service: SessionManagementService, registered_guard: str
):
    portal = _portal_umo(registered_guard)
    other = _non_portal_umo()
    await sp.session_put(portal, "kb_config", {"kb_ids": ["kb-a"], "top_k": 4})
    await sp.session_put(other, "kb_config", {"kb_ids": ["kb-b"], "top_k": 4})

    await service.delete_session_rules(
        {"umos": [portal, other], "rule_key": "kb_config"}
    )

    assert await _get_kb_config(portal) == _default_kb_config()
    assert await _get_kb_config(other) is _MISSING, (
        "non-portal umos keep the original remove-the-key behaviour"
    )


@pytest.mark.asyncio
async def test_batch_delete_full_clear_mixed_umos(
    service: SessionManagementService, registered_guard: str
):
    portal = _portal_umo(registered_guard)
    other = _non_portal_umo()
    await sp.session_put(portal, "kb_config", {"kb_ids": ["kb-a"], "top_k": 4})
    await sp.session_put(other, "kb_config", {"kb_ids": ["kb-b"], "top_k": 4})

    await service.delete_session_rules({"umos": [portal, other]})

    assert await _get_kb_config(portal) == _default_kb_config()
    assert await _get_kb_config(other) is _MISSING


# ---------------------------------------------------------------------------
# Phase 4 extension: identification for the two new keys.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_guard_claims_new_keys_only_for_real_portal_users(portal_user: str):
    guard = session_rule_guard.PortalKbConfigGuard()

    assert await guard.owns(_portal_umo(portal_user), "session_service_config") is True
    assert (
        await guard.owns(_portal_umo(portal_user), "provider_perf_chat_completion")
        is True
    )
    # Same regression this file already pins for kb_config: the operator's
    # own test chat (same webchat platform, different -- non-portal --
    # creator) must not be claimed for either new key either.
    assert (
        await guard.owns(_operator_testchat_umo(), "session_service_config") is False
    )
    assert (
        await guard.owns(_operator_testchat_umo(), "provider_perf_chat_completion")
        is False
    )
    assert await guard.owns(_non_portal_umo(), "session_service_config") is False
    assert (
        await guard.owns(_non_portal_umo(), "provider_perf_chat_completion") is False
    )
    # A rule key this guard does not claim at all is still correctly refused
    # for a real portal umo, even after widening to three keys.
    assert (
        await guard.owns(_portal_umo(portal_user), "session_plugin_config") is False
    )


# ---------------------------------------------------------------------------
# Blocker 3: session_service_config is refused WHOLESALE for portal
# sessions -- the persona-shadowing bypass Phase 4's Agent Bundle feature
# needs closed (a data-scope caller could otherwise force
# session_service_config['persona_id'] and override a bundle's own
# conv_mgr-applied persona).
# ---------------------------------------------------------------------------


async def _get_session_service_config(umo: str):
    return await sp.session_get(umo, "session_service_config", default=_MISSING)


@pytest.mark.asyncio
async def test_update_session_rule_refuses_session_service_config_for_portal_session(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    baseline = await _get_session_service_config(umo)

    with pytest.raises(SessionManagementServiceError):
        await service.update_session_rule(
            {
                "umo": umo,
                "rule_key": "session_service_config",
                "rule_value": {"persona_id": "attacker-forced-persona"},
            }
        )

    assert await _get_session_service_config(umo) == baseline, (
        "a refused write must not have mutated sp at all"
    )


@pytest.mark.asyncio
async def test_update_session_rule_refuses_session_service_config_even_for_non_persona_write(
    service: SessionManagementService, registered_guard: str
):
    """The guard's own documented cost (module docstring, ``session_rule_guard.py``):
    it cannot distinguish a persona-touching write from any other write to
    this key, because ``owns`` is never handed the ``rule_value``. This test
    pins that documented, deliberate over-refusal rather than assuming a
    narrower, unimplementable scope."""
    umo = _portal_umo(registered_guard)
    baseline = await _get_session_service_config(umo)

    with pytest.raises(SessionManagementServiceError):
        await service.update_session_rule(
            {
                "umo": umo,
                "rule_key": "session_service_config",
                "rule_value": {"llm_enabled": False},
            }
        )

    assert await _get_session_service_config(umo) == baseline


@pytest.mark.asyncio
async def test_update_session_rule_still_allows_operator_testchat_session_service_config(
    service: SessionManagementService, registered_guard: str
):
    umo = _operator_testchat_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "session_service_config",
            "rule_value": {"persona_id": "operator-chosen-persona"},
        }
    )
    assert await _get_session_service_config(umo) == {
        "persona_id": "operator-chosen-persona"
    }


@pytest.mark.asyncio
async def test_update_session_rule_session_service_config_unaffected_for_other_platforms(
    service: SessionManagementService, registered_guard: str
):
    umo = _non_portal_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "session_service_config",
            "rule_value": {"persona_id": "some-persona"},
        }
    )
    assert await _get_session_service_config(umo) == {"persona_id": "some-persona"}


# ---------------------------------------------------------------------------
# Blocker 4: provider_perf_chat_completion is refused for portal sessions --
# the spend-redirection bypass.
# ---------------------------------------------------------------------------


async def _get_provider_pref(umo: str):
    return await sp.session_get(umo, "provider_perf_chat_completion", default=_MISSING)


@pytest.mark.asyncio
async def test_update_session_rule_refuses_provider_perf_chat_completion_for_portal_session(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    baseline = await _get_provider_pref(umo)

    with pytest.raises(SessionManagementServiceError):
        await service.update_session_rule(
            {
                "umo": umo,
                "rule_key": "provider_perf_chat_completion",
                "rule_value": "attacker-chosen-expensive-provider",
            }
        )

    assert await _get_provider_pref(umo) == baseline, (
        "a refused write must not have mutated sp at all -- this is "
        "specifically the spend-redirection bypass this phase closes"
    )


@pytest.mark.asyncio
async def test_update_session_rule_still_allows_operator_testchat_provider_perf_chat_completion(
    service: SessionManagementService, registered_guard: str
):
    umo = _operator_testchat_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "provider_perf_chat_completion",
            "rule_value": "operator-chosen-provider",
        }
    )
    assert await _get_provider_pref(umo) == "operator-chosen-provider"


@pytest.mark.asyncio
async def test_update_session_rule_provider_perf_chat_completion_unaffected_for_other_platforms(
    service: SessionManagementService, registered_guard: str
):
    umo = _non_portal_umo()
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "provider_perf_chat_completion",
            "rule_value": "some-provider",
        }
    )
    assert await _get_provider_pref(umo) == "some-provider"


# ---------------------------------------------------------------------------
# Delete-path (reset()) behaviour for the two new keys.
#
# ``session_rule_guard.py``'s ``reset()`` for
# ``session_service_config``/``provider_perf_chat_completion`` calls
# ``core_facade.reset_session_persona_override``/``core_facade.
# clear_chat_provider_preference_for_umo`` respectively -- both now exist in
# ``core_facade.py`` (added during Phase 4 verification, next to the
# pre-existing ``write_default_kb_config_for_umo``, with the exact bodies
# recorded in this phase's own contracts_for_downstream). These two tests
# exercise that reset behaviour for real.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_session_rule_session_service_config_resets_persona_only(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    await sp.session_put(
        umo,
        "session_service_config",
        {"persona_id": "forced-persona", "llm_enabled": False, "tts_enabled": True},
    )

    await service.delete_session_rule({"umo": umo, "rule_key": "session_service_config"})

    after = await _get_session_service_config(umo)
    assert "persona_id" not in after, (
        "reset() must remove only the persona_id sub-key, letting persona "
        "resolution fall back to the conversation's own persona / the "
        "Agent Bundle's conv_mgr write"
    )
    assert after.get("llm_enabled") is False, (
        "llm_enabled must survive a session_service_config reset untouched"
    )
    assert after.get("tts_enabled") is True, (
        "tts_enabled must survive a session_service_config reset untouched"
    )


@pytest.mark.asyncio
async def test_delete_session_rule_provider_perf_chat_completion_is_cleared(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)
    await sp.session_put(umo, "provider_perf_chat_completion", "forced-provider")

    await service.delete_session_rule(
        {"umo": umo, "rule_key": "provider_perf_chat_completion"}
    )

    assert await _get_provider_pref(umo) is _MISSING, (
        "absence is the safe state for this key -- reset() must remove it "
        "entirely, never write a placeholder value"
    )


# ---------------------------------------------------------------------------
# The operator's separate llm_enabled/tts_enabled toggle path
# (``batch_update_service``) never calls ``_guard_for`` at all (verified
# ground truth, ``session_management_service.py``'s own body) -- so it must
# keep working for Portal sessions even though the generic per-key rule
# editor now refuses ``session_service_config`` outright for them. This is
# the documented mitigation for that refusal's cost (session_rule_guard.py's
# own module docstring).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_batch_update_service_llm_tts_toggle_bypasses_the_guard_entirely(
    service: SessionManagementService, registered_guard: str
):
    umo = _portal_umo(registered_guard)

    result = await service.batch_update_service(
        {"umos": [umo], "llm_enabled": False, "tts_enabled": True}
    )

    assert result["success_count"] == 1
    assert result["failed_count"] == 0
    after = await _get_session_service_config(umo)
    assert after["llm_enabled"] is False
    assert after["tts_enabled"] is True


# ---------------------------------------------------------------------------
# Without the portal, core behaves exactly as before
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_registered_guard_preserves_legacy_write_behaviour(
    service: SessionManagementService, no_guard
):
    umo = f"webchat:FriendMessage:webchat!{uuid.uuid4().hex}!{uuid.uuid4().hex}"
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "kb_config",
            "rule_value": {"kb_ids": ["kb-z"], "top_k": 1},
        }
    )
    assert await _get_kb_config(umo) == {"kb_ids": ["kb-z"], "top_k": 1}


@pytest.mark.asyncio
async def test_no_registered_guard_preserves_legacy_delete_behaviour(
    service: SessionManagementService, no_guard
):
    umo = f"webchat:FriendMessage:webchat!{uuid.uuid4().hex}!{uuid.uuid4().hex}"
    await sp.session_put(umo, "kb_config", {"kb_ids": ["kb-z"], "top_k": 1})

    await service.delete_session_rule({"umo": umo, "rule_key": "kb_config"})

    assert await _get_kb_config(umo) is _MISSING, (
        "with no guard registered the key is removed, as it always was"
    )


@pytest.mark.asyncio
async def test_a_guard_that_raises_is_treated_as_owning_the_key(
    service: SessionManagementService, no_guard
):
    """Failing open would silently restore the bypass the guard exists to stop."""

    class Broken:
        refusal_message = "broken guard"

        async def owns(self, umo: str, rule_key: str) -> bool:
            raise RuntimeError("portal database unreachable")

        async def reset(self, umo: str, rule_key: str) -> None:
            pass

    sms.register_session_rule_guard(Broken())
    umo = _portal_umo("whoever")
    baseline = await _get_kb_config(umo)

    with pytest.raises(SessionManagementServiceError):
        await service.update_session_rule(
            {"umo": umo, "rule_key": "kb_config", "rule_value": _ATTACKER_BINDING}
        )
    assert await _get_kb_config(umo) == baseline


@pytest.mark.asyncio
async def test_no_registered_guard_preserves_legacy_write_behaviour_for_new_keys(
    service: SessionManagementService, no_guard
):
    """Phase 4 extension of the "without the portal" guarantee: a deployment
    that never registers this guard must let ``session_service_config``/
    ``provider_perf_chat_completion`` writes through for ANY umo, portal-
    shaped or not, exactly as they always could before this phase existed.
    """
    umo = f"webchat:FriendMessage:webchat!{uuid.uuid4().hex}!{uuid.uuid4().hex}"
    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "session_service_config",
            "rule_value": {"persona_id": "whatever"},
        }
    )
    assert await _get_session_service_config(umo) == {"persona_id": "whatever"}

    await service.update_session_rule(
        {
            "umo": umo,
            "rule_key": "provider_perf_chat_completion",
            "rule_value": "whatever-provider",
        }
    )
    assert await _get_provider_pref(umo) == "whatever-provider"


@pytest.mark.asyncio
async def test_no_registered_guard_preserves_legacy_delete_behaviour_for_new_keys(
    service: SessionManagementService, no_guard
):
    umo = f"webchat:FriendMessage:webchat!{uuid.uuid4().hex}!{uuid.uuid4().hex}"
    await sp.session_put(umo, "session_service_config", {"persona_id": "whatever"})
    await sp.session_put(umo, "provider_perf_chat_completion", "whatever-provider")

    await service.delete_session_rule({"umo": umo, "rule_key": "session_service_config"})
    await service.delete_session_rule(
        {"umo": umo, "rule_key": "provider_perf_chat_completion"}
    )

    assert await _get_session_service_config(umo) is _MISSING, (
        "with no guard registered the key is removed outright, as it always "
        "was -- this guard's surgical persona_id-only reset only applies "
        "when the guard is actually registered"
    )
    assert await _get_provider_pref(umo) is _MISSING
