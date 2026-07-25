"""Guardrail tests: per-user chat/session isolation (IDOR) for the portal
module's Phase 1 exit criteria.

Every route under ``astrbot.portal.api.chat_routes`` is exercised through the
*real* production stack -- ``portal_router`` -> ``core_facade`` -> the
ownership gate in ``core_facade._owned_session_row`` / the ``chat_runs``
dict check -- with only ``ChatService`` itself replaced by a small
duck-typed fake (``FakeChatService``). This keeps the tests fast and
independent of a real LLM provider/conversation manager, while still
proving the actual security-critical code (``core_facade.py``,
``chat_routes.py``) behaves correctly: every ownership failure must surface
as 404 (never 403 -- a 403 would leak whether the resource exists at all),
and a streaming-run IDOR attempt must never call into the underlying
stream generator at all, so literally zero bytes of another user's run are
ever produced.

Auth for these tests is set up by minting access tokens directly via
``astrbot.portal.auth.security.mint_access_token`` and inserting the
corresponding ``PortalUser`` rows straight into a temp portal database --
bypassing ``session_service.issue_token_pair`` on purpose, since that
function's real mint path has an independently-reported, already-tracked
bug (see ``tests/test_portal_auth.py``'s module docstring and this task's
``gaps``) that is orthogonal to the ownership/IDOR guarantees this file is
about.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.portal.api import chat_routes
from astrbot.portal.api.router import portal_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalUser

PORTAL_TEST_SECRET = "portal-isolation-test-secret-32bytes!"


# ---------------------------------------------------------------------------
# FakeChatService: implements exactly the surface core_facade.py calls on
# ``request.app.state.services.chat``.
# ---------------------------------------------------------------------------


@dataclass
class FakeSession:
    session_id: str
    creator: str
    platform_id: str = "webchat"
    display_name: str = ""
    is_group: int = 0
    created_at: str = "2026-01-01T00:00:00+00:00"
    updated_at: str = "2026-01-01T00:00:00+00:00"


async def _fake_stream(chunks: list[str]) -> AsyncIterator[str]:
    for chunk in chunks:
        yield chunk


@dataclass
class FakeRun:
    username: str


class FakeChatService:
    """Duck-typed stand-in for ``astrbot.dashboard.services.chat_service.ChatService``.

    Only implements the methods/attributes ``core_facade.py`` actually
    touches (see ``test_portal_core_facade_contract.py`` for the pinned
    signatures those real methods must keep matching).
    """

    def __init__(self) -> None:
        self.sessions_by_id: dict[str, FakeSession] = {}
        self.chat_runs: dict[str, FakeRun] = {}
        self.build_chat_stream_calls: list[dict[str, Any]] = []
        self.build_chat_run_stream_calls: list[tuple[str, str]] = []
        self.deleted_session_ids: list[str] = []
        self.stopped_session_ids: list[str] = []
        self.renamed: list[tuple[str, str]] = []
        self.db = _FakeDb(self)

    def add_session(self, session: FakeSession) -> None:
        self.sessions_by_id[session.session_id] = session

    async def new_session(self, username: str, platform_id: str) -> dict:
        session = FakeSession(session_id=str(uuid.uuid4()), creator=username)
        self.add_session(session)
        return {"session_id": session.session_id, "platform_id": platform_id}

    async def get_sessions(self, username: str, platform_id: str | None) -> list[dict]:
        return [
            {
                "session_id": s.session_id,
                "platform_id": s.platform_id,
                "creator": s.creator,
                "display_name": s.display_name,
                "is_group": s.is_group,
                "created_at": s.created_at,
                "updated_at": s.updated_at,
            }
            for s in self.sessions_by_id.values()
            if s.creator == username
        ]

    async def get_session(self, username: str, session_id: str) -> dict:
        return {
            "history": [],
            "threads": [],
            "is_running": False,
            "active_runs": [],
        }

    async def update_session_display_name(
        self, username: str, session_id: str, display_name: str
    ) -> dict:
        self.renamed.append((session_id, display_name))
        self.sessions_by_id[session_id].display_name = display_name
        return {}

    async def delete_webchat_session(self, username: str, session_id: str) -> None:
        self.deleted_session_ids.append(session_id)
        self.sessions_by_id.pop(session_id, None)

    async def delete_session_internal(self, session: FakeSession, username: str) -> None:
        self.deleted_session_ids.append(session.session_id)
        self.sessions_by_id.pop(session.session_id, None)

    async def build_chat_stream(self, username: str, post_data: dict) -> AsyncIterator[str]:
        self.build_chat_stream_calls.append(post_data)
        return _fake_stream(
            [
                'data: {"type": "plain", "text": "hi"}\n\n',
                'data: {"type": "end"}\n\n',
            ]
        )

    async def build_chat_run_stream(self, username: str, run_id: str) -> AsyncIterator[str]:
        self.build_chat_run_stream_calls.append((username, run_id))
        return _fake_stream(['data: {"type": "plain", "text": "resumed"}\n\n'])

    async def stop_session(self, username: str, session_id: str) -> dict:
        self.stopped_session_ids.append(session_id)
        return {"stopped_count": 1}

    async def update_message(self, username: str, data: dict) -> dict:
        return {"message": None, "needs_regenerate": True, "truncated_after_message": True}

    async def prepare_regenerate_message_payload(self, username: str, data: dict) -> dict:
        return {"message": [], "session_id": data["session_id"]}


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


# ---------------------------------------------------------------------------
# App / client plumbing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-isolation-test.db",
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
def app(portal_sessionmaker, fake_chat_service: FakeChatService, monkeypatch) -> FastAPI:
    from types import SimpleNamespace

    application = FastAPI()
    application.include_router(portal_router, prefix="/api/v1")
    application.state.services = SimpleNamespace(chat=fake_chat_service)
    application.state.portal_jwt_secret = PORTAL_TEST_SECRET

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    # chat_routes.py calls portal_async_sessionmaker() directly (not via
    # FastAPI Depends()) for its concurrency-slot bookkeeping -- redirect
    # that module-level name to our temp test database too.
    monkeypatch.setattr(chat_routes, "portal_async_sessionmaker", portal_sessionmaker)

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


def _auth(user_id: str) -> tuple[dict, dict]:
    """Return (cookies, headers) for an authenticated + CSRF-ready request."""
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


# ---------------------------------------------------------------------------
# 1. Two accounts: user B requesting user A's session_id gets 404, not 403.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def two_users_with_owned_session(portal_sessionmaker, fake_chat_service):
    user_a = f"user-a-{uuid.uuid4().hex}"
    user_b = f"user-b-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    await _insert_user(portal_sessionmaker, user_id=user_b)
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)
    return user_a, user_b, session.session_id


@pytest.mark.asyncio
async def test_owner_can_read_their_own_session(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    user_a, _user_b, session_id = two_users_with_owned_session
    cookies, _headers = _auth(user_a)

    response = await client.get(
        f"/api/v1/portal/chat/sessions/{session_id}", cookies=cookies
    )
    assert response.status_code == 200, response.text


@pytest.mark.asyncio
async def test_other_user_get_session_is_404_not_403(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    _user_a, user_b, session_id = two_users_with_owned_session
    cookies, _headers = _auth(user_b)

    response = await client.get(
        f"/api/v1/portal/chat/sessions/{session_id}", cookies=cookies
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_other_user_patch_session_is_404_not_403(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    _user_a, user_b, session_id = two_users_with_owned_session
    cookies, headers = _auth(user_b)

    response = await client.patch(
        f"/api/v1/portal/chat/sessions/{session_id}",
        json={"display_name": "hijacked"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_other_user_delete_session_is_404_not_403(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    _user_a, user_b, session_id = two_users_with_owned_session
    cookies, headers = _auth(user_b)

    response = await client.delete(
        f"/api/v1/portal/chat/sessions/{session_id}",
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_other_user_send_message_is_404_not_403(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    _user_a, user_b, session_id = two_users_with_owned_session
    cookies, headers = _auth(user_b)

    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session_id}/messages",
        json={"message": "hello from an attacker"},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


@pytest.mark.asyncio
async def test_other_user_stop_session_is_404_not_403(
    client: httpx.AsyncClient, two_users_with_owned_session
):
    _user_a, user_b, session_id = two_users_with_owned_session
    cookies, headers = _auth(user_b)

    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session_id}/stop",
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# 2. Streaming-run IDOR: the single most important test in this file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_streaming_run_idor_returns_404_and_zero_stream_bytes(
    client: httpx.AsyncClient, portal_sessionmaker, fake_chat_service: FakeChatService
):
    user_a = f"user-a-{uuid.uuid4().hex}"
    user_b = f"user-b-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    await _insert_user(portal_sessionmaker, user_id=user_b)

    run_id = str(uuid.uuid4())
    fake_chat_service.chat_runs[run_id] = FakeRun(username=user_a)

    cookies_b, _headers_b = _auth(user_b)
    response = await client.get(
        f"/api/v1/portal/chat/runs/{run_id}/stream", cookies=cookies_b
    )

    assert response.status_code == 404, response.text
    assert response.status_code != 403
    # Never even attempted to open the stream for the wrong owner.
    assert fake_chat_service.build_chat_run_stream_calls == []
    # No SSE bytes at all leaked to the attacker.
    assert response.content == b"" or "text/event-stream" not in response.headers.get(
        "content-type", ""
    )
    assert "resumed" not in response.text


@pytest.mark.asyncio
async def test_streaming_run_owner_can_read_their_own_run(
    client: httpx.AsyncClient, portal_sessionmaker, fake_chat_service: FakeChatService
):
    user_a = f"user-a-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)

    run_id = str(uuid.uuid4())
    fake_chat_service.chat_runs[run_id] = FakeRun(username=user_a)

    cookies_a, _headers_a = _auth(user_a)
    response = await client.get(
        f"/api/v1/portal/chat/runs/{run_id}/stream", cookies=cookies_a
    )

    assert response.status_code == 200, response.text
    assert "text/event-stream" in response.headers.get("content-type", "")
    assert "resumed" in response.text
    assert fake_chat_service.build_chat_run_stream_calls == [(user_a, run_id)]


@pytest.mark.asyncio
async def test_streaming_run_unknown_run_id_is_404(client: httpx.AsyncClient, portal_sessionmaker):
    user_a = f"user-a-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    cookies_a, _headers_a = _auth(user_a)

    response = await client.get(
        f"/api/v1/portal/chat/runs/{uuid.uuid4()}/stream", cookies=cookies_a
    )
    assert response.status_code == 404
    assert response.status_code != 403


# ---------------------------------------------------------------------------
# 3. selected_provider / selected_model must never reach the agent payload.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_selected_provider_and_model_are_stripped_from_send_message(
    client: httpx.AsyncClient, portal_sessionmaker, fake_chat_service: FakeChatService
):
    user_a = f"user-a-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)

    cookies, headers = _auth(user_a)
    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages",
        json={
            "message": "hello",
            "selected_provider": "attacker-chosen-provider",
            "selected_model": "attacker-chosen-model",
        },
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(fake_chat_service.build_chat_stream_calls) == 1
    forwarded_payload = fake_chat_service.build_chat_stream_calls[0]
    assert "selected_provider" not in forwarded_payload
    assert "selected_model" not in forwarded_payload
    assert forwarded_payload["session_id"] == session.session_id
    assert forwarded_payload["message"] == "hello"


@pytest.mark.asyncio
async def test_selected_provider_and_model_are_stripped_from_regenerate(
    client: httpx.AsyncClient, portal_sessionmaker, fake_chat_service: FakeChatService
):
    user_a = f"user-a-{uuid.uuid4().hex}"
    await _insert_user(portal_sessionmaker, user_id=user_a)
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)

    cookies, headers = _auth(user_a)
    response = await client.post(
        f"/api/v1/portal/chat/sessions/{session.session_id}/messages/1/regenerate",
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(fake_chat_service.build_chat_stream_calls) == 1
    forwarded_payload = fake_chat_service.build_chat_stream_calls[0]
    assert "selected_provider" not in forwarded_payload
    assert "selected_model" not in forwarded_payload
