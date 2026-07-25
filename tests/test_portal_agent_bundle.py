"""Guardrail tests for the portal module's Phase 4 Agent Bundle feature:
the operator-curated persona + chat-provider + KB "preset" an end user
selects by ``bundle_id`` only.

Every handler here is exercised through the *real* production stack --
``portal_router`` (``agent_routes.py`` + the agent-bundle CRUD in
``admin_routes.py``) -> ``agent_bundle_service`` -> ``core_facade`` -> the
real, process-wide ``astrbot.core.sp`` (``SharedPreferences``) singleton --
with only ``ChatService`` (via a duck-typed ``FakeChatService`` whose
``conv_mgr`` is a small in-memory fake) and ``core_lifecycle.persona_mgr``/
``core_lifecycle.provider_manager`` replaced by small fakes. This mirrors
exactly the approach ``test_portal_kb_whitelist.py``/
``test_portal_chat_isolation.py`` already take.

**Ground truth this file's fakes are built against** (verified by reading
the real classes, not assumed):

* ``core_facade.set_session_persona`` writes through
  ``ChatService.conv_mgr.update_conversation(unified_msg_origin=umo,
  conversation_id=cid, persona_id=...)`` -- *never* through
  ``session_service_config['persona_id']``. ``FakeConvMgr`` below models
  exactly the four methods ``core_facade.py`` calls on a real
  ``ConversationManager``: ``get_curr_conversation_id``,
  ``get_conversation``, ``new_conversation``, ``update_conversation``.
* ``core_facade.set_session_provider`` writes/removes the per-umo ``sp``
  key ``provider_perf_chat_completion`` directly -- no ``ChatService``
  involvement at all for this dimension.
* ``core_facade.persona_exists``/``get_persona_v3_by_id`` and
  ``core_facade.provider_exists``/``get_provider_by_id`` read
  ``request.app.state.core_lifecycle.persona_mgr``/``.provider_manager``
  respectively (plain attribute access, not an import) -- ``FakePersonaManager``/
  ``FakeProviderManager`` below implement exactly those two methods each,
  nothing more.

Because ``session_service_config`` is *not* the binding surface this phase
chose for persona, the "assert on ``session_service_config``" acceptance
criterion in this task's own brief is satisfied here by proving the
*opposite* of a naive reading: binding a bundle with a persona never writes
anything into ``session_service_config`` at all, so any ``llm_enabled``/
``tts_enabled``/other sub-keys already present in that shared dict survive
a bind completely untouched -- see
``test_bind_applies_persona_and_provider_and_preserves_session_service_config``
for the actual assertions this file makes and why.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.core import sp
from astrbot.dashboard.responses import ApiError, error
from astrbot.portal.api import chat_routes
from astrbot.portal.api.agent_routes import BindAgentBundleRequest
from astrbot.portal.api.router import portal_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import (
    PortalAgentBundle,
    PortalBaseModel,
    PortalKbVisibility,
    PortalUser,
)
from astrbot.portal.services import agent_bundle_service, core_facade

PORTAL_TEST_SECRET = "portal-agent-bundle-test-secret-32bytes!"
DASHBOARD_JWT_SECRET = "dashboard-agent-bundle-test-secret-32b!"

_MISSING = object()
"""Local sentinel for "this sp key was never written at all" -- distinct
from any real, falsy value a test might otherwise mistake for absence."""


# ---------------------------------------------------------------------------
# Fakes. See module docstring for the exact real-class surface each mirrors.
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    session_id: str
    creator: str
    platform_id: str = "webchat"
    is_group: int = 0


class _FakeDb:
    def __init__(self, service: FakeChatService) -> None:
        self._service = service

    async def get_platform_session_by_id(self, session_id: str) -> FakeSession | None:
        return self._service.sessions_by_id.get(session_id)

    async def get_platform_sessions_by_ids(self, session_ids: list[str]) -> list[FakeSession]:
        return [
            self._service.sessions_by_id[sid]
            for sid in session_ids
            if sid in self._service.sessions_by_id
        ]


@dataclass
class FakeConversation:
    conversation_id: str
    persona_id: str | None = None


class FakeConvMgr:
    """Implements exactly the four ``ConversationManager`` methods
    ``core_facade.set_session_persona``/``get_session_agent_bindings``
    touch."""

    def __init__(self) -> None:
        self.session_conversation: dict[str, str] = {}
        self.conversations: dict[str, FakeConversation] = {}

    async def get_curr_conversation_id(self, unified_msg_origin: str) -> str | None:
        return self.session_conversation.get(unified_msg_origin)

    async def get_conversation(
        self, unified_msg_origin: str, conversation_id: str
    ) -> FakeConversation | None:
        del unified_msg_origin
        return self.conversations.get(conversation_id)

    async def new_conversation(self, unified_msg_origin: str, platform_id: str) -> str:
        del platform_id
        conversation_id = str(uuid.uuid4())
        self.conversations[conversation_id] = FakeConversation(conversation_id=conversation_id)
        self.session_conversation[unified_msg_origin] = conversation_id
        return conversation_id

    async def update_conversation(
        self,
        unified_msg_origin: str,
        conversation_id: str,
        persona_id: str | None = None,
        **_kwargs: Any,
    ) -> None:
        del unified_msg_origin
        conv = self.conversations.setdefault(
            conversation_id, FakeConversation(conversation_id=conversation_id)
        )
        if persona_id is not None:
            conv.persona_id = persona_id


class FakeChatService:
    def __init__(self) -> None:
        self.sessions_by_id: dict[str, FakeSession] = {}
        self.conv_mgr = FakeConvMgr()
        self.db = _FakeDb(self)

    def add_session(self, session: FakeSession) -> None:
        self.sessions_by_id[session.session_id] = session

    async def new_session(self, username: str, platform_id: str) -> dict:
        session = FakeSession(session_id=str(uuid.uuid4()), creator=username)
        self.add_session(session)
        return {"session_id": session.session_id, "platform_id": platform_id}


class FakePersonaManager:
    """Implements exactly ``get_persona_v3_by_id`` (sync) and
    ``get_default_persona_v3`` (async) -- the only two ``PersonaManager``
    methods ``core_facade.py`` calls."""

    def __init__(self) -> None:
        self.personas_v3: list[dict[str, Any]] = []
        self.default_persona_name = "operator-default-persona"

    def add_persona(self, name: str) -> None:
        self.personas_v3.append({"name": name})

    def get_persona_v3_by_id(self, persona_id: str | None) -> dict[str, Any] | None:
        if not persona_id:
            return None
        if persona_id == "default":
            return {"name": "default"}
        return next((p for p in self.personas_v3 if p["name"] == persona_id), None)

    async def get_default_persona_v3(self, umo: str | None = None) -> dict[str, Any]:
        del umo
        return {"name": self.default_persona_name}


class FakeProviderManager:
    """Implements exactly ``get_provider_by_id`` (async) -- the only
    ``ProviderManager`` method ``core_facade.py`` calls for Phase 4."""

    def __init__(self) -> None:
        self.inst_map: dict[str, Any] = {}

    def add_provider(self, provider_id: str) -> None:
        self.inst_map[provider_id] = object()

    async def get_provider_by_id(self, provider_id: str) -> Any | None:
        return self.inst_map.get(provider_id)


@dataclass
class FakeCoreLifecycle:
    persona_mgr: FakePersonaManager = field(default_factory=FakePersonaManager)
    provider_manager: FakeProviderManager = field(default_factory=FakeProviderManager)


# ---------------------------------------------------------------------------
# App / client plumbing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-agent-bundle-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


@pytest.fixture
def fake_chat_service() -> FakeChatService:
    return FakeChatService()


@pytest.fixture
def fake_core_lifecycle() -> FakeCoreLifecycle:
    return FakeCoreLifecycle()


@pytest.fixture
def app(
    portal_sessionmaker,
    fake_chat_service: FakeChatService,
    fake_core_lifecycle: FakeCoreLifecycle,
    monkeypatch,
) -> FastAPI:
    from types import SimpleNamespace

    application = FastAPI()
    application.include_router(portal_router, prefix="/api/v1")
    application.state.services = SimpleNamespace(chat=fake_chat_service)
    application.state.portal_jwt_secret = PORTAL_TEST_SECRET
    application.state.core_lifecycle = fake_core_lifecycle
    # Only needed so require_system_scope (the admin routes' gate) can decode
    # a real operator dashboard JWT -- see test_portal_kb_whitelist.py's
    # identical fixture for the same rationale.
    application.state.jwt_secret = DASHBOARD_JWT_SECRET

    @application.exception_handler(ApiError)
    async def _api_error_handler(_request, exc: ApiError):
        return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    monkeypatch.setattr(chat_routes, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(core_facade, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(
        agent_bundle_service, "portal_async_sessionmaker", portal_sessionmaker
    )

    return application


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


async def _insert_user(sessionmaker, *, user_id: str) -> None:
    async with sessionmaker() as session:
        session.add(
            PortalUser(
                user_id=user_id,
                email=f"{user_id}@example.test",
                password_hash=None,
                display_name=user_id,
                auth_method="local",
                role="user",
                status="active",
            )
        )
        await session.commit()


@pytest_asyncio.fixture
async def two_users(portal_sessionmaker) -> tuple[str, str]:
    user_a = f"user-a-{uuid.uuid4().hex}"
    user_b = f"user-b-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    await _insert_user(portal_sessionmaker, user_id=user_b)
    return user_a, user_b


def _auth(user_id: str) -> tuple[dict, dict]:
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


def _admin_headers(username: str = "test-operator") -> dict:
    token = jwt.encode({"username": username}, DASHBOARD_JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


async def _create_session(client: httpx.AsyncClient, cookies: dict, headers: dict) -> str:
    response = await client.post(
        "/api/v1/portal/chat/sessions", cookies=cookies, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


def _umo(user_id: str, session_id: str) -> str:
    """Same umo formula ``core_facade._session_umo`` uses for a non-group
    WebChat session."""
    return f"webchat:FriendMessage:webchat!{user_id}!{session_id}"


async def _grant_kb(sessionmaker, *, kb_id: str, enabled: bool = True) -> None:
    async with sessionmaker() as session:
        session.add(
            PortalKbVisibility(
                kb_id=kb_id,
                kb_name=f"KB {kb_id}",
                enabled=enabled,
                granted_by="test-operator",
            )
        )
        await session.commit()


async def _insert_bundle(
    sessionmaker,
    *,
    name: str = "Test Bundle",
    description: str | None = None,
    persona_id: str | None = None,
    chat_provider_id: str | None = None,
    kb_ids: list[str] | None = None,
    enabled: bool = True,
) -> str:
    """Insert a ``PortalAgentBundle`` row directly, bypassing the admin HTTP
    layer (which gets its own dedicated CRUD tests below) -- mirrors
    ``test_portal_kb_whitelist.py``'s own ``_grant_kb`` helper."""
    bundle = PortalAgentBundle(
        name=name,
        description=description,
        persona_id=persona_id,
        chat_provider_id=chat_provider_id,
        kb_ids_json=json.dumps(kb_ids or []),
        enabled=enabled,
        updated_by="test-operator",
    )
    async with sessionmaker() as session:
        session.add(bundle)
        await session.commit()
        await session.refresh(bundle)
    return bundle.bundle_id


def _default_kb_config() -> dict[str, Any]:
    settings = get_portal_settings()
    return {"kb_ids": [], "top_k": settings.default_kb_top_k}


async def _kb_config(client: httpx.AsyncClient, session_id: str, cookies: dict) -> dict:
    response = await client.get(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base", cookies=cookies
    )
    assert response.status_code == 200, response.text
    return response.json()


# ---------------------------------------------------------------------------
# 0. Unit-level: the request model itself drops every field but bundle_id.
# ---------------------------------------------------------------------------


def test_bind_request_model_drops_every_field_except_bundle_id():
    payload = {
        "bundle_id": "real-bundle-id",
        "selected_provider": "attacker-chosen-provider",
        "selected_model": "attacker-chosen-model",
        "provider_id": "attacker-chosen-provider-id",
        "persona_id": "attacker-chosen-persona",
    }
    parsed = BindAgentBundleRequest.model_validate(payload)

    assert parsed.bundle_id == "real-bundle-id"
    assert parsed.model_dump() == {"bundle_id": "real-bundle-id"}
    assert not hasattr(parsed, "selected_provider")
    assert not hasattr(parsed, "selected_model")
    assert not hasattr(parsed, "provider_id")
    assert not hasattr(parsed, "persona_id")


# ---------------------------------------------------------------------------
# 1. THE headline acceptance criterion: injected provider/model/persona
#    fields in a bind request body are ignored end-to-end, never influence
#    what actually gets resolved and written.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_injected_provider_and_persona_fields_never_reach_core(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
    fake_chat_service: FakeChatService,
):
    user_a, _user_b = two_users
    bundle_persona = "bundle-real-persona"
    bundle_provider = "bundle-real-provider"
    fake_core_lifecycle.persona_mgr.add_persona(bundle_persona)
    fake_core_lifecycle.provider_manager.add_provider(bundle_provider)
    # The attacker's injected provider/persona are deliberately never
    # registered as existing anywhere -- if they were ever actually looked
    # up or applied, the bind would either 404 (nonexistent) or the
    # resulting binding would show their value. Neither may happen.
    bundle_id = await _insert_bundle(
        portal_sessionmaker,
        persona_id=bundle_persona,
        chat_provider_id=bundle_provider,
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={
            "bundle_id": bundle_id,
            "selected_provider": "attacker-chosen-provider",
            "selected_model": "attacker-chosen-model",
            "provider_id": "attacker-chosen-provider-id",
            "persona_id": "attacker-chosen-persona",
        },
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    # The response itself is the narrow consumer shape -- it never echoes
    # back a resolved persona/provider either way.
    assert set(response.json().keys()) == {
        "bundle_id",
        "name",
        "description",
        "sort_order",
        "is_default",
    }

    umo = _umo(user_a, session_id)
    persisted_provider = await sp.session_get(
        umo, "provider_perf_chat_completion", default=_MISSING
    )
    assert persisted_provider == bundle_provider, (
        "the resolved provider must be the bundle's own, never the "
        "attacker-injected one"
    )
    assert persisted_provider != "attacker-chosen-provider-id"

    conv_mgr = fake_chat_service.conv_mgr
    cid = conv_mgr.session_conversation.get(umo)
    assert cid is not None
    assert conv_mgr.conversations[cid].persona_id == bundle_persona, (
        "the resolved persona must be the bundle's own, never the "
        "attacker-injected one"
    )


# ---------------------------------------------------------------------------
# 2. Binding an enabled bundle applies persona + provider server-side, and
#    session_service_config's other keys survive (see module docstring for
#    why this is phrased as "session_service_config is never touched at
#    all", not "session_service_config holds the persona").
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_applies_persona_and_provider_and_preserves_session_service_config(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
    fake_chat_service: FakeChatService,
):
    user_a, _user_b = two_users
    fake_core_lifecycle.persona_mgr.add_persona("helpful-assistant")
    fake_core_lifecycle.provider_manager.add_provider("gpt-cheap")
    bundle_id = await _insert_bundle(
        portal_sessionmaker,
        persona_id="helpful-assistant",
        chat_provider_id="gpt-cheap",
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    umo = _umo(user_a, session_id)

    # Pre-populate session_service_config the way the operator's own
    # llm/tts toggles (session_llm_manager.py) would, BEFORE the bundle is
    # ever applied.
    preexisting_service_config = {
        "llm_enabled": True,
        "tts_enabled": False,
        "session_enabled": True,
    }
    await sp.session_put(umo, "session_service_config", dict(preexisting_service_config))

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200, response.text

    # provider_perf_chat_completion: the actual Phase 4 provider binding
    # surface.
    assert (
        await sp.session_get(umo, "provider_perf_chat_completion", default=_MISSING)
        == "gpt-cheap"
    )

    # persona: applied via conv_mgr.update_conversation, per the load-bearing
    # BINDING SURFACE DECISION -- NOT via session_service_config['persona_id'].
    conv_mgr = fake_chat_service.conv_mgr
    cid = conv_mgr.session_conversation.get(umo)
    assert cid is not None
    assert conv_mgr.conversations[cid].persona_id == "helpful-assistant"

    # session_service_config itself must be byte-for-byte unchanged: the
    # bundle-apply path never writes to this key at all, so llm_enabled/
    # tts_enabled/session_enabled all survive.
    after = await sp.session_get(umo, "session_service_config", default=_MISSING)
    assert after == preexisting_service_config
    assert "persona_id" not in after


# ---------------------------------------------------------------------------
# 3. Disabled bundle refused; unknown bundle_id refused indistinguishably.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_disabled_bundle_is_refused(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    bundle_id = await _insert_bundle(portal_sessionmaker, enabled=False)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert await _kb_config(client, session_id, cookies) == baseline


@pytest.mark.asyncio
async def test_bind_unknown_bundle_id_is_refused_identically_to_disabled(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    """Same status code AND same response shape as a disabled bundle -- a
    caller must not be able to use the response to enumerate which bundle
    ids exist versus which are merely disabled (mirrors
    ``kb_visibility_service``'s own 404-uniformly precedent)."""
    user_a, _user_b = two_users
    disabled_bundle_id = await _insert_bundle(portal_sessionmaker, enabled=False)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    disabled_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": disabled_bundle_id},
        cookies=cookies,
        headers=headers,
    )
    unknown_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": str(uuid.uuid4())},
        cookies=cookies,
        headers=headers,
    )
    assert disabled_resp.status_code == unknown_resp.status_code == 404
    assert disabled_resp.json() == unknown_resp.json()


# ---------------------------------------------------------------------------
# 4. A bundle whose persona/provider no longer exists is refused, and never
#    appears in the end-user listing. (KB dangling is handled differently
#    by the shipped implementation -- see its own contrast test below.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_with_dangling_persona_is_refused_and_hidden_from_listing(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    # persona_id set, but never registered in FakePersonaManager -- models
    # "operator deleted this persona after the bundle was created".
    bundle_id = await _insert_bundle(portal_sessionmaker, persona_id="deleted-persona")

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    list_resp = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert list_resp.status_code == 200, list_resp.text
    assert bundle_id not in {b["bundle_id"] for b in list_resp.json()["agent_bundles"]}

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert bind_resp.status_code == 404, bind_resp.text


@pytest.mark.asyncio
async def test_bundle_with_dangling_provider_is_refused_and_hidden_from_listing(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    bundle_id = await _insert_bundle(
        portal_sessionmaker, chat_provider_id="deleted-provider"
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    list_resp = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert bundle_id not in {b["bundle_id"] for b in list_resp.json()["agent_bundles"]}

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert bind_resp.status_code == 404, bind_resp.text


@pytest.mark.asyncio
async def test_bundle_with_dangling_kb_id_stays_selectable_and_drops_it_silently(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
):
    """Deliberate contrast with the two tests above: per
    ``agent_bundle_service.py``'s own documented design (module docstring,
    "why persona/provider are NOT treated the same as KB ids"), a bundle
    referencing a KB id that is no longer ``PortalKbVisibility.enabled``
    does NOT make the whole bundle unselectable -- it is silently dropped
    at apply time, logged as a warning, and the bind still succeeds with
    the remaining (still-visible) KB ids. This test asserts the actual
    shipped behaviour, not the literal "KB no longer exists -> refused"
    reading of this task's own brief.
    """
    user_a, _user_b = two_users
    live_kb_id = str(uuid.uuid4())
    await _grant_kb(portal_sessionmaker, kb_id=live_kb_id, enabled=True)
    bundle_id = await _insert_bundle(
        portal_sessionmaker, kb_ids=[live_kb_id, "revoked-or-deleted-kb-id"]
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    list_resp = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert bundle_id in {b["bundle_id"] for b in list_resp.json()["agent_bundles"]}

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert bind_resp.status_code == 200, bind_resp.text
    assert (await _kb_config(client, session_id, cookies))["kb_ids"] == [live_kb_id]


# ---------------------------------------------------------------------------
# 5. All-or-nothing: a refused bind leaves no partial binding behind.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refused_bind_leaves_no_partial_binding(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
    fake_chat_service: FakeChatService,
):
    user_a, _user_b = two_users
    fake_core_lifecycle.provider_manager.add_provider("would-have-applied-provider")
    live_kb_id = str(uuid.uuid4())
    await _grant_kb(portal_sessionmaker, kb_id=live_kb_id, enabled=True)
    # persona_id dangling -> the whole bundle must be refused, even though
    # its provider and KB references are both perfectly healthy.
    bundle_id = await _insert_bundle(
        portal_sessionmaker,
        persona_id="dangling-persona",
        chat_provider_id="would-have-applied-provider",
        kb_ids=[live_kb_id],
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    umo = _umo(user_a, session_id)

    baseline_provider = await sp.session_get(
        umo, "provider_perf_chat_completion", default=_MISSING
    )
    baseline_bundle_bookkeeping = await sp.session_get(
        umo, "portal_agent_bundle_id", default=_MISSING
    )
    baseline_kb_config = await _kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text

    assert (
        await sp.session_get(umo, "provider_perf_chat_completion", default=_MISSING)
        == baseline_provider
    ), "the provider must never have been written for a refused bundle"
    assert (
        await sp.session_get(umo, "portal_agent_bundle_id", default=_MISSING)
        == baseline_bundle_bookkeeping
    ), "bundle-id bookkeeping must never be set for a refused bundle"
    assert await _kb_config(client, session_id, cookies) == baseline_kb_config
    assert fake_chat_service.conv_mgr.session_conversation.get(umo) is None, (
        "no conversation should ever have been created/touched for a "
        "refused bind attempt"
    )


# ---------------------------------------------------------------------------
# 6. GET /portal/agent-bundles never leaks provider id, persona id, or
#    model/kb inventory.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_agent_bundles_response_never_leaks_operator_inventory(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, _user_b = two_users
    fake_core_lifecycle.persona_mgr.add_persona("secret-persona-name")
    fake_core_lifecycle.provider_manager.add_provider("secret-provider-id")
    kb_id = str(uuid.uuid4())
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, enabled=True)
    await _insert_bundle(
        portal_sessionmaker,
        name="Friendly Assistant",
        description="A helpful preset",
        persona_id="secret-persona-name",
        chat_provider_id="secret-provider-id",
        kb_ids=[kb_id],
    )

    cookies, _headers = _auth(user_a)
    response = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert response.status_code == 200, response.text

    entries = response.json()["agent_bundles"]
    assert len(entries) == 1
    entry = entries[0]
    assert set(entry.keys()) == {
        "bundle_id",
        "name",
        "description",
        "sort_order",
        "is_default",
    }
    serialized = json.dumps(entry)
    assert "secret-persona-name" not in serialized
    assert "secret-provider-id" not in serialized
    assert kb_id not in serialized


# ---------------------------------------------------------------------------
# 7. User B cannot read or bind on user A's session.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_b_cannot_read_or_bind_on_user_a_session(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, user_b = two_users
    bundle_id = await _insert_bundle(portal_sessionmaker)

    cookies_a, headers_a = _auth(user_a)
    session_id = await _create_session(client, cookies_a, headers_a)

    cookies_b, headers_b = _auth(user_b)

    read_resp = await client.get(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle", cookies=cookies_b
    )
    assert read_resp.status_code == 404, read_resp.text

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies_b,
        headers=headers_b,
    )
    assert bind_resp.status_code == 404, bind_resp.text

    unbind_resp = await client.delete(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        cookies=cookies_b,
        headers=headers_b,
    )
    assert unbind_resp.status_code == 404, unbind_resp.text

    # And A's own binding truly never changed via B's attempts.
    assert (
        await client.get(
            f"/api/v1/portal/sessions/{session_id}/agent-bundle", cookies=cookies_a
        )
    ).json() == {"agent_bundle": None}


# ---------------------------------------------------------------------------
# 8. Operator/end-user auth boundaries.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_bundle_routes_reject_portal_end_user_token(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    for method, path, kwargs in (
        ("GET", "/api/v1/portal/admin/agent-bundles", {}),
        (
            "POST",
            "/api/v1/portal/admin/agent-bundles",
            {"json": {"name": "hijacked"}},
        ),
        (
            "PUT",
            f"/api/v1/portal/admin/agent-bundles/{uuid.uuid4()}",
            {"json": {"name": "hijacked"}},
        ),
        (
            "PATCH",
            f"/api/v1/portal/admin/agent-bundles/{uuid.uuid4()}/enabled",
            {"json": {"enabled": False}},
        ),
    ):
        response = await client.request(
            method, path, cookies=cookies, headers=headers, **kwargs
        )
        assert response.status_code == 401, (method, path, response.text)


@pytest.mark.asyncio
async def test_enduser_bundle_routes_reject_operator_dashboard_token(
    client: httpx.AsyncClient,
):
    admin_headers = _admin_headers()

    list_resp = await client.get("/api/v1/portal/agent-bundles", headers=admin_headers)
    assert list_resp.status_code == 401, list_resp.text

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{uuid.uuid4()}/agent-bundle",
        json={"bundle_id": str(uuid.uuid4())},
        headers=admin_headers,
    )
    assert bind_resp.status_code == 401, bind_resp.text


# ---------------------------------------------------------------------------
# 9. Unbinding returns the session to the operator's defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unbind_restores_operator_defaults_on_every_dimension(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
    fake_chat_service: FakeChatService,
):
    user_a, _user_b = two_users
    fake_core_lifecycle.persona_mgr.add_persona("bundle-persona")
    fake_core_lifecycle.provider_manager.add_provider("bundle-provider")
    kb_id = str(uuid.uuid4())
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, enabled=True)
    bundle_id = await _insert_bundle(
        portal_sessionmaker,
        persona_id="bundle-persona",
        chat_provider_id="bundle-provider",
        kb_ids=[kb_id],
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    umo = _umo(user_a, session_id)

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert bind_resp.status_code == 200, bind_resp.text
    assert (
        await sp.session_get(umo, "provider_perf_chat_completion", default=_MISSING)
        == "bundle-provider"
    )

    unbind_resp = await client.delete(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        cookies=cookies,
        headers=headers,
    )
    assert unbind_resp.status_code == 200, unbind_resp.text
    assert unbind_resp.json() == {"agent_bundle": None}

    # Provider preference: removed entirely (absence is the safe/default
    # state -- falls through to the operator's own default_provider_id).
    assert (
        await sp.session_get(umo, "provider_perf_chat_completion", default=_MISSING)
        is _MISSING
    )

    # Persona: reset to the operator's own currently configured default,
    # never left at the bundle's previously-applied value.
    conv_mgr = fake_chat_service.conv_mgr
    cid = conv_mgr.session_conversation.get(umo)
    assert cid is not None
    assert (
        conv_mgr.conversations[cid].persona_id
        == fake_core_lifecycle.persona_mgr.default_persona_name
    )

    # KB binding: explicit empty, never merely absent (Phase 3's
    # default-open trap applies here too).
    assert await _kb_config(client, session_id, cookies) == _default_kb_config()

    # And the session's own binding read-back agrees there is no bundle.
    get_resp = await client.get(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle", cookies=cookies
    )
    assert get_resp.json() == {"agent_bundle": None}


# ---------------------------------------------------------------------------
# 10. Unsafe requests without CSRF are rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_without_csrf_header_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    bundle_id = await _insert_bundle(portal_sessionmaker)
    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        # Deliberately no X-Portal-CSRF-Token header.
    )
    assert response.status_code == 403, response.text
    assert (
        await client.get(
            f"/api/v1/portal/sessions/{session_id}/agent-bundle", cookies=cookies
        )
    ).json() == {"agent_bundle": None}


@pytest.mark.asyncio
async def test_unbind_without_csrf_header_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.delete(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        cookies=cookies,
        # No CSRF header.
    )
    assert response.status_code == 403, response.text


# ---------------------------------------------------------------------------
# 11. KB whitelist smuggling: a bundle can never bind a non-whitelisted
#     corpus, whether validated at admin-create time or at apply time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_silently_drops_non_whitelisted_kb_id_at_apply_time(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    """A bundle row inserted directly (bypassing admin-time validation --
    simulating an admin having whitelisted a KB that was since revoked)
    must never bind the no-longer-whitelisted id to a session, even though
    the bundle itself stays selectable (see the dangling-KB contrast test
    above)."""
    user_a, _user_b = two_users
    whitelisted_id = str(uuid.uuid4())
    revoked_id = str(uuid.uuid4())
    await _grant_kb(portal_sessionmaker, kb_id=whitelisted_id, enabled=True)
    await _grant_kb(portal_sessionmaker, kb_id=revoked_id, enabled=False)
    bundle_id = await _insert_bundle(
        portal_sessionmaker, kb_ids=[whitelisted_id, revoked_id]
    )

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    bound = await _kb_config(client, session_id, cookies)
    assert bound["kb_ids"] == [whitelisted_id]
    assert revoked_id not in bound["kb_ids"]


@pytest.mark.asyncio
async def test_admin_create_bundle_rejects_non_whitelisted_kb_id(
    client: httpx.AsyncClient,
):
    """The operator-facing create route itself refuses a bundle that
    references a ``kb_id`` with no current, enabled ``PortalKbVisibility``
    row -- validated immediately, not silently truncated (module
    docstring's KB two-point-validation, point 1)."""
    response = await client.post(
        "/api/v1/portal/admin/agent-bundles",
        json={"name": "Sneaky Bundle", "kb_ids": [str(uuid.uuid4())]},
        headers=_admin_headers(),
    )
    assert response.status_code == 400, response.text


# ---------------------------------------------------------------------------
# Bonus: operator bundle CRUD roundtrip, so the harness (fake persona/
# provider managers, admin auth wiring) is independently proven correct --
# every rejection test above implicitly trusts that a correctly-configured
# bundle CAN be created, listed, updated, and disabled.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_bundle_crud_roundtrip(
    client: httpx.AsyncClient, fake_core_lifecycle: FakeCoreLifecycle
):
    fake_core_lifecycle.persona_mgr.add_persona("assistant-v1")
    fake_core_lifecycle.provider_manager.add_provider("provider-v1")
    admin_headers = _admin_headers()

    create_resp = await client.post(
        "/api/v1/portal/admin/agent-bundles",
        json={
            "name": "Roundtrip Bundle",
            "persona_id": "assistant-v1",
            "chat_provider_id": "provider-v1",
            "kb_ids": [],
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert create_resp.status_code == 200, create_resp.text
    created = create_resp.json()
    bundle_id = created["bundle_id"]
    assert created["persona_id"] == "assistant-v1"
    assert created["chat_provider_id"] == "provider-v1"
    assert created["dangling"] is False

    list_resp = await client.get(
        "/api/v1/portal/admin/agent-bundles", headers=admin_headers
    )
    assert bundle_id in {b["bundle_id"] for b in list_resp.json()["agent_bundles"]}

    get_resp = await client.get(
        f"/api/v1/portal/admin/agent-bundles/{bundle_id}", headers=admin_headers
    )
    assert get_resp.status_code == 200, get_resp.text

    update_resp = await client.put(
        f"/api/v1/portal/admin/agent-bundles/{bundle_id}",
        json={
            "name": "Roundtrip Bundle Renamed",
            "persona_id": None,
            "chat_provider_id": "provider-v1",
            "kb_ids": [],
            "enabled": True,
        },
        headers=admin_headers,
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["name"] == "Roundtrip Bundle Renamed"
    assert update_resp.json()["persona_id"] is None

    disable_resp = await client.patch(
        f"/api/v1/portal/admin/agent-bundles/{bundle_id}/enabled",
        json={"enabled": False},
        headers=admin_headers,
    )
    assert disable_resp.status_code == 200, disable_resp.text
    assert disable_resp.json()["enabled"] is False

    # A disabled bundle must disappear from the admin list's own enabled
    # flag too (the same row, not a second one) -- re-fetch to confirm the
    # PATCH actually persisted rather than just echoing the request back.
    refetched = await client.get(
        f"/api/v1/portal/admin/agent-bundles/{bundle_id}", headers=admin_headers
    )
    assert refetched.json()["enabled"] is False


@pytest.mark.asyncio
async def test_admin_disabled_bundle_disappears_from_end_user_listing(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    admin_headers = _admin_headers()

    create_resp = await client.post(
        "/api/v1/portal/admin/agent-bundles",
        json={"name": "Will Be Disabled", "kb_ids": [], "enabled": True},
        headers=admin_headers,
    )
    bundle_id = create_resp.json()["bundle_id"]

    cookies, _headers = _auth(user_a)
    before = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert bundle_id in {b["bundle_id"] for b in before.json()["agent_bundles"]}

    await client.patch(
        f"/api/v1/portal/admin/agent-bundles/{bundle_id}/enabled",
        json={"enabled": False},
        headers=admin_headers,
    )

    after = await client.get("/api/v1/portal/agent-bundles", cookies=cookies)
    assert bundle_id not in {b["bundle_id"] for b in after.json()["agent_bundles"]}


@pytest.mark.asyncio
async def test_admin_update_unknown_bundle_id_is_404(client: httpx.AsyncClient):
    response = await client.put(
        f"/api/v1/portal/admin/agent-bundles/{uuid.uuid4()}",
        json={"name": "Nope", "kb_ids": []},
        headers=_admin_headers(),
    )
    assert response.status_code == 404, response.text


# ---------------------------------------------------------------------------
# The cap that matters is per SESSION, from either binding path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_clamps_kb_ids_to_the_per_session_cap(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    monkeypatch,
):
    """A bundle cannot apply more knowledge bases than a direct bind permits.

    ``max_kb_ids_per_bundle`` only *defaults* to ``max_kb_ids_per_session``;
    they are independent settings. An operator who tightens the session cap
    without also tightening the bundle cap would otherwise let a bundle
    authored under the looser limit bind more corpora than
    ``kb_visibility_service.bind_session`` allows -- the same session ending up
    over the cap depending only on which route the user took.

    Not an authorization hole (every id here is still operator-whitelisted),
    but the per-session cap exists for context cost and has to hold from both
    paths. Removing the clamp in ``bind_session`` makes this fail.
    """
    user_a, _user_b = two_users

    kb_ids = [str(uuid.uuid4()) for _ in range(3)]
    for kb_id in kb_ids:
        await _grant_kb(portal_sessionmaker, kb_id=kb_id, enabled=True)
    # Authored while the bundle cap still allowed three.
    bundle_id = await _insert_bundle(portal_sessionmaker, kb_ids=kb_ids)

    # The operator later tightens the per-session cap to one.
    import dataclasses

    tightened = dataclasses.replace(get_portal_settings(), max_kb_ids_per_session=1)
    monkeypatch.setattr(agent_bundle_service, "get_portal_settings", lambda: tightened)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/agent-bundle",
        json={"bundle_id": bundle_id},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200, response.text

    bound = await _kb_config(client, session_id, cookies)
    assert len(bound["kb_ids"]) == 1, (
        f"bundle bound {len(bound['kb_ids'])} knowledge bases despite a "
        f"per-session cap of 1: {bound['kb_ids']}"
    )
    assert bound["kb_ids"][0] in kb_ids
