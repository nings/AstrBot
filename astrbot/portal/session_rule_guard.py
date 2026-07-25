"""The Portal's claim over the ``kb_config`` session rule.

``POST /api/v1/sessions/rules`` and its delete siblings
(``astrbot/dashboard/services/session_management_service.py``) let a
``require_data_scope`` caller write or remove any key in
``AVAILABLE_SESSION_RULE_KEYS`` for any known umo. ``kb_config`` is in that
list, and for a Portal session it is the only thing standing between an end
user and the operator's *global* knowledge-base corpora:
``retrieve_knowledge_base`` (``astrbot/core/tools/knowledge_base_tools.py``)
reads a *missing* ``kb_config`` as "fall back to the operator's global
``kb_names``", and only an explicit empty ``kb_ids`` list means "no knowledge
base".

Left alone, that route therefore offers two bypasses of everything Phase 3
built: write any ``kb_ids`` at any ``top_k`` with no whitelist check, or delete
the key and re-open the fallback on demand.

This guard closes both, and it lives here rather than in core for two reasons.
Core must not import the Portal -- a core service that depends on a feature
module cannot be upgraded independently of it, and would fail to import in a
deployment that does not run the Portal at all. And only the Portal can tell a
Portal session apart from the operator's own built-in test chat: both use the
``webchat`` platform, so a platform-id check would over-match and silently take
away the operator's ability to configure their own test sessions. Matching on
whether the umo's creator is a real ``PortalUser`` is exact.

Registered with core at startup via
``session_management_service.register_session_rule_guard``; see the call in
``astrbot/dashboard/api/app.py``'s lifespan. A deployment without this
registration behaves exactly as it did before Phase 3.
"""

from __future__ import annotations

from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import core_facade

GUARDED_RULE_KEY = "kb_config"

# The platform id Portal sessions are created under. Necessary but NOT
# sufficient to identify one -- the operator's own dashboard test chat shares
# it, which is exactly why `owns` goes on to check the creator.
_WEBCHAT_PLATFORM_ID = "webchat"

REFUSAL_MESSAGE = (
    "kb_config 由 Portal 的知识库可见性白名单托管，不能通过该接口直接写入。"
    "该接口仅校验 require_data_scope，不做白名单、数量与 top_k 校验，"
    "绕过它会让终端用户读到未授权的知识库。"
    "请改用 Portal 的知识库管理接口（require_system_scope）。"
)


def extract_portal_user_id(umo: str) -> str | None:
    """The creator embedded in a WebChat umo, or ``None`` if it is not one.

    Portal umos look like ``webchat:<message_type>:webchat!<creator>!<id>``
    (built in ``astrbot/dashboard/services/chat_service.py``), where
    ``creator`` is the authenticated Portal ``user_id``. Pure string work, so
    no core import is needed.
    """
    parts = umo.split(":", 2)
    if len(parts) < 3 or parts[0] != _WEBCHAT_PLATFORM_ID:
        return None

    session_part = parts[2].split("!")
    if len(session_part) < 3 or session_part[0] != _WEBCHAT_PLATFORM_ID:
        return None

    creator = session_part[1].strip()
    return creator or None


class PortalKbConfigGuard:
    """Claims ``kb_config`` for umos whose creator is a known Portal user."""

    @property
    def refusal_message(self) -> str:
        return REFUSAL_MESSAGE

    async def owns(self, umo: str, rule_key: str) -> bool:
        if rule_key != GUARDED_RULE_KEY:
            return False

        user_id = extract_portal_user_id(umo)
        if user_id is None:
            # Not a WebChat umo at all, so not ours. The operator keeps full
            # control of kb_config on every other platform.
            return False

        async with portal_async_sessionmaker() as session:
            result = await session.execute(
                select(PortalUser.user_id).where(col(PortalUser.user_id) == user_id)
            )
            # A WebChat umo whose creator is not a Portal user is the
            # operator's own test chat; leave it to them.
            return result.scalar_one_or_none() is not None

    async def reset(self, umo: str, rule_key: str) -> None:
        if rule_key != GUARDED_RULE_KEY:
            return
        await core_facade.write_default_kb_config_for_umo(umo)
        logger.info(
            f"[portal] restored the explicit empty kb_config for {umo} after a "
            "session-rule delete (a missing key would re-open the global-KB "
            "fallback)"
        )


def register(register_fn) -> PortalKbConfigGuard:
    """Register this guard with core's session-rule service.

    Takes the registration function rather than importing it, so this module
    stays independent of where core keeps it.
    """
    guard = PortalKbConfigGuard()
    register_fn(guard)
    logger.info(
        f"[portal] registered the {GUARDED_RULE_KEY} session-rule guard "
        "(operator session-rule writes for Portal sessions are now refused)"
    )
    return guard
