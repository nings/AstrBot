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
