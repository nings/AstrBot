"""Guardrail tests for the portal module's Phase 3 KB visibility whitelist +
session-binding path.

Every handler here is exercised through the *real* production stack --
``portal_router`` (``kb_routes.py`` + ``admin_routes.py``) ->
``kb_visibility_service`` -> ``core_facade`` -> the real, process-wide
``astrbot.core.sp`` (``SharedPreferences``) singleton and a fake, duck-typed
``KnowledgeBaseManager`` -- with only ``ChatService`` and
``core_lifecycle.kb_manager`` replaced by small fakes. This mirrors exactly
the approach ``test_portal_attachment_isolation.py``/
``test_portal_chat_isolation.py`` already take: real ``sp`` calls are not
mocked away (Phase 1/2 already rely on this -- ``_owned_session_row`` has
called into the real ``sp`` singleton since Phase 3 added the kb_config
backfill, and every session_id used below is a fresh random uuid, so there is
no cross-test collision risk on the shared ``umo`` keyspace).

403 vs 404 (engineering rule 8) -- this suite asserts the convention the
implementation actually chose, not the plan's literal wording
-----------------------------------------------------------------------
``design/portal-multiuser-plan.md``'s Phase 3 acceptance criterion 1 says a
non-whitelisted ``kb_id`` should return 403. The shipped implementation
(``kb_visibility_service.py``'s own module docstring, and ``kb_routes.py``'s
own module docstring, both read in full before writing this file)
deliberately does **not** follow that literal wording: every KB
authorization/existence failure -- "never whitelisted", "whitelisted but
revoked", "whitelisted but deleted from core since", and "session not owned
by this caller" alike -- raises the identical
``core_facade.ResourceNotFoundError`` and is mapped to a single,
indistinguishable HTTP **404**, for the same "never let a response be an
existence oracle" reason invariant 2 of ``core_facade.py``'s own module
docstring gives for every other ownership check in this codebase. This test
file asserts 404 (never 403) for every one of those cases, matching the
convention actually in force in the shipped code -- see this task's report
for the explicit confirmation that this is a deliberate, documented
implementation choice and not an oversight this suite is failing to catch.
``KbSelectionDisabledError`` (the feature's blanket master switch, an
operator on/off toggle rather than a per-resource decision) is the one
exception that does map to 403 in ``kb_routes.py`` -- not exercised in this
file since it is orthogonal to the whitelist-authorization tests below.

The default-open trap (criterion 8) is the single most important test in
this file -- see its own docstring further down for why "missing kb_config"
and "present-but-empty kb_config" are not the same thing at all.
"""

from __future__ import annotations

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
from astrbot.portal.api.router import portal_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalKbVisibility, PortalUser
from astrbot.portal.services import (
    attachment_service,
    core_facade,
    kb_visibility_service,
)

PORTAL_TEST_SECRET = "portal-kb-whitelist-test-secret-32bytes!"
DASHBOARD_JWT_SECRET = "dashboard-kb-whitelist-test-secret-32b!"

_MISSING = object()
"""Local sentinel for "this sp key was never written at all" -- distinct
from any real, falsy ``kb_config`` value a test might otherwise mistake for
"absent"."""


# ---------------------------------------------------------------------------
# FakeChatService: implements exactly the surface core_facade.py touches on
# a real ChatService (see test_portal_chat_isolation.py for the full
# original shape; only the subset kb_routes.py's call graph reaches is
# reproduced here).
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


class FakeChatService:
    def __init__(self) -> None:
        self.sessions_by_id: dict[str, FakeSession] = {}
        self.db = _FakeDb(self)

    def add_session(self, session: FakeSession) -> None:
        self.sessions_by_id[session.session_id] = session

    async def new_session(self, username: str, platform_id: str) -> dict:
        session = FakeSession(session_id=str(uuid.uuid4()), creator=username)
        self.add_session(session)
        return {"session_id": session.session_id, "platform_id": platform_id}


# ---------------------------------------------------------------------------
# Fake core KB manager: implements exactly the surface core_facade.py's
# KB-resolution helpers touch on the real KnowledgeBaseManager/KBHelper
# (astrbot/core/knowledge_base/kb_mgr.py, kb_helper.py) -- an in-memory dict
# keyed by kb_id, ``get_kb`` a plain lookup, ``list_kbs`` every value's
# ``.kb``. A kb_id simply absent from this fake models "deleted from core /
# never existed", exactly like the real one.
# ---------------------------------------------------------------------------


@dataclass
class FakeCoreKb:
    kb_id: str
    kb_name: str
    description: str | None = None
    emoji: str | None = "📚"
    doc_count: int = 0
    chunk_count: int = 0


@dataclass
class FakeKbHelper:
    kb: FakeCoreKb
    init_error: str | None = None


class FakeKbManager:
    def __init__(self) -> None:
        self.kb_insts: dict[str, FakeKbHelper] = {}

    def add_kb(self, kb: FakeCoreKb, *, init_error: str | None = None) -> None:
        self.kb_insts[kb.kb_id] = FakeKbHelper(kb=kb, init_error=init_error)

    async def get_kb(self, kb_id: str) -> FakeKbHelper | None:
        return self.kb_insts.get(kb_id)

    async def list_kbs(self) -> list[FakeCoreKb]:
        return [helper.kb for helper in self.kb_insts.values()]


@dataclass
class FakeCoreLifecycle:
    kb_manager: FakeKbManager = field(default_factory=FakeKbManager)


# ---------------------------------------------------------------------------
# App / client plumbing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-kb-whitelist-test.db",
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
    # Only needed so astrbot.dashboard.api.auth.require_system_scope (the
    # admin routes' gate) can decode a real operator dashboard JWT -- the
    # admin routes never touch app.state.db in the paths these tests
    # exercise (no API-key fallback is ever attempted: no test here sends a
    # malformed Bearer token that would fall through to that branch).
    application.state.jwt_secret = DASHBOARD_JWT_SECRET

    # require_scope/require_system_scope raise astrbot.dashboard.responses
    # .ApiError (not fastapi.HTTPException) on auth failure. The real
    # dashboard app registers a handler for this in
    # astrbot.dashboard.api.app.create_dashboard_asgi_app -- reproduced here
    # verbatim so admin-route auth failures surface as a clean 401/403 JSON
    # response instead of an unhandled-exception 500 in this bare test app.
    @application.exception_handler(ApiError)
    async def _api_error_handler(_request, exc: ApiError):
        return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    # chat_routes.py, attachment_service.py, core_facade.py, and
    # kb_visibility_service.py all call `portal_async_sessionmaker()`
    # directly (module-level name, not a FastAPI Depends()) -- redirect
    # every one of them to our temp test database.
    monkeypatch.setattr(chat_routes, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(attachment_service, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(core_facade, "portal_async_sessionmaker", portal_sessionmaker)
    monkeypatch.setattr(
        kb_visibility_service, "portal_async_sessionmaker", portal_sessionmaker
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


async def _grant_kb(
    sessionmaker,
    *,
    kb_id: str,
    kb_name: str = "Test KB",
    enabled: bool = True,
    display_name: str | None = None,
    description: str | None = None,
) -> None:
    """Insert a ``PortalKbVisibility`` row directly (bypassing the admin HTTP
    layer -- that layer gets its own dedicated auth-boundary tests further
    down; every whitelist-*enforcement* test below only cares about the row
    existing in a particular shape, not about how it got there)."""
    async with sessionmaker() as session:
        session.add(
            PortalKbVisibility(
                kb_id=kb_id,
                kb_name=kb_name,
                enabled=enabled,
                display_name=display_name,
                description=description,
                granted_by="test-operator",
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
    """Return (cookies, headers) for an authenticated end-user + CSRF-ready request."""
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


def _admin_headers(username: str = "test-operator") -> dict:
    """A real, correctly-signed operator dashboard JWT, sent the same way a
    genuine dashboard client would: as an ``Authorization: Bearer`` header,
    never through a cookie the portal end-user auth would ever look at."""
    token = jwt.encode({"username": username}, DASHBOARD_JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


async def _create_session(client: httpx.AsyncClient, cookies: dict, headers: dict) -> str:
    """Create a session through the real production route
    (``POST /chat/sessions`` -> ``core_facade.create_session``) so it starts
    life with an explicit, already-backfilled ``kb_config`` -- exactly the
    state every real portal session is in from the moment of creation
    (criterion 8)."""
    response = await client.post(
        "/api/v1/portal/chat/sessions", cookies=cookies, headers=headers
    )
    assert response.status_code == 201, response.text
    return response.json()["session_id"]


async def _get_kb_config(client: httpx.AsyncClient, session_id: str, cookies: dict) -> dict:
    response = await client.get(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base", cookies=cookies
    )
    assert response.status_code == 200, response.text
    return response.json()


def _default_kb_config() -> dict[str, Any]:
    settings = get_portal_settings()
    return {"kb_ids": [], "top_k": settings.default_kb_top_k}


# ---------------------------------------------------------------------------
# 1. Binding a kb_id that is not in the whitelist is refused.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_non_whitelisted_kb_id_is_refused_and_session_unchanged(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)
    assert baseline == _default_kb_config()

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [str(uuid.uuid4())]},
        cookies=cookies,
        headers=headers,
    )

    # This is the implementation's own documented rule-8 decision (see this
    # file's module docstring): 404, never 403, for a non-whitelisted kb_id.
    assert response.status_code == 404, response.text
    assert response.status_code != 403

    after = await _get_kb_config(client, session_id, cookies)
    assert after == baseline, "a refused bind must never mutate the session's kb_config"


# ---------------------------------------------------------------------------
# 2. Binding a whitelisted-but-disabled entry is refused.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_whitelisted_but_disabled_kb_is_refused(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, _user_b = two_users
    kb_id = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id, kb_name="Revoked KB"))
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, kb_name="Revoked KB", enabled=False)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id]},
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 404, response.text
    assert response.status_code != 403
    assert await _get_kb_config(client, session_id, cookies) == baseline


# ---------------------------------------------------------------------------
# 3. Binding a whitelisted entry whose kb_id no longer exists in core is
#    refused (the "dangling" whitelist row case).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_whitelisted_but_dangling_kb_is_refused(
    client: httpx.AsyncClient, two_users: tuple[str, str], portal_sessionmaker
):
    user_a, _user_b = two_users
    kb_id = str(uuid.uuid4())
    # Whitelisted (enabled=True) but deliberately never added to the fake
    # core KB manager -- models "granted, then deleted from core since".
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, kb_name="Deleted KB", enabled=True)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id]},
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 404, response.text
    assert response.status_code != 403
    assert await _get_kb_config(client, session_id, cookies) == baseline


# ---------------------------------------------------------------------------
# 4. A mixed request (one valid id + one non-whitelisted id) binds NOTHING.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mixed_valid_and_invalid_kb_ids_binds_nothing(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, _user_b = two_users
    good_kb_id = str(uuid.uuid4())
    bad_kb_id = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=good_kb_id, kb_name="Good KB"))
    await _grant_kb(portal_sessionmaker, kb_id=good_kb_id, kb_name="Good KB", enabled=True)
    # bad_kb_id is neither granted nor live -- never whitelisted at all.

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [good_kb_id, bad_kb_id]},
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 404, response.text
    # The whole point: NOT a partial bind of just good_kb_id.
    after = await _get_kb_config(client, session_id, cookies)
    assert after == baseline
    assert after["kb_ids"] == []


# ---------------------------------------------------------------------------
# 5. GET /portal/knowledge-bases returns only enabled, live whitelist
#    entries -- never leaking non-whitelisted or dangling KBs.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_knowledge_bases_returns_only_enabled_live_whitelist_entries(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, _user_b = two_users

    visible_id = str(uuid.uuid4())
    disabled_id = str(uuid.uuid4())
    dangling_id = str(uuid.uuid4())
    never_whitelisted_id = str(uuid.uuid4())

    fake_core_lifecycle.kb_manager.add_kb(
        FakeCoreKb(kb_id=visible_id, kb_name="Visible KB", doc_count=3, chunk_count=42)
    )
    fake_core_lifecycle.kb_manager.add_kb(
        FakeCoreKb(kb_id=disabled_id, kb_name="Disabled KB")
    )
    fake_core_lifecycle.kb_manager.add_kb(
        FakeCoreKb(kb_id=never_whitelisted_id, kb_name="Never Whitelisted KB")
    )
    # dangling_id is deliberately NOT added to the fake core KB manager.

    await _grant_kb(portal_sessionmaker, kb_id=visible_id, kb_name="Visible KB", enabled=True)
    await _grant_kb(
        portal_sessionmaker, kb_id=disabled_id, kb_name="Disabled KB", enabled=False
    )
    await _grant_kb(
        portal_sessionmaker, kb_id=dangling_id, kb_name="Dangling KB", enabled=True
    )
    # never_whitelisted_id: exists live in core, but no PortalKbVisibility
    # row at all -- the operator simply never granted it.

    cookies, _headers = _auth(user_a)
    response = await client.get("/api/v1/portal/knowledge-bases", cookies=cookies)

    assert response.status_code == 200, response.text
    entries = response.json()["knowledge_bases"]
    ids = {entry["kb_id"] for entry in entries}
    assert ids == {visible_id}
    assert len(entries) == 1
    assert entries[0]["doc_count"] == 3
    assert entries[0]["chunk_count"] == 42
    assert entries[0]["available"] is True
    # Never the operator-facing raw kb_name/granted_by surface.
    assert "granted_by" not in entries[0]
    assert "kb_name" not in entries[0]


# ---------------------------------------------------------------------------
# 6. User B cannot read or write user A's session binding.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_user_b_cannot_read_or_write_user_a_session_kb_binding(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, user_b = two_users
    kb_id = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id, kb_name="Shared KB"))
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, kb_name="Shared KB", enabled=True)

    cookies_a, headers_a = _auth(user_a)
    session_id = await _create_session(client, cookies_a, headers_a)

    cookies_b, headers_b = _auth(user_b)

    read_resp = await client.get(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base", cookies=cookies_b
    )
    assert read_resp.status_code == 404, read_resp.text
    assert read_resp.status_code != 403

    write_resp = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id]},
        cookies=cookies_b,
        headers=headers_b,
    )
    assert write_resp.status_code == 404, write_resp.text
    assert write_resp.status_code != 403

    # And A's own binding truly never changed via B's attempt.
    assert await _get_kb_config(client, session_id, cookies_a) == _default_kb_config()


# ---------------------------------------------------------------------------
# 7. Exceeding the configured maximum bound-KB count is refused; top_k is
#    validated against PortalSettings.max_kb_top_k (rejected, not silently
#    clamped -- see this test's docstring for why "clamped" doesn't apply to
#    the shipped implementation).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_exceeding_max_kb_count_is_refused(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
    monkeypatch,
):
    import dataclasses

    user_a, _user_b = two_users
    base_settings = get_portal_settings()
    capped_settings = dataclasses.replace(base_settings, max_kb_ids_per_session=1)
    monkeypatch.setattr(
        kb_visibility_service, "get_portal_settings", lambda: capped_settings
    )

    kb_id_1 = str(uuid.uuid4())
    kb_id_2 = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id_1, kb_name="KB One"))
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id_2, kb_name="KB Two"))
    await _grant_kb(portal_sessionmaker, kb_id=kb_id_1, kb_name="KB One")
    await _grant_kb(portal_sessionmaker, kb_id=kb_id_2, kb_name="KB Two")

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id_1, kb_id_2]},
        cookies=cookies,
        headers=headers,
    )

    assert response.status_code == 400, response.text
    assert await _get_kb_config(client, session_id, cookies) == baseline


@pytest.mark.asyncio
async def test_top_k_out_of_range_is_rejected(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    """``kb_visibility_service.bind_session`` deliberately REJECTS an
    out-of-range ``top_k`` (``InvalidTopKError`` -> HTTP 400) rather than
    silently clamping it down to ``PortalSettings.max_kb_top_k`` -- see that
    function's own docstring ("a client that sent an invalid top_k gets a
    clear rejection, not a silently different number written on their
    behalf"). This test therefore asserts "validated/rejected", the
    convention actually shipped, rather than "clamped".
    """
    user_a, _user_b = two_users
    kb_id = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id, kb_name="Some KB"))
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, kb_name="Some KB")

    settings = get_portal_settings()
    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)
    baseline = await _get_kb_config(client, session_id, cookies)

    too_large = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id], "top_k": settings.max_kb_top_k + 1},
        cookies=cookies,
        headers=headers,
    )
    assert too_large.status_code == 400, too_large.text

    zero = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id], "top_k": 0},
        cookies=cookies,
        headers=headers,
    )
    assert zero.status_code == 400, zero.text

    negative = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id], "top_k": -1},
        cookies=cookies,
        headers=headers,
    )
    assert negative.status_code == 400, negative.text

    # None of the three rejected attempts wrote anything.
    assert await _get_kb_config(client, session_id, cookies) == baseline

    # Sanity: the same kb_id with an in-range top_k does succeed, proving
    # the rejections above are specifically about top_k, not the kb_id.
    ok = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id], "top_k": settings.max_kb_top_k},
        cookies=cookies,
        headers=headers,
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["top_k"] == settings.max_kb_top_k


# ---------------------------------------------------------------------------
# 8. The default-open trap. THE most important test in this file.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_new_session_has_explicit_empty_kb_config_not_missing(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    """A brand-new portal session must have an EXPLICIT ``kb_config`` --
    ``{"kb_ids": [], "top_k": ...}`` -- written into ``sp`` at creation time,
    not merely *behave as if* it has one on the next read.

    Why this distinction matters (verified ground truth,
    ``astrbot/core/tools/knowledge_base_tools.py:24-87``):
    ``retrieve_knowledge_base`` branches on
    ``if session_config and "kb_ids" in session_config`` -- a *missing*
    ``kb_config`` key takes the ``else`` branch and falls through to the
    operator's **global** ``kb_names`` config, silently handing an end user
    whatever knowledge bases the operator configured for the whole bot,
    including corpora never meant for portal traffic. Only a ``kb_config``
    that is explicitly *present*, with an empty ``kb_ids`` list, means "this
    session uses no knowledge base" to that function. So this test does NOT
    merely assert that ``GET .../knowledge-base`` *returns*
    ``{"kb_ids": [], ...}`` (a route can synthesize that response from
    nothing without ever persisting it) -- it reads the raw persisted value
    back out of the real ``sp`` singleton directly, by the same umo formula
    ``core_facade._session_umo``/``chat_service.build_webchat_unified_msg_origin``
    use, and asserts the key is genuinely present in storage, not merely
    absent-but-defaulted-on-read.
    """
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    session_id = await _create_session(client, cookies, headers)
    # Same umo formula core_facade._session_umo / chat_service's
    # build_webchat_unified_msg_origin use for a non-group WebChat session.
    umo = f"webchat:FriendMessage:webchat!{user_a}!{session_id}"

    persisted = await sp.session_get(umo, "kb_config", default=_MISSING)
    assert persisted is not _MISSING, (
        "kb_config must be explicitly written at session creation -- an "
        "absent key silently falls through to the operator's global "
        "kb_names config (the default-open trap), which is the #1 finding "
        "of this phase"
    )
    assert persisted == _default_kb_config()
    assert persisted["kb_ids"] == []


@pytest.mark.asyncio
async def test_preexisting_phase1_session_without_kb_config_is_backfilled_on_access(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    fake_chat_service: FakeChatService,
):
    """A session that predates Phase 3 (created by directly registering a
    ``PlatformSession`` row the way Phase 1's ``ChatService.new_session``
    would have, never going through Phase 3's ``core_facade.create_session``
    at all) has a genuinely ABSENT ``kb_config`` in ``sp`` until its first
    touch through this module -- simulated here by adding the fake session
    directly rather than via the ``POST /chat/sessions`` route (which always
    writes the explicit binding up front, see the sibling test above).

    Confirms the backfill (``core_facade._ensure_explicit_kb_config``,
    funneled through the session-ownership gate every session-scoped
    facade function calls) actually closes the hole for such a session: the
    key is genuinely absent beforehand, and genuinely present (not just
    computed-on-response) immediately after the first authorized access.
    """
    user_a, _user_b = two_users
    session = FakeSession(session_id=str(uuid.uuid4()), creator=user_a)
    fake_chat_service.add_session(session)
    umo = f"webchat:FriendMessage:webchat!{user_a}!{session.session_id}"

    before = await sp.session_get(umo, "kb_config", default=_MISSING)
    assert before is _MISSING, "test setup must start from a genuinely absent kb_config"

    cookies, _headers = _auth(user_a)
    response = await client.get(
        f"/api/v1/portal/sessions/{session.session_id}/knowledge-base", cookies=cookies
    )
    assert response.status_code == 200, response.text
    assert response.json() == _default_kb_config()

    after = await sp.session_get(umo, "kb_config", default=_MISSING)
    assert after is not _MISSING, "the backfill must actually persist, not just respond"
    assert after == _default_kb_config()


# ---------------------------------------------------------------------------
# 9. The two auth systems must not interoperate.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_endpoint_accepts_real_operator_token_baseline(
    client: httpx.AsyncClient,
):
    """Baseline/contrast for the two rejection tests below: a genuine
    operator dashboard JWT DOES work against the admin KB-visibility
    endpoint, so the rejection tests aren't vacuously true from a broken
    auth wiring rejecting everything indiscriminately."""
    response = await client.get(
        "/api/v1/portal/admin/kb-visibility", headers=_admin_headers()
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"kb_visibility": []}


@pytest.mark.asyncio
async def test_admin_endpoints_reject_portal_end_user_token(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    """The operator-only admin KB-visibility routes
    (``Depends(require_system_scope)``) must never authenticate an end-user
    portal session. A portal JWT lives only in the ``astrbot_portal_jwt``
    cookie under the portal module's own signing secret;
    ``require_system_scope`` looks exclusively at an ``Authorization``
    Bearer header or the *dashboard's* own cookie name, decoded with the
    *dashboard's* own secret -- neither of which a portal cookie satisfies."""
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)

    for method, path, kwargs in (
        ("GET", "/api/v1/portal/admin/kb-visibility", {}),
        ("GET", "/api/v1/portal/admin/kb-visibility/core-kbs", {}),
        (
            "PUT",
            f"/api/v1/portal/admin/kb-visibility/{uuid.uuid4()}",
            {"json": {"display_name": "hijacked"}},
        ),
        ("DELETE", f"/api/v1/portal/admin/kb-visibility/{uuid.uuid4()}", {}),
    ):
        response = await client.request(
            method, path, cookies=cookies, headers=headers, **kwargs
        )
        assert response.status_code == 401, (method, path, response.text)


@pytest.mark.asyncio
async def test_enduser_kb_endpoints_reject_operator_dashboard_token(
    client: httpx.AsyncClient,
):
    """Symmetrically, the end-user KB routes (``Depends(get_current_user)``)
    must never authenticate an operator's dashboard JWT: ``get_current_user``
    only ever looks at the ``astrbot_portal_jwt`` cookie, decoded with the
    portal module's own secret -- an ``Authorization`` Bearer header (where a
    real dashboard client sends its JWT) is never even inspected."""
    admin_headers = _admin_headers()

    list_resp = await client.get(
        "/api/v1/portal/knowledge-bases", headers=admin_headers
    )
    assert list_resp.status_code == 401, list_resp.text

    bind_resp = await client.post(
        f"/api/v1/portal/sessions/{uuid.uuid4()}/knowledge-base",
        json={"kb_ids": []},
        headers=admin_headers,
    )
    assert bind_resp.status_code == 401, bind_resp.text

    # Even the smuggled-cookie variant (copying the dashboard JWT into the
    # portal's own cookie name) must fail: wrong signing secret, wrong claim
    # shape (`username`, not `sub=<uuid>`).
    smuggled = await client.get(
        "/api/v1/portal/knowledge-bases",
        cookies={get_portal_settings().jwt_cookie_name: list(admin_headers.values())[0].removeprefix("Bearer ")},
    )
    assert smuggled.status_code == 401, smuggled.text


# ---------------------------------------------------------------------------
# 10. An unsafe request without a valid CSRF header is rejected.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_without_csrf_header_is_rejected(
    client: httpx.AsyncClient, two_users: tuple[str, str]
):
    user_a, _user_b = two_users
    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": []},
        cookies=cookies,
        # Deliberately no X-Portal-CSRF-Token header, even though the CSRF
        # cookie is present -- the double-submit check must still reject.
    )
    assert response.status_code == 403, response.text

    # And confirm it really never wrote anything either.
    assert await _get_kb_config(client, session_id, cookies) == _default_kb_config()


# ---------------------------------------------------------------------------
# Bonus: a straightforward happy-path bind, so the harness itself (fake KB
# manager wiring, whitelist grant helper) is independently proven correct --
# every test above that asserts a *rejection* implicitly trusts that a
# correctly-granted, live kb_id CAN bind; this makes that assumption explicit.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bind_single_whitelisted_live_kb_succeeds_and_is_read_back(
    client: httpx.AsyncClient,
    two_users: tuple[str, str],
    portal_sessionmaker,
    fake_core_lifecycle: FakeCoreLifecycle,
):
    user_a, _user_b = two_users
    kb_id = str(uuid.uuid4())
    fake_core_lifecycle.kb_manager.add_kb(FakeCoreKb(kb_id=kb_id, kb_name="Good KB"))
    await _grant_kb(portal_sessionmaker, kb_id=kb_id, kb_name="Good KB", enabled=True)

    cookies, headers = _auth(user_a)
    session_id = await _create_session(client, cookies, headers)

    response = await client.post(
        f"/api/v1/portal/sessions/{session_id}/knowledge-base",
        json={"kb_ids": [kb_id], "top_k": 7},
        cookies=cookies,
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"kb_ids": [kb_id], "top_k": 7}
    assert await _get_kb_config(client, session_id, cookies) == {
        "kb_ids": [kb_id],
        "top_k": 7,
    }
