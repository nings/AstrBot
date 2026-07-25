"""The Portal's claim over the ``kb_config``, ``session_service_config`` and
``provider_perf_chat_completion`` session rules.

``POST /api/v1/sessions/rules`` and its delete siblings
(``astrbot/dashboard/services/session_management_service.py``) let a
``require_data_scope`` caller write or remove any key in
``AVAILABLE_SESSION_RULE_KEYS`` for any known umo. Three of those keys are the
only things standing between an end user and something the operator did not
authorize for a Portal session:

- ``kb_config`` (Phase 3): a *missing* key falls back to the operator's
  *global* knowledge-base corpora (``retrieve_knowledge_base`` in
  ``astrbot/core/tools/knowledge_base_tools.py``), and a present key has no
  whitelist/top_k check at this route.
- ``session_service_config`` (Phase 4): its ``persona_id`` sub-key is a
  **force-applied** persona -- ``PersonaManager.resolve_selected_persona``
  (``astrbot/core/persona_mgr.py``) reads it *before* falling back to the
  conversation's own persona, which is the surface Phase 4's Agent Bundle
  writes through ``conv_mgr.update_conversation(persona_id=...)``. Left open,
  this route lets any data-scope caller shadow a bundle's persona choice with
  an arbitrary one.
- ``provider_perf_chat_completion`` (Phase 4): the per-umo chat-provider
  preference. ``ProviderManager.get_using_provider``
  (``astrbot/core/provider/manager.py``) reads it *before* falling back to the
  operator's ``default_provider_id``. Left open, this route lets any
  data-scope caller point a Portal session's spend at any provider configured
  on the instance, with no whitelist check at all.

This guard closes all three, and it lives here rather than in core for two
reasons. Core must not import the Portal -- a core service that depends on a
feature module cannot be upgraded independently of it, and would fail to
import in a deployment that does not run the Portal at all. And only the
Portal can tell a Portal session apart from the operator's own built-in test
chat: both use the ``webchat`` platform, so a platform-id check would
over-match and silently take away the operator's ability to configure their
own test sessions. Matching on whether the umo's creator is a real
``PortalUser`` is exact, and unchanged from Phase 3.

Registered with core at startup via
``session_management_service.register_session_rule_guard``; see the call in
``astrbot/dashboard/api/app.py``'s lifespan. A deployment without this
registration behaves exactly as it did before Phase 3/4.

Per-key design notes (each key's safe "absence" and "reset" behaviour differs
-- see each key's block below and :meth:`PortalKbConfigGuard.reset`):

``kb_config`` -- absence is UNSAFE (re-opens the global-KB fallback), so
``reset`` writes an explicit empty binding. Unchanged from Phase 3.

``provider_perf_chat_completion`` -- absence is SAFE. ``get_using_provider``
already treats a missing/unresolvable per-umo key as "fall through to the
operator's ``default_provider_id``" -- that is exactly the state a bundle with
no ``chat_provider_id`` (or no bundle at all) wants, and exactly the state
Phase 1's "never forward a client-supplied provider" invariant wants for any
session this guard has never touched. So ``reset`` does not write a
placeholder the way ``kb_config`` does; it performs the plain removal that a
non-guarded ``session_remove`` would have done, restoring that safe absent
state instead of leaving a stale value in place.

``session_service_config`` -- this key is a shared container: besides
``persona_id`` it also carries ``llm_enabled``/``tts_enabled``
(``astrbot/core/star/session_llm_manager.py``). A blanket replace on reset
would silently re-enable/disable those for the caller, so ``reset`` merges:
it removes only the ``persona_id`` sub-key (letting persona resolution fall
back to the conversation's own persona / the bundle's ``conv_mgr`` write) and
leaves every other sub-key untouched.

For *writes* (not deletes), ``owns`` is asked only ``(umo, rule_key)`` --
the ``SessionRuleGuard`` protocol never hands it the incoming ``rule_value``,
and ``update_session_rule`` raises the refusal message the moment a guard
claims the key, before it would otherwise write anything. That means there is
no way to claim ``session_service_config`` "only when the write touches
persona_id" -- claiming the key at all necessarily refuses every write
through this route, including one that only intends to toggle
``llm_enabled``/``tts_enabled``. This guard accepts that cost rather than
leave the persona bypass open, for two reasons: first, it is the only
implementable choice given the protocol's signature; second, it is a small
cost in practice, because ``SessionManagementService.batch_update_service``
already flips ``llm_enabled``/``tts_enabled``/``session_enabled`` for any
umo (Portal or not) through a separate write path that never calls
``_guard_for`` at all -- so the operator does not lose that control, only the
generic per-key rule editor's ability to touch this one key for Portal
sessions.
"""

from __future__ import annotations

from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import core_facade

# Rule keys this guard claims for Portal umos. See the module docstring for
# why each one needs claiming and what "reset" means for it.
GUARDED_RULE_KEYS = frozenset(
    {
        "kb_config",
        "session_service_config",
        "provider_perf_chat_completion",
    }
)

# The platform id Portal sessions are created under. Necessary but NOT
# sufficient to identify one -- the operator's own dashboard test chat shares
# it, which is exactly why `owns` goes on to check the creator.
_WEBCHAT_PLATFORM_ID = "webchat"

REFUSAL_MESSAGE = (
    "该规则键由 AstrBot Portal 托管，不能通过该接口直接写入或删除。"
    "该接口仅校验 require_data_scope，不做 Portal 的白名单/绑定校验："
    "对 kb_config 会让终端用户读到未授权的知识库，对 session_service_config 会绕过"
    "Agent Bundle 强制指定任意 persona，对 provider_perf_chat_completion 会把开销"
    "转嫁到运营方未预期的模型上。请改用 Portal 的知识库 / Agent Bundle 管理接口"
    "（require_system_scope）或 Portal 会话端点。"
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
    """Claims ``kb_config``, ``session_service_config`` and
    ``provider_perf_chat_completion`` for umos whose creator is a known
    Portal user.

    Kept under its Phase 3 name (``PortalKbConfigGuard``) rather than renamed
    -- it is imported directly by ``tests/test_portal_session_management_kb_guard.py``
    and registered as a singleton instance in ``astrbot/dashboard/api/app.py``;
    renaming it would be a pure churn risk with no behavioural benefit.
    """

    @property
    def refusal_message(self) -> str:
        return REFUSAL_MESSAGE

    async def owns(self, umo: str, rule_key: str) -> bool:
        if rule_key not in GUARDED_RULE_KEYS:
            return False

        user_id = extract_portal_user_id(umo)
        if user_id is None:
            # Not a WebChat umo at all, so not ours. The operator keeps full
            # control of these keys on every other platform.
            return False

        async with portal_async_sessionmaker() as session:
            result = await session.execute(
                select(PortalUser.user_id).where(col(PortalUser.user_id) == user_id)
            )
            # A WebChat umo whose creator is not a Portal user is the
            # operator's own test chat; leave it to them.
            return result.scalar_one_or_none() is not None

    async def reset(self, umo: str, rule_key: str) -> None:
        if rule_key == "kb_config":
            await core_facade.write_default_kb_config_for_umo(umo)
            logger.info(
                f"[portal] restored the explicit empty kb_config for {umo} after a "
                "session-rule delete (a missing key would re-open the global-KB "
                "fallback)"
            )
        elif rule_key == "session_service_config":
            await core_facade.reset_session_persona_override(umo)
            logger.info(
                f"[portal] removed the forced persona_id from session_service_config "
                f"for {umo} after a session-rule delete, leaving llm_enabled/"
                "tts_enabled untouched (persona resolution now falls back to the "
                "conversation's own persona / the Agent Bundle's conv_mgr write)"
            )
        elif rule_key == "provider_perf_chat_completion":
            await core_facade.clear_chat_provider_preference_for_umo(umo)
            logger.info(
                f"[portal] removed the provider_perf_chat_completion override for "
                f"{umo} after a session-rule delete (absence is the safe state here: "
                "it falls back to the operator's default_provider_id)"
            )
        # Any other key is not ours; `owns` would not have claimed it, so
        # `_guard_for`/`_restore_guarded_keys` would not route a reset here for
        # it. No-op rather than raise, since a guard that raises on reset is
        # treated as a restore failure by the caller (logged, not fatal).


def register(register_fn) -> PortalKbConfigGuard:
    """Register this guard with core's session-rule service.

    Takes the registration function rather than importing it, so this module
    stays independent of where core keeps it.
    """
    guard = PortalKbConfigGuard()
    register_fn(guard)
    logger.info(
        f"[portal] registered the {sorted(GUARDED_RULE_KEYS)} session-rule guard "
        "(operator session-rule writes for Portal sessions are now refused for "
        "these keys)"
    )
    return guard
