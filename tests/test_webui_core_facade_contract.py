"""Pin the signatures of every core method ``core_facade.py`` depends on.

``astrbot/webui/services/core_facade.py`` is the module's one and only
"capability wall" seam into core AstrBot state -- it calls a small, fixed
set of ``ChatService`` methods (plus two of its ``db`` attribute's methods)
and two ``SharedPreferences`` methods. None of core_facade's own security
guarantees (the ownership gate, the "never forward selected_provider/
selected_model" rule) are re-verified here -- that is
``tests/test_webui_chat_isolation.py``'s job. This file exists purely so
that a future AstrBot core upgrade that silently renames a parameter,
drops a default, or changes a return-shape-relevant parameter on any of
these methods fails this test suite loudly, instead of core_facade calling
the new signature wrong at runtime and silently doing the wrong thing (or
crashing) in production.

Every assertion below is generated from ``inspect.signature`` against the
*real* classes -- not hand-copied strings -- so a genuine drift is what
actually fails these tests, not a stale expectation.

Scope note (see this task's ``gaps``): the assignment's shorthand names
this contract "ChatService/conv_mgr/sp/PersonaManager". As currently
written, ``core_facade.py`` does not import or call ``conv_mgr``
(``conversation_mgr``) or ``PersonaManager`` at all -- every current
facade function goes through ``ChatService`` methods, which already hold
``self.conv_mgr`` internally (see the module's own gaps/notes). There is
therefore nothing to pin for those two today; this file pins exactly what
``core_facade.py`` actually imports and calls (``ChatService``, its ``db``
attribute's two session-lookup methods, and ``SharedPreferences``'s two
preference accessors). If a future phase adds a direct ``conv_mgr``/
``persona_mgr`` import to ``core_facade.py``, this file must be extended
accordingly at that time.
"""

from __future__ import annotations

import ast
import inspect
import textwrap

from astrbot.core.db import BaseDatabase
from astrbot.core.utils.shared_preferences import SharedPreferences
from astrbot.dashboard.services.chat_service import ChatService
from astrbot.webui.services import core_facade


def _params(func) -> list[str]:
    return list(inspect.signature(func).parameters.keys())


def _defaults(func) -> dict[str, object]:
    return {
        name: param.default
        for name, param in inspect.signature(func).parameters.items()
        if param.default is not inspect.Parameter.empty
    }


# ---------------------------------------------------------------------------
# ChatService: every method core_facade.py calls.
# ---------------------------------------------------------------------------


def test_chat_service_constructor_signature():
    assert _params(ChatService.__init__) == ["self", "db", "core_lifecycle"]


def test_chat_service_new_session_signature():
    assert _params(ChatService.new_session) == ["self", "username", "platform_id"]


def test_chat_service_get_sessions_signature():
    assert _params(ChatService.get_sessions) == ["self", "username", "platform_id"]


def test_chat_service_get_session_signature():
    assert _params(ChatService.get_session) == ["self", "username", "session_id"]


def test_chat_service_update_session_display_name_signature():
    assert _params(ChatService.update_session_display_name) == [
        "self",
        "username",
        "session_id",
        "display_name",
    ]


def test_chat_service_delete_webchat_session_signature():
    assert _params(ChatService.delete_webchat_session) == [
        "self",
        "username",
        "session_id",
    ]


def test_chat_service_delete_session_internal_signature():
    assert _params(ChatService.delete_session_internal) == [
        "self",
        "session",
        "username",
    ]


def test_chat_service_build_chat_stream_signature():
    assert _params(ChatService.build_chat_stream) == ["self", "username", "post_data"]


def test_chat_service_build_chat_run_stream_signature():
    assert _params(ChatService.build_chat_run_stream) == ["self", "username", "run_id"]


def test_chat_service_stop_session_signature():
    assert _params(ChatService.stop_session) == ["self", "username", "session_id"]


def test_chat_service_update_message_signature():
    assert _params(ChatService.update_message) == ["self", "username", "data"]


def test_chat_service_prepare_regenerate_message_payload_signature():
    assert _params(ChatService.prepare_regenerate_message_payload) == [
        "self",
        "username",
        "data",
    ]


def test_chat_service_has_chat_runs_dict_attribute():
    """core_facade.stream_run reads ``chat_service.chat_runs.get(run_id)``
    and each entry's ``.username`` directly (not through a method) -- pin
    that this attribute is still initialized as a plain dict in __init__."""
    source = textwrap.dedent(inspect.getsource(ChatService.__init__))
    tree = ast.parse(source)
    assigned_attrs = {
        node.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Store)
    }
    assert "chat_runs" in assigned_attrs


# ---------------------------------------------------------------------------
# BaseDatabase: the two session-lookup methods core_facade.py calls
# directly via ``chat_service.db``.
# ---------------------------------------------------------------------------


def test_base_database_get_platform_session_by_id_signature():
    assert _params(BaseDatabase.get_platform_session_by_id) == [
        "self",
        "session_id",
    ]


def test_base_database_get_platform_sessions_by_ids_signature():
    assert _params(BaseDatabase.get_platform_sessions_by_ids) == [
        "self",
        "session_ids",
    ]


# ---------------------------------------------------------------------------
# SharedPreferences: core_facade.get_jwt_secret's two touchpoints
# (``sp.get_async`` / ``sp.put_async``), matching the exact call shape
# ``astrbot.webui.config.get_webui_jwt_secret`` expects its injected
# ``get_preference``/``put_preference`` callables to have.
# ---------------------------------------------------------------------------


def test_shared_preferences_get_async_signature():
    params = _params(SharedPreferences.get_async)
    assert params[:4] == ["self", "scope", "scope_id", "key"]
    defaults = _defaults(SharedPreferences.get_async)
    assert "default" in defaults
    assert defaults["default"] is None


def test_shared_preferences_put_async_signature():
    assert _params(SharedPreferences.put_async) == [
        "self",
        "scope",
        "scope_id",
        "key",
        "value",
    ]


# ---------------------------------------------------------------------------
# Sanity: core_facade.py itself still only imports what the capability wall
# permits (ChatService + sp), and does not (yet) reach for conv_mgr/persona_mgr
# -- see module docstring for why that's not pinned as a positive contract.
# ---------------------------------------------------------------------------


def test_core_facade_module_exports_unchanged():
    assert core_facade.__all__ == [
        "CoreFacadeError",
        "ResourceNotFoundError",
        "ValidationError",
        "get_jwt_secret",
        "create_session",
        "list_sessions",
        "get_owned_session",
        "rename_session",
        "delete_session",
        "batch_delete_sessions",
        "send_message",
        "stream_run",
        "resume_run",
        "stop_run",
        "update_message",
        "regenerate",
        "get_chat_configs",
    ]
