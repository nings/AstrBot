"""Authentication primitives for the AstrBot WebUI module.

Covers access/refresh token minting and verification, refresh-token
rotation with replay detection, CSRF double-submit tokens, FastAPI
dependencies (`get_current_user`, `require_csrf`), and process-local
login/concurrency throttling.

None of these submodules import ``astrbot.dashboard.*`` or the
capability-wall-forbidden core services (``ConversationService``,
``SessionManagementService``, ``ApiKeyService``, ``SubAgentOrchestrator``,
or direct ``SharedPreferences``/``conversation_mgr``/``persona_mgr``
imports) -- see ``astrbot/webui/services/core_facade.py`` for the sole
module allowed to cross that boundary.
"""

from __future__ import annotations
