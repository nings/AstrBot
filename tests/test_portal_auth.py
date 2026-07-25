"""Guardrail tests for the portal auth system (Phase 1 exit criteria).

Covers:

* Access-JWT mint/verify roundtrip + rejection of expired/wrong-secret/
  wrong-``typ`` tokens (``astrbot.portal.auth.security``).
* Six wrong-password login attempts trigger the rate limiter (429).
* Refresh-token rotation + replay detection revokes the whole token family,
  so a previously-issued access/refresh pair stops working.
* CSRF double-submit enforcement on an authenticated write route.
* A portal JWT must not authenticate against the core dashboard's admin
  routes, and a dashboard JWT must not authenticate as a portal user --
  the two token systems must not recognize each other.

Test-harness note on a formerly-live P0 bug (now fixed)
-------------------------------------------------------
``astrbot.portal.services.session_service._mint_access_token`` (the function
actually used by ``issue_token_pair``/``rotate_refresh``, i.e. every real
login/register/refresh) used to **not** set a ``"typ": "access"`` claim,
while ``astrbot.portal.auth.security.verify_access_token`` (used by
``auth.dependencies.get_current_user``, i.e. every protected route) hard
-rejects any token missing that claim. This meant every real login was
immediately unusable for any subsequent authenticated request. Fixed by
adding ``"typ": "access"`` to the claims dict in ``_mint_access_token``;
``test_login_then_me_succeeds`` below is the regression test for this
(no longer ``xfail``).

Most other tests in this file still mint their own access tokens directly
via ``security.mint_access_token`` (rather than going through a real
register/login HTTP call) to keep ownership/IDOR/CSRF/rate-limit guardrails
independent of the auth-flow's own wiring -- this is a deliberate scoping
choice, not a workaround for the bug above, and remains valid post-fix.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from astrbot.dashboard.api import app as dashboard_app
from astrbot.dashboard.api.app import create_dashboard_asgi_app
from astrbot.portal.api.auth_routes import router as auth_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalUser
from astrbot.portal.services import core_facade, session_service

# Import (read-only reuse) of an already-verified fixture pair from a
# sibling test file that builds the full, real ``create_dashboard_asgi_app``
# app -- see that module for the FakeDb/fake_core_lifecycle implementation.
# Not modified here; only imported so this file doesn't need to duplicate
# ~350 lines of fake-service plumbing that already exists and is already
# exercised elsewhere.
from tests.test_fastapi_v1_dashboard import (
    JWT_SECRET as DASHBOARD_JWT_SECRET,
)
from tests.test_fastapi_v1_dashboard import (
    fake_core_lifecycle,  # noqa: F401  (used as a pytest fixture by name)
    fake_db,  # noqa: F401  (used as a pytest fixture by name)
)

PORTAL_TEST_SECRET = "portal-test-secret-do-not-reuse-32bytes!"


# ---------------------------------------------------------------------------
# Shared test-database plumbing: an independent, temp-file-backed portal DB
# per test, distinct from the process-wide ``astrbot.portal.db.engine``
# singleton (which points at the real data directory).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-auth-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


async def _insert_user(
    sessionmaker: async_sessionmaker, *, user_id: str, email: str
) -> PortalUser:
    async with sessionmaker() as session:
        user = PortalUser(
            user_id=user_id,
            email=email,
            password_hash=None,
            display_name=email.split("@")[0],
            auth_method="local",
            role="user",
            status="active",
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
        return user


@pytest.fixture
def auth_app(portal_sessionmaker, monkeypatch) -> FastAPI:
    """Minimal app exposing only ``/api/v1/portal/auth/*`` -- no ChatService
    needed for any test in this file."""
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1/portal")
    app.state.portal_jwt_secret = PORTAL_TEST_SECRET

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_portal_db_session] = _override_db_session

    # session_service.issue_token_pair calls core_facade.get_jwt_secret(),
    # which (in production) touches the real, process-wide SharedPreferences
    # instance. Patch it to return a fixed test secret so these tests never
    # read/write real on-disk preference state.
    async def _fake_get_jwt_secret() -> str:
        return PORTAL_TEST_SECRET

    monkeypatch.setattr(core_facade, "get_jwt_secret", _fake_get_jwt_secret)

    return app


@pytest_asyncio.fixture
async def auth_client(auth_app: FastAPI):
    transport = httpx.ASGITransport(app=auth_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.test"


# ---------------------------------------------------------------------------
# 1. Access-JWT mint/verify contract (astrbot.portal.auth.security).
# ---------------------------------------------------------------------------


def test_mint_and_verify_access_token_roundtrip():
    token = security.mint_access_token(
        user_id="user-a", session_id="sess-a", secret=PORTAL_TEST_SECRET
    )
    payload = security.verify_access_token(token, PORTAL_TEST_SECRET)
    assert payload.user_id == "user-a"
    assert payload.session_id == "sess-a"


def test_verify_access_token_rejects_expired_token():
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    token = security.mint_access_token(
        user_id="user-a",
        session_id="sess-a",
        secret=PORTAL_TEST_SECRET,
        ttl_seconds=60,
        now=now,
    )
    with pytest.raises(security.TokenExpired):
        security.verify_access_token(token, PORTAL_TEST_SECRET)


def test_verify_access_token_rejects_wrong_secret():
    token = security.mint_access_token(
        user_id="user-a", session_id="sess-a", secret=PORTAL_TEST_SECRET
    )
    with pytest.raises(security.TokenInvalid):
        security.verify_access_token(token, "a-completely-different-secret")


def test_verify_access_token_rejects_missing_typ_claim():
    """This is precisely the shape of token
    ``session_service._mint_access_token`` currently produces -- see the
    module docstring and ``test_login_then_me_is_broken_by_missing_typ_claim``
    below."""
    bad_token = jwt.encode(
        {
            "sub": "user-a",
            "sid": "sess-a",
            "iat": int(datetime.now(timezone.utc).timestamp()),
            "exp": int(
                (datetime.now(timezone.utc) + timedelta(minutes=15)).timestamp()
            ),
            # no "typ" claim at all
        },
        PORTAL_TEST_SECRET,
        algorithm="HS256",
    )
    with pytest.raises(security.TokenInvalid):
        security.verify_access_token(bad_token, PORTAL_TEST_SECRET)


def test_constant_time_compare_rejects_none_and_mismatch():
    assert security.constant_time_compare("abc", "abc") is True
    assert security.constant_time_compare("abc", "abd") is False
    assert security.constant_time_compare(None, "abc") is False
    assert security.constant_time_compare("abc", None) is False


# ---------------------------------------------------------------------------
# 2. Login throttling: six wrong-password attempts trigger the rate limiter.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_six_wrong_password_attempts_trigger_rate_limit(
    auth_client: httpx.AsyncClient, portal_sessionmaker
):
    email = _unique_email()
    ip = f"203.0.113.{uuid.uuid4().int % 250 + 1}"
    await _insert_user(portal_sessionmaker, user_id=str(uuid.uuid4()), email=email)

    statuses = []
    for _ in range(6):
        response = await auth_client.post(
            "/api/v1/portal/auth/login",
            json={"email": email, "password": "definitely-wrong"},
            headers={"X-Forwarded-For": ip},
        )
        statuses.append(response.status_code)

    assert statuses[:5] == [401] * 5, statuses
    assert statuses[5] == 429, statuses


# ---------------------------------------------------------------------------
# 3. Refresh-token rotation + replay detection.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_replay_revokes_whole_family(
    auth_client: httpx.AsyncClient, portal_sessionmaker
):
    settings = get_portal_settings()
    email = _unique_email()
    ip = f"198.51.100.{uuid.uuid4().int % 250 + 1}"

    register_response = await auth_client.post(
        "/api/v1/portal/auth/register",
        json={"email": email, "password": "Sup3rSecret!42"},
        headers={"X-Forwarded-For": ip},
    )
    assert register_response.status_code == 200, register_response.text
    refresh_token_1 = register_response.cookies.get(settings.refresh_cookie_name)
    assert refresh_token_1

    # First refresh: valid, rotates to a brand new refresh token.
    refresh_response_1 = await auth_client.post(
        "/api/v1/portal/auth/refresh",
        cookies={settings.refresh_cookie_name: refresh_token_1},
        headers={"X-Forwarded-For": ip},
    )
    assert refresh_response_1.status_code == 200, refresh_response_1.text
    refresh_token_2 = refresh_response_1.cookies.get(settings.refresh_cookie_name)
    assert refresh_token_2
    assert refresh_token_2 != refresh_token_1

    # Replay the ALREADY-CONSUMED first refresh token.
    replay_response = await auth_client.post(
        "/api/v1/portal/auth/refresh",
        cookies={settings.refresh_cookie_name: refresh_token_1},
        headers={"X-Forwarded-For": ip},
    )
    assert replay_response.status_code == 401

    # The previously-issued (second, still "current") refresh token must
    # ALSO stop working now -- replay revokes the whole family, not just
    # the specific replayed token.
    followup_response = await auth_client.post(
        "/api/v1/portal/auth/refresh",
        cookies={settings.refresh_cookie_name: refresh_token_2},
        headers={"X-Forwarded-For": ip},
    )
    assert followup_response.status_code == 401


@pytest.mark.asyncio
async def test_rotate_refresh_service_level_family_revocation(portal_sessionmaker, monkeypatch):
    """Same guarantee as above, exercised directly at the service layer
    (no HTTP), so this guardrail does not depend on the auth-route wiring
    at all."""

    async def _fake_get_jwt_secret() -> str:
        return PORTAL_TEST_SECRET

    monkeypatch.setattr(core_facade, "get_jwt_secret", _fake_get_jwt_secret)

    user_id = str(uuid.uuid4())
    await _insert_user(portal_sessionmaker, user_id=user_id, email=_unique_email())

    async with portal_sessionmaker() as session:
        pair1 = await session_service.issue_token_pair(session, user_id=user_id)

    async with portal_sessionmaker() as session:
        pair2 = await session_service.rotate_refresh(
            session, refresh_token=pair1.refresh_token
        )
    assert pair2.family_id == pair1.family_id

    async with portal_sessionmaker() as session:
        with pytest.raises(session_service.RefreshReplayDetectedError):
            await session_service.rotate_refresh(
                session, refresh_token=pair1.refresh_token
            )

    async with portal_sessionmaker() as session:
        with pytest.raises(session_service.InvalidRefreshTokenError):
            await session_service.rotate_refresh(
                session, refresh_token=pair2.refresh_token
            )


# ---------------------------------------------------------------------------
# 4. CSRF double-submit enforcement.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_csrf_missing_or_mismatched_header_is_rejected(
    auth_client: httpx.AsyncClient, portal_sessionmaker
):
    settings = get_portal_settings()
    user_id = str(uuid.uuid4())
    await _insert_user(portal_sessionmaker, user_id=user_id, email=_unique_email())

    access_token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf_token = security.generate_csrf_token()
    cookies = {
        settings.jwt_cookie_name: access_token,
        settings.csrf_cookie_name: csrf_token,
    }

    missing_header_response = await auth_client.post(
        "/api/v1/portal/auth/logout", cookies=cookies
    )
    assert missing_header_response.status_code == 403

    mismatched_response = await auth_client.post(
        "/api/v1/portal/auth/logout",
        cookies=cookies,
        headers={settings.csrf_header_name: "not-the-real-token"},
    )
    assert mismatched_response.status_code == 403

    matching_response = await auth_client.post(
        "/api/v1/portal/auth/logout",
        cookies=cookies,
        headers={settings.csrf_header_name: csrf_token},
    )
    assert matching_response.status_code == 200


# ---------------------------------------------------------------------------
# 5. Regression test for a formerly-live P0 bug: session_service's real mint
#    path used to omit the "typ" claim, making a real login immediately
#    unusable against any protected route. Fixed in
#    session_service._mint_access_token by adding `"typ": "access"` to the
#    claims dict -- this test now asserts the fix stays in place.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_login_then_me_succeeds(
    auth_client: httpx.AsyncClient, portal_sessionmaker
):
    email = _unique_email()
    register_response = await auth_client.post(
        "/api/v1/portal/auth/register",
        json={"email": email, "password": "Sup3rSecret!42"},
    )
    assert register_response.status_code == 200, register_response.text
    settings = get_portal_settings()
    access_token = register_response.cookies.get(settings.jwt_cookie_name)
    assert access_token

    me_response = await auth_client.get(
        "/api/v1/portal/auth/me",
        cookies={settings.jwt_cookie_name: access_token},
    )
    assert me_response.status_code == 200, me_response.text


# ---------------------------------------------------------------------------
# 6. The two token systems must not recognize each other.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_portal_jwt_is_rejected_by_admin_dashboard_routes(
    fake_core_lifecycle,  # noqa: F811
    fake_db,  # noqa: F811
):
    app = create_dashboard_asgi_app(
        core_lifecycle=fake_core_lifecycle,
        db=fake_db,
        jwt_secret=DASHBOARD_JWT_SECRET,
    )
    app.state.portal_jwt_secret = PORTAL_TEST_SECRET

    portal_token = security.mint_access_token(
        user_id=str(uuid.uuid4()), session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        for path in (
            "/api/v1/conversations",
            "/api/v1/sessions",
            "/api/v1/system-config",
        ):
            response = await client.get(
                path, headers={"Authorization": f"Bearer {portal_token}"}
            )
            assert response.status_code == 401, (path, response.text)


@pytest.mark.asyncio
async def test_dashboard_jwt_does_not_authenticate_as_portal_user(
    fake_core_lifecycle,  # noqa: F811
    fake_db,  # noqa: F811
):
    app = create_dashboard_asgi_app(
        core_lifecycle=fake_core_lifecycle,
        db=fake_db,
        jwt_secret=DASHBOARD_JWT_SECRET,
    )
    app.state.portal_jwt_secret = PORTAL_TEST_SECRET

    dashboard_token = jwt.encode(
        {"username": "dashboard-admin"}, DASHBOARD_JWT_SECRET, algorithm="HS256"
    )

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/api/v1/portal/auth/me",
            cookies={"astrbot_portal_jwt": dashboard_token},
        )
        assert response.status_code == 401, response.text


# ---------------------------------------------------------------------------
# 7. Regression test for a formerly-live P0 bug: nothing in the codebase ever
#    called ``init_portal_db()`` or stashed a value onto
#    ``app.state.portal_jwt_secret`` for the *real* app assembled by
#    ``create_dashboard_asgi_app`` -- every other test in this file (and in
#    ``test_portal_chat_isolation.py``) worked around the gap by setting
#    ``app.state.portal_jwt_secret`` manually, which masked the fact that the
#    production wiring never did this itself. Fixed by adding a FastAPI
#    startup hook in ``astrbot.dashboard.api.app.create_dashboard_asgi_app``
#    that awaits ``portal_db_engine.init_portal_db()`` and stores
#    ``await portal_core_facade.get_jwt_secret()`` on ``app.state``.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dashboard_app_startup_wires_portal_jwt_secret_and_db(
    fake_core_lifecycle,  # noqa: F811
    fake_db,  # noqa: F811
    monkeypatch,
):
    """Drive ``astrbot.dashboard.api.app.init_portal_module_state`` directly
    (the function ``create_dashboard_asgi_app`` registers as a FastAPI
    startup handler -- calling it straight avoids needing to drive the ASGI
    lifespan protocol, which httpx's ``ASGITransport`` does not do on its
    own) and assert both effects it must produce actually happen: the portal
    schema gets initialized and ``app.state.portal_jwt_secret`` gets
    populated from ``core_facade.get_jwt_secret()``.
    """
    init_calls: list[bool] = []

    async def _fake_init_portal_db() -> None:
        init_calls.append(True)

    async def _fake_get_jwt_secret() -> str:
        return PORTAL_TEST_SECRET

    monkeypatch.setattr("astrbot.portal.db.engine.init_portal_db", _fake_init_portal_db)
    monkeypatch.setattr(core_facade, "get_jwt_secret", _fake_get_jwt_secret)

    app = create_dashboard_asgi_app(
        core_lifecycle=fake_core_lifecycle,
        db=fake_db,
        jwt_secret=DASHBOARD_JWT_SECRET,
    )
    assert getattr(app.state, "portal_jwt_secret", None) is None

    await dashboard_app.init_portal_module_state(app)

    assert init_calls == [True]
    assert app.state.portal_jwt_secret == PORTAL_TEST_SECRET
