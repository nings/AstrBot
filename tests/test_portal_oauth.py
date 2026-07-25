"""Adversarial test suite for the portal SSO/OAuth surface (Phase 5 tests-sso).

This suite is the evidence for whether the self-built OIDC client
(``astrbot.portal.services.sso.oidc``) and the account-linking policy layer
(``astrbot.portal.services.sso_service``) are sound. It is written to be
adversarial rather than happy-path, per the assignment: forged/missing/
replayed state, algorithm-confusion/``alg:none``/wrong-key id_tokens,
account-takeover-by-email attempts, and unlink-yourself-out lockouts are all
first-class citizens here, not afterthoughts.

Structure
---------
1. Shared fixtures: a temp-file portal DB, a minimal FastAPI app exposing
   only ``/api/v1/portal/auth/*`` (mirrors ``tests/test_portal_auth.py``'s
   own harness shape, including its ``core_facade.get_jwt_secret``
   monkeypatch gotcha), and a fully-offline ``_FakeOidcProvider`` registered
   in place of the real network-talking ``OidcProvider`` so the
   state/linking/takeover/rate-limit policy layer (``sso_service.py``) can be
   exercised over real HTTP without any network access.
2. Acceptance criterion 3 -- forged/missing/unknown/expired/already-consumed
   ``state`` is rejected, case by case, with no session ever issued.
3. Acceptance criterion 4 -- linking to a logged-in account requires an
   explicit second confirmation; forged/replayed/wrong-account confirmations
   are rejected and never write a binding.
4. ``OidcProvider`` id_token verification, directly, against locally
   generated RSA keys and a stubbed JWKS client -- no network, ever inside
   this section.
5. Account-takeover-by-email and subject-stability (the ``sub``, not the
   email, is what a binding is keyed on).
6. Unlink cannot leave an account with zero sign-in methods.
7. The providers-availability endpoint never echoes a secret.
8. Rate limiting applies to the callback.
9. No open redirect: an unallowlisted ``next`` is never honored.
10. WeCom/Feishu adapters, exercised via stubbed SDK/HTTP responses.

Everything in this file runs fully offline: no real HTTP request ever
leaves the process (OIDC's discovery/token-exchange/JWKS calls are stubbed
at the adapter-instance level; WeCom's ``httpx.AsyncClient.get`` is
monkeypatched; Feishu's ``lark_oapi`` client methods are monkeypatched).
"""

from __future__ import annotations

import dataclasses
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import pytest_asyncio
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import FastAPI
from lark_oapi.api.authen.v1 import (
    CreateOidcAccessTokenResponse,
    CreateOidcAccessTokenResponseBody,
    GetUserInfoResponse,
    GetUserInfoResponseBody,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col, select

from astrbot.portal.api import auth_routes as auth_routes_module
from astrbot.portal.api.auth_routes import _validate_redirect_target
from astrbot.portal.api.auth_routes import router as auth_router
from astrbot.portal.auth import dependencies as portal_dependencies
from astrbot.portal.auth import security
from astrbot.portal.config import PORTAL_JWT_COOKIE_NAME, get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import PortalBaseModel, PortalOAuthIdentity, PortalUser
from astrbot.portal.services import core_facade, sso_service
from astrbot.portal.services.sso import base as sso_base
from astrbot.portal.services.sso import wecom as wecom_module
from astrbot.portal.services.sso.base import (
    AuthorizationRequest,
    SsoTrustBasis,
    VerifiedIdentity,
)
from astrbot.portal.services.sso.feishu import FeishuProvider
from astrbot.portal.services.sso.oidc import OidcProvider
from astrbot.portal.services.sso.wecom import WeComProvider

PORTAL_TEST_SECRET = "portal-test-secret-do-not-reuse-32bytes!"
SSO_STATE_COOKIE = sso_service.SSO_STATE_COOKIE_NAME
SSO_NEXT_COOKIE = "astrbot_portal_sso_next"

OIDC_ISSUER = "https://idp.example.test"
OIDC_CLIENT_ID = "portal-test-client-id"


# ---------------------------------------------------------------------------
# Shared DB / user helpers (mirrors tests/test_portal_auth.py's own shape).
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-oauth-test.db",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as conn:
        await conn.run_sync(PortalBaseModel.metadata.create_all)
    sessionmaker = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield sessionmaker
    finally:
        await engine.dispose()


def _unique_email() -> str:
    return f"user-{uuid.uuid4().hex}@example.test"


def _unique_ip() -> str:
    """A per-call unique rate-limit bucket key.

    ``LoginThrottle`` is a process-wide singleton shared across every test
    in this file (and every other test file in the same pytest process --
    see ``astrbot.portal.auth.rate_limit.get_login_throttle``'s own
    docstring), so a real-looking-but-narrow IPv4 range (e.g. a single
    ``/24``) is not safe here: with dozens of tests each drawing one or more
    "IPs" from only ~250 possible values, a birthday-paradox collision
    between two unrelated tests' buckets is likely, not just theoretically
    possible, and would make an otherwise-correct test flake. The throttle
    only ever treats this value as an opaque string bucket key (see
    ``_client_ip``/``LoginThrottle.check`` -- it is never parsed as a real
    IP address), so using a high-entropy token instead of a plausible IPv4
    address is both safe and collision-proof in practice."""
    return f"203.0.113.{uuid.uuid4().hex}"


async def _insert_user(
    sessionmaker: async_sessionmaker,
    *,
    email: str | None = None,
    password_hash: str | None = None,
    status: str = "active",
    auth_method: str = "local",
) -> PortalUser:
    user = PortalUser(
        user_id=str(uuid.uuid4()),
        email=email or _unique_email(),
        password_hash=password_hash,
        display_name="Test User",
        auth_method=auth_method,
        role="user",
        status=status,
    )
    async with sessionmaker() as session:
        session.add(user)
        await session.commit()
        await session.refresh(user)
    return user


def _auth_cookies_for(user: PortalUser, settings) -> tuple[dict, str]:
    """Mint a real access-JWT + CSRF token for ``user``, matching exactly
    the shape ``test_portal_auth.py`` uses to authenticate as a given user
    without going through a real login HTTP call."""
    access_token = security.mint_access_token(
        user_id=user.user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf_token = security.generate_csrf_token()
    cookies = {
        settings.jwt_cookie_name: access_token,
        settings.csrf_cookie_name: csrf_token,
    }
    return cookies, csrf_token


async def _identity_rows_for_subject(
    sessionmaker: async_sessionmaker, *, provider: str, subject: str
) -> list[PortalOAuthIdentity]:
    async with sessionmaker() as session:
        result = await session.execute(
            select(PortalOAuthIdentity).where(
                col(PortalOAuthIdentity.provider) == provider,
                col(PortalOAuthIdentity.provider_subject) == subject,
            )
        )
        return list(result.scalars().all())


# ---------------------------------------------------------------------------
# A fully offline, controllable stand-in for OidcProvider, registered in
# place of the real network-talking adapter so the *policy* layer
# (sso_service.py: state/CSRF binding, login-vs-link resolution, takeover
# guards, rate limiting) can be exercised over real HTTP with zero network
# access. The real OidcProvider's own id_token verification is tested
# directly and separately, in section 4 below, against locally generated
# RSA keys.
# ---------------------------------------------------------------------------


class _FakeOidcProvider:
    provider_id = "oidc"
    trust_basis = SsoTrustBasis.SIGNED_ASSERTION

    def __init__(self) -> None:
        self.next_identity: VerifiedIdentity | BaseException | None = None

    async def build_authorization_request(
        self, *, redirect_uri: str
    ) -> AuthorizationRequest:
        return AuthorizationRequest(
            authorize_url=f"https://idp.fake.test/authorize?redirect_uri={redirect_uri}",
            state=secrets.token_urlsafe(16),
            provider_state={
                "nonce": secrets.token_urlsafe(8),
                "code_verifier": secrets.token_urlsafe(8),
            },
        )

    async def resolve_identity(
        self, *, code: str, redirect_uri: str, provider_state
    ) -> VerifiedIdentity:
        if self.next_identity is None:
            raise AssertionError(
                "test forgot to set fake_oidc_provider.next_identity before "
                "triggering a callback"
            )
        if isinstance(self.next_identity, BaseException):
            raise self.next_identity
        return self.next_identity


def _test_sso_settings():
    """A ``PortalSettings`` with SSO fully enabled/configured for all three
    providers, built via ``dataclasses.replace`` off the real defaults so
    every field this file doesn't care about (cookie names, CSRF header
    name, rate-limit capacity, ...) stays byte-for-byte identical to what
    ``auth.dependencies``/``auth_routes`` use when NOT patched -- only the
    SSO-specific fields below are overridden."""
    base = get_portal_settings()
    return dataclasses.replace(
        base,
        sso_enabled=True,
        sso_enabled_providers=frozenset({"oidc", "wecom", "feishu"}),
        sso_redirect_base_url="https://portal.test.local/portal",
        oidc_client_id=OIDC_CLIENT_ID,
        oidc_client_secret="portal-test-oidc-client-secret-value",
        oidc_issuer=OIDC_ISSUER,
        wecom_corp_id="corp-test-id",
        wecom_agent_id="1000002",
        wecom_app_secret="wecom-test-app-secret-value",
        feishu_app_id="cli_test_app_id",
        feishu_app_secret="feishu-test-app-secret-value",
    )


@pytest.fixture
def fake_oidc_provider():
    return _FakeOidcProvider()


@pytest.fixture
def sso_app(portal_sessionmaker, monkeypatch, fake_oidc_provider):
    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1/portal")
    app.state.portal_jwt_secret = PORTAL_TEST_SECRET

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    app.dependency_overrides[get_portal_db_session] = _override_db_session

    async def _fake_get_jwt_secret() -> str:
        return PORTAL_TEST_SECRET

    monkeypatch.setattr(core_facade, "get_jwt_secret", _fake_get_jwt_secret)

    settings = _test_sso_settings()
    # Patch every module's OWN bound reference to get_portal_settings --
    # each of these files did `from astrbot.portal.config import
    # get_portal_settings`, so the name is bound at import time in each
    # module's namespace and patching astrbot.portal.config.get_portal_settings
    # itself would not reach any of them.
    monkeypatch.setattr(sso_service, "get_portal_settings", lambda: settings)
    monkeypatch.setattr(auth_routes_module, "get_portal_settings", lambda: settings)
    monkeypatch.setattr(portal_dependencies, "get_portal_settings", lambda: settings)
    # Register the fully-offline fake in place of the real network-talking
    # OidcProvider -- sso_service._get_provider("oidc", ...) calls this.
    monkeypatch.setattr(sso_service, "_get_oidc_provider", lambda s: fake_oidc_provider)

    return app


@pytest_asyncio.fixture
async def sso_client(sso_app: FastAPI):
    transport = httpx.ASGITransport(app=sso_app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        yield client


# ===========================================================================
# 2. Acceptance criterion 3 -- state rejection, case by case.
# ===========================================================================


@pytest.mark.asyncio
async def test_callback_rejects_missing_state(sso_client: httpx.AsyncClient):
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code"},
        headers={"X-Forwarded-For": _unique_ip()},
    )
    assert response.status_code == 400, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_callback_rejects_forged_state_with_no_cookie_at_all(
    sso_client: httpx.AsyncClient,
):
    """An attacker who guesses/observes a state value in transit but never
    obtained the matching httpOnly browser-binding cookie."""
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": "attacker-guessed-state-value"},
        headers={"X-Forwarded-For": _unique_ip()},
    )
    assert response.status_code == 400, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_callback_rejects_forged_state_with_mismatched_cookie(
    sso_client: httpx.AsyncClient,
):
    """The OAuth-login-CSRF shape: attacker starts their own flow and tricks
    the victim's browser into visiting the resulting (attacker's own valid)
    callback URL -- the victim's browser carries a DIFFERENT state cookie
    (its own, unrelated pending flow, or none), so the two must not match."""
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": "attacker-state-value"},
        cookies={SSO_STATE_COOKIE: "victims-own-different-cookie-value"},
        headers={"X-Forwarded-For": _unique_ip()},
    )
    assert response.status_code == 400, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_callback_rejects_unknown_state_never_issued(
    sso_client: httpx.AsyncClient,
):
    """Cookie and query state match each other (so the browser-binding check
    alone would pass) but this exact value was never produced by a real
    ``/start`` call -- it must still be rejected identically."""
    same_value = "looks-plausible-but-was-never-issued-by-start"
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": same_value},
        cookies={SSO_STATE_COOKIE: same_value},
        headers={"X-Forwarded-For": _unique_ip()},
    )
    assert response.status_code == 400, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_callback_rejects_expired_state(sso_client: httpx.AsyncClient):
    state_value = secrets.token_urlsafe(16)
    flow = sso_service._PendingFlow(
        provider="oidc",
        purpose=sso_service._FlowPurpose.LOGIN,
        nonce="n",
        code_verifier="v",
        redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
        link_user_id=None,
        expires_at=time.monotonic() - 1.0,
    )
    # expires_at already in the past -- _ExpiringSingleUseStore.take() must
    # treat this identically to "never existed".
    await sso_service._pending_flows.put(state_value, flow, expires_at=time.monotonic() - 1.0)

    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": state_value},
        cookies={SSO_STATE_COOKIE: state_value},
        headers={"X-Forwarded-For": _unique_ip()},
    )
    assert response.status_code == 400, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


@pytest.mark.asyncio
async def test_callback_rejects_replayed_state_after_a_failed_attempt(
    sso_client: httpx.AsyncClient, fake_oidc_provider: _FakeOidcProvider
):
    """State is single-use regardless of what happens after it is consumed
    -- even a failed verification attempt burns it, so it can never be
    replayed with a corrected payload."""
    ip = _unique_ip()
    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = sso_base.SsoIdentityVerificationError(
        "forged upstream response"
    )

    first = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert first.status_code == 400, first.text
    assert PORTAL_JWT_COOKIE_NAME not in first.cookies

    replay = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "some-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert replay.status_code == 400, replay.text
    assert PORTAL_JWT_COOKIE_NAME not in replay.cookies


@pytest.mark.asyncio
async def test_callback_replay_after_a_successful_login_grants_no_second_session(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    """The strongest form of the replay guarantee: a state that already
    completed one successful login must not be usable to mint a second
    session, even presenting the exact same code/state/cookie again."""
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker)
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(
                user_id=user.user_id, provider="oidc", provider_subject="replay-test-sub"
            )
        )
        await session.commit()

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="replay-test-sub",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=user.email,
        email_verified=True,
    )

    first = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "real-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert first.status_code == 200, first.text
    assert first.json()["result"] == "login"
    assert PORTAL_JWT_COOKIE_NAME in first.cookies

    replay = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "real-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert replay.status_code == 400, replay.text
    assert PORTAL_JWT_COOKIE_NAME not in replay.cookies


# ===========================================================================
# 3. Acceptance criterion 4 -- account linking requires an explicit second
#    confirmation; unconfirmed/forged/replayed confirmations grant nothing.
# ===========================================================================


@pytest.mark.asyncio
async def test_link_is_not_effective_until_explicit_confirmation(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    settings = get_portal_settings()
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker, password_hash="irrelevant-hash-value")
    cookies, csrf = _auth_cookies_for(user, settings)

    link_start = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link",
        cookies=cookies,
        headers={settings.csrf_header_name: csrf, "X-Forwarded-For": ip},
    )
    assert link_start.status_code == 200, link_start.text
    state_cookie_value = link_start.cookies.get(SSO_STATE_COOKIE)
    assert state_cookie_value

    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="link-subject-001",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email="idp-reported@example.test",
        email_verified=True,
        display_name="IdP Name",
    )
    callback = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "link-code", "state": state_cookie_value},
        cookies={**cookies, SSO_STATE_COOKIE: state_cookie_value},
        headers={"X-Forwarded-For": ip},
    )
    assert callback.status_code == 200, callback.text
    body = callback.json()
    assert body["result"] == "link_pending"
    confirmation_token = body["confirmation_token"]

    # Unconfirmed: no PortalOAuthIdentity row exists yet, and grants nothing.
    assert await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="link-subject-001"
    ) == []

    # A forged confirmation token (never issued by any callback) is rejected.
    forged = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link/confirm",
        json={"confirmation_token": secrets.token_urlsafe(32)},
        cookies=cookies,
        headers={settings.csrf_header_name: csrf},
    )
    assert forged.status_code == 400, forged.text

    # A DIFFERENT, currently-logged-in user presenting the real token is
    # rejected -- and burns the token (single-use, regardless of outcome).
    other_user = await _insert_user(portal_sessionmaker, password_hash="irrelevant")
    other_cookies, other_csrf = _auth_cookies_for(other_user, settings)
    wrong_user_confirm = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link/confirm",
        json={"confirmation_token": confirmation_token},
        cookies=other_cookies,
        headers={settings.csrf_header_name: other_csrf},
    )
    assert wrong_user_confirm.status_code == 400, wrong_user_confirm.text
    assert await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="link-subject-001"
    ) == []

    # Even the RIGHT user can no longer confirm: the token was already burned
    # by the failed attempt above (pop-on-read, not pop-on-success).
    right_user_confirm_after_burn = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link/confirm",
        json={"confirmation_token": confirmation_token},
        cookies=cookies,
        headers={settings.csrf_header_name: csrf},
    )
    assert right_user_confirm_after_burn.status_code == 400, right_user_confirm_after_burn.text
    assert await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="link-subject-001"
    ) == []


@pytest.mark.asyncio
async def test_link_confirm_success_then_replay_is_rejected_and_creates_no_duplicate(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    settings = get_portal_settings()
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker, password_hash="irrelevant")
    cookies, csrf = _auth_cookies_for(user, settings)

    link_start = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link",
        cookies=cookies,
        headers={settings.csrf_header_name: csrf, "X-Forwarded-For": ip},
    )
    state_cookie_value = link_start.cookies.get(SSO_STATE_COOKIE)

    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="link-subject-002",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email="whoever@example.test",
        email_verified=True,
    )
    callback = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": state_cookie_value},
        cookies={**cookies, SSO_STATE_COOKIE: state_cookie_value},
        headers={"X-Forwarded-For": ip},
    )
    confirmation_token = callback.json()["confirmation_token"]

    ok = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link/confirm",
        json={"confirmation_token": confirmation_token},
        cookies=cookies,
        headers={settings.csrf_header_name: csrf},
    )
    assert ok.status_code == 200, ok.text

    rows = await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="link-subject-002"
    )
    assert len(rows) == 1
    assert rows[0].user_id == user.user_id

    replay = await sso_client.post(
        "/api/v1/portal/auth/sso/oidc/link/confirm",
        json={"confirmation_token": confirmation_token},
        cookies=cookies,
        headers={settings.csrf_header_name: csrf},
    )
    assert replay.status_code == 400, replay.text

    rows_after_replay = await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="link-subject-002"
    )
    assert len(rows_after_replay) == 1  # no duplicate row from the replay


# ===========================================================================
# 4. OidcProvider id_token verification -- offline, against locally
#    generated RSA keys and a stubbed jwt.PyJWKClient. No network here.
# ===========================================================================


def _gen_rsa_keypair():
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return private_key, private_key.public_key()


def _rsa_public_pem(public_key) -> bytes:
    from cryptography.hazmat.primitives import serialization

    return public_key.public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _valid_claims(
    *,
    iss: str = OIDC_ISSUER,
    aud: str = OIDC_CLIENT_ID,
    sub: str = "user-stable-subject-001",
    nonce: str | None = "expected-nonce-value",
    now: datetime | None = None,
    exp_delta: timedelta = timedelta(minutes=5),
    iat_delta: timedelta = timedelta(seconds=0),
    email: str | None = None,
    email_verified: bool | None = None,
) -> dict:
    now = now or datetime.now(timezone.utc)
    claims: dict = {
        "iss": iss,
        "aud": aud,
        "sub": sub,
        "iat": int((now + iat_delta).timestamp()),
        "exp": int((now + exp_delta).timestamp()),
    }
    if nonce is not None:
        claims["nonce"] = nonce
    if email is not None:
        claims["email"] = email
    if email_verified is not None:
        claims["email_verified"] = email_verified
    return claims


class _FakeSigningKey:
    def __init__(self, key) -> None:
        self.key = key


def _b64url(data: bytes) -> bytes:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=")


def _hand_craft_hs256_token(claims: dict, *, key: bytes) -> str:
    """Build an HS256 JWS by hand (bypassing PyJWT's own encode-side guard
    against signing with a PEM-formatted key) -- this is exactly what an
    attacker attempting the classic RS256->HS256 algorithm-confusion attack
    would have to do, since they are not going through PyJWT's convenience
    wrapper to forge the victim's token in the first place."""
    import hashlib
    import hmac as hmac_module
    import json

    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = _b64url(json.dumps(header).encode()) + b"." + _b64url(
        json.dumps(claims).encode()
    )
    signature = hmac_module.new(key, signing_input, hashlib.sha256).digest()
    return (signing_input + b"." + _b64url(signature)).decode("ascii")


def _patch_jwks(monkeypatch, *, key=None, raise_unknown_kid: bool = False) -> None:
    """Replace ``jwt.PyJWKClient.get_signing_key_from_jwt`` -- the one call
    in ``oidc.py`` that would otherwise perform a real HTTP fetch of the
    provider's JWKS -- with a fully offline stub, so this whole section runs
    with no network access whatsoever."""

    def fake_get_signing_key_from_jwt(self, token: str):  # noqa: ANN001 - test stub
        if raise_unknown_kid:
            raise jwt.PyJWKClientError("no signing key found for this kid")
        return _FakeSigningKey(key)

    monkeypatch.setattr(
        jwt.PyJWKClient, "get_signing_key_from_jwt", fake_get_signing_key_from_jwt
    )


@pytest.fixture(scope="module")
def rsa_keys():
    return _gen_rsa_keypair()


@pytest.fixture
def oidc_provider():
    return OidcProvider(
        client_id=OIDC_CLIENT_ID,
        client_secret="does-not-matter-for-these-tests",
        issuer=OIDC_ISSUER,
    )


JWKS_URI = f"{OIDC_ISSUER}/jwks.json"


@pytest.mark.asyncio
async def test_id_token_wrong_signature_is_rejected(oidc_provider, rsa_keys, monkeypatch):
    _real_priv, real_pub = rsa_keys
    attacker_priv, _attacker_pub = _gen_rsa_keypair()
    token = jwt.encode(_valid_claims(), attacker_priv, algorithm="RS256")
    # JWKS legitimately reports the REAL public key -- but the token was
    # signed by a different (attacker-controlled) private key.
    _patch_jwks(monkeypatch, key=real_pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_signed_by_a_key_not_in_jwks_is_rejected(
    oidc_provider, rsa_keys, monkeypatch
):
    priv, _pub = rsa_keys
    token = jwt.encode(_valid_claims(), priv, algorithm="RS256")
    # Simulates: this token's `kid` does not match anything in the
    # provider's published JWKS at all (key resolution itself fails).
    _patch_jwks(monkeypatch, raise_unknown_kid=True)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_alg_none_is_rejected(oidc_provider, rsa_keys, monkeypatch):
    _priv, pub = rsa_keys
    token = jwt.encode(_valid_claims(), key=None, algorithm="none")
    # Even if key resolution "succeeds" (attacker-controlled kid confusion),
    # the hardcoded algorithms allowlist has no "none" in it.
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_algorithm_confusion_hmac_with_public_key_is_rejected(
    oidc_provider, rsa_keys, monkeypatch
):
    """Classic RS256->HS256 algorithm-confusion attack: sign with the
    provider's own PUBLIC key (which an attacker can always obtain) reused
    as an HMAC secret. The hardcoded, header-independent algorithms
    allowlist (asymmetric only) must reject this regardless of what
    signing-key resolution returns."""
    _priv, pub = rsa_keys
    pub_pem = _rsa_public_pem(pub)
    # PyJWT's own `encode()` refuses to sign HS256 with a PEM-formatted key
    # (a client-side guard against exactly this mistake) -- a real attacker
    # is not using PyJWT's convenience wrapper to *forge* the token, so we
    # hand-construct the HS256 JWS ourselves to faithfully simulate the
    # attack payload our *decode*-side defense must reject.
    token = _hand_craft_hs256_token(_valid_claims(), key=pub_pem)
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_wrong_issuer_is_rejected(oidc_provider, rsa_keys, monkeypatch):
    priv, pub = rsa_keys
    token = jwt.encode(
        _valid_claims(iss="https://attacker-controlled-idp.example"), priv, algorithm="RS256"
    )
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_wrong_audience_is_rejected(oidc_provider, rsa_keys, monkeypatch):
    priv, pub = rsa_keys
    token = jwt.encode(
        _valid_claims(aud="some-other-clients-id"), priv, algorithm="RS256"
    )
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_expired_is_rejected(oidc_provider, rsa_keys, monkeypatch):
    priv, pub = rsa_keys
    now = datetime.now(timezone.utc) - timedelta(hours=1)
    token = jwt.encode(
        _valid_claims(now=now, exp_delta=timedelta(minutes=5)), priv, algorithm="RS256"
    )
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_issued_too_far_in_the_future_is_rejected(
    oidc_provider, rsa_keys, monkeypatch
):
    priv, pub = rsa_keys
    # Well beyond the adapter's 120s clock-skew leeway.
    token = jwt.encode(
        _valid_claims(iat_delta=timedelta(seconds=1000), exp_delta=timedelta(minutes=90)),
        priv,
        algorithm="RS256",
    )
    _patch_jwks(monkeypatch, key=pub)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)


@pytest.mark.asyncio
async def test_id_token_verification_succeeds_for_a_genuinely_valid_token(
    oidc_provider, rsa_keys, monkeypatch
):
    """Positive control: proves the harness itself (RSA keys + JWKS stub) is
    wired correctly, so the rejections above are not accidentally passing
    because nothing would ever verify."""
    priv, pub = rsa_keys
    token = jwt.encode(_valid_claims(), priv, algorithm="RS256")
    _patch_jwks(monkeypatch, key=pub)

    claims = await oidc_provider._verify_id_token(token, jwks_uri=JWKS_URI)
    assert claims["sub"] == "user-stable-subject-001"


# -- nonce checks (resolve_identity-level, not _verify_id_token-level) ------


def _prime_provider_for_resolve(monkeypatch, provider: OidcProvider, id_token: str) -> None:
    """Skip the (network) discovery fetch and code-exchange step -- prime
    the provider's discovery cache directly and stub `_exchange_code` to
    hand back a fixed id_token, so `resolve_identity` can be exercised
    end-to-end with no network access."""
    provider._discovery_cache = {
        "authorization_endpoint": f"{OIDC_ISSUER}/authorize",
        "token_endpoint": f"{OIDC_ISSUER}/token",
        "jwks_uri": JWKS_URI,
        "issuer": OIDC_ISSUER,
    }
    provider._discovery_cache_at = time.monotonic()

    async def fake_exchange_code(**kwargs):
        return {"id_token": id_token}

    monkeypatch.setattr(provider, "_exchange_code", fake_exchange_code)


@pytest.mark.asyncio
async def test_resolve_identity_rejects_nonce_mismatch(oidc_provider, rsa_keys, monkeypatch):
    priv, pub = rsa_keys
    token = jwt.encode(
        _valid_claims(nonce="the-actual-token-nonce"), priv, algorithm="RS256"
    )
    _patch_jwks(monkeypatch, key=pub)
    _prime_provider_for_resolve(monkeypatch, oidc_provider, token)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider.resolve_identity(
            code="c",
            redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
            provider_state={
                "nonce": "a-completely-different-nonce-this-flow-issued",
                "code_verifier": "verifier",
            },
        )


@pytest.mark.asyncio
async def test_resolve_identity_rejects_missing_nonce_in_flow_state(
    oidc_provider, rsa_keys, monkeypatch
):
    """The flow's own stored state has no nonce at all -- refuse rather than
    skip the check."""
    priv, pub = rsa_keys
    token = jwt.encode(_valid_claims(nonce="whatever"), priv, algorithm="RS256")
    _patch_jwks(monkeypatch, key=pub)
    _prime_provider_for_resolve(monkeypatch, oidc_provider, token)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider.resolve_identity(
            code="c",
            redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
            provider_state={"nonce": "", "code_verifier": "verifier"},
        )


@pytest.mark.asyncio
async def test_resolve_identity_rejects_id_token_missing_nonce_claim(
    oidc_provider, rsa_keys, monkeypatch
):
    """The flow issued a real nonce, but the id_token itself carries none at
    all (e.g. a token from an unrelated flow, or a provider that dropped the
    claim) -- refuse rather than treat absence as a match."""
    priv, pub = rsa_keys
    token = jwt.encode(_valid_claims(nonce=None), priv, algorithm="RS256")
    _patch_jwks(monkeypatch, key=pub)
    _prime_provider_for_resolve(monkeypatch, oidc_provider, token)

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await oidc_provider.resolve_identity(
            code="c",
            redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
            provider_state={"nonce": "expected-nonce-value", "code_verifier": "verifier"},
        )


@pytest.mark.asyncio
async def test_resolve_identity_succeeds_with_matching_nonce(
    oidc_provider, rsa_keys, monkeypatch
):
    priv, pub = rsa_keys
    token = jwt.encode(
        _valid_claims(nonce="matching-nonce", email="user@example.test", email_verified=True),
        priv,
        algorithm="RS256",
    )
    _patch_jwks(monkeypatch, key=pub)
    _prime_provider_for_resolve(monkeypatch, oidc_provider, token)

    identity = await oidc_provider.resolve_identity(
        code="c",
        redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
        provider_state={"nonce": "matching-nonce", "code_verifier": "verifier"},
    )
    assert identity.subject == "user-stable-subject-001"
    assert identity.trust_basis is SsoTrustBasis.SIGNED_ASSERTION
    assert identity.email == "user@example.test"
    assert identity.email_verified is True


@pytest.mark.asyncio
async def test_resolve_identity_email_verified_defaults_false_when_claim_absent(
    oidc_provider, rsa_keys, monkeypatch
):
    """Absence of email_verified must never be treated as verified-by-omission."""
    priv, pub = rsa_keys
    token = jwt.encode(
        _valid_claims(nonce="n", email="user@example.test", email_verified=None),
        priv,
        algorithm="RS256",
    )
    _patch_jwks(monkeypatch, key=pub)
    _prime_provider_for_resolve(monkeypatch, oidc_provider, token)

    identity = await oidc_provider.resolve_identity(
        code="c",
        redirect_uri="https://portal.test.local/portal/oauth/callback/oidc",
        provider_state={"nonce": "n", "code_verifier": "verifier"},
    )
    assert identity.email_verified is False


# ===========================================================================
# 5. Account takeover: an SSO identity asserting an existing account's email
#    must not gain access to it. Subject stability across email changes.
# ===========================================================================


@pytest.mark.asyncio
async def test_unrecognized_identity_with_victims_verified_email_gains_no_access(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    """Rule 5's central scenario: the presenter's identity is not linked to
    any account, but they assert (even with a verified-email claim) an
    email address that belongs to an existing local user. This build has no
    auto-provisioning/auto-linking-by-email code path anywhere (see
    sso_service module docstring): the ONLY lookup a LOGIN-purpose callback
    performs is PortalOAuthIdentity by (provider, subject) -- so this must
    be refused, unconditionally."""
    ip = _unique_ip()
    victim = await _insert_user(
        portal_sessionmaker, email="victim@example.test", password_hash="hash-irrelevant"
    )

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="attacker-controlled-subject-xyz",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=victim.email,
        email_verified=True,  # even a fully-verified claim must not help
    )

    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert response.status_code == 403, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies
    assert await _identity_rows_for_subject(
        portal_sessionmaker, provider="oidc", subject="attacker-controlled-subject-xyz"
    ) == []


@pytest.mark.asyncio
async def test_login_resolves_by_subject_and_survives_an_email_change(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    """Item 5: the stored subject is the provider's stable id, not the
    email -- a returning user whose email changed (at the IdP, or locally)
    since they linked must still resolve to the same account."""
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker, email="old-address@example.test")
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(
                user_id=user.user_id, provider="oidc", provider_subject="stable-sub-xyz"
            )
        )
        await session.commit()
        db_user = await session.get(PortalUser, user.user_id)
        db_user.email = "new-address@example.test"
        session.add(db_user)
        await session.commit()

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="stable-sub-xyz",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email="yet-a-third-email-the-idp-happens-to-report@example.test",
        email_verified=True,
    )
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["user"]["user_id"] == user.user_id
    assert body["user"]["email"] == "new-address@example.test"


@pytest.mark.asyncio
async def test_different_subject_with_same_email_does_not_resolve_to_existing_account(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker, email="shared-address@example.test")
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(
                user_id=user.user_id, provider="oidc", provider_subject="sub-original"
            )
        )
        await session.commit()

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="sub-DIFFERENT-entirely",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email="shared-address@example.test",
        email_verified=True,
    )
    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
        headers={"X-Forwarded-For": ip},
    )
    assert response.status_code == 403, response.text
    assert PORTAL_JWT_COOKIE_NAME not in response.cookies


# ===========================================================================
# 6. Unlinking cannot leave a user with no way to sign in.
# ===========================================================================


@pytest.mark.asyncio
async def test_unlink_refuses_when_it_would_remove_the_last_credential(portal_sessionmaker):
    user = await _insert_user(portal_sessionmaker, password_hash=None)
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(user_id=user.user_id, provider="oidc", provider_subject="only-id")
        )
        await session.commit()

    async with portal_sessionmaker() as session:
        with pytest.raises(sso_service.SsoCannotRemoveLastCredentialError):
            await sso_service.unlink(session, user_id=user.user_id, provider_id="oidc")

    rows = await _identity_rows_for_subject(portal_sessionmaker, provider="oidc", subject="only-id")
    assert len(rows) == 1  # refused, not silently removed


@pytest.mark.asyncio
async def test_unlink_succeeds_when_a_password_remains(portal_sessionmaker):
    user = await _insert_user(portal_sessionmaker, password_hash="a-real-password-hash")
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(user_id=user.user_id, provider="oidc", provider_subject="only-id")
        )
        await session.commit()

    async with portal_sessionmaker() as session:
        await sso_service.unlink(session, user_id=user.user_id, provider_id="oidc")

    rows = await _identity_rows_for_subject(portal_sessionmaker, provider="oidc", subject="only-id")
    assert rows == []


@pytest.mark.asyncio
async def test_unlink_succeeds_when_another_linked_identity_remains(portal_sessionmaker):
    user = await _insert_user(portal_sessionmaker, password_hash=None)
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(user_id=user.user_id, provider="oidc", provider_subject="sub-a")
        )
        session.add(
            PortalOAuthIdentity(user_id=user.user_id, provider="wecom", provider_subject="sub-b")
        )
        await session.commit()

    async with portal_sessionmaker() as session:
        await sso_service.unlink(session, user_id=user.user_id, provider_id="oidc")

    async with portal_sessionmaker() as session:
        remaining = (
            await session.execute(
                select(PortalOAuthIdentity).where(
                    col(PortalOAuthIdentity.user_id) == user.user_id
                )
            )
        ).scalars().all()
    assert [row.provider for row in remaining] == ["wecom"]

    # And now THAT one is the last credential -- refuse too.
    async with portal_sessionmaker() as session:
        with pytest.raises(sso_service.SsoCannotRemoveLastCredentialError):
            await sso_service.unlink(session, user_id=user.user_id, provider_id="wecom")


# ===========================================================================
# 7. The providers endpoint exposes availability only -- no secrets.
# ===========================================================================


@pytest.mark.asyncio
async def test_providers_endpoint_exposes_only_booleans_never_secrets(
    sso_client: httpx.AsyncClient,
):
    response = await sso_client.get("/api/v1/portal/auth/sso/providers")
    assert response.status_code == 200, response.text
    body = response.json()
    providers = body["providers"]
    assert set(providers) == {"oidc", "wecom", "feishu"}
    for value in providers.values():
        assert isinstance(value, bool)

    secret_values = [
        "portal-test-oidc-client-secret-value",
        "wecom-test-app-secret-value",
        "feishu-test-app-secret-value",
    ]
    for secret in secret_values:
        assert secret not in response.text


# ===========================================================================
# 8. Rate limiting applies to the callback.
# ===========================================================================


@pytest.mark.asyncio
async def test_callback_is_rate_limited(sso_client: httpx.AsyncClient):
    ip = _unique_ip()
    statuses = []
    for i in range(6):
        response = await sso_client.get(
            "/api/v1/portal/auth/sso/oidc/callback",
            params={"code": "any-code", "state": f"never-issued-state-{i}"},
            headers={"X-Forwarded-For": ip},
        )
        statuses.append(response.status_code)
    assert statuses[:5] == [400] * 5, statuses
    assert statuses[5] == 429, statuses


# ===========================================================================
# 9. No open redirect: an unallowlisted `next` is never honored.
# ===========================================================================


def test_validate_redirect_target_rejects_everything_off_allowlist():
    assert _validate_redirect_target(None) is None
    assert _validate_redirect_target("") is None
    assert _validate_redirect_target("https://evil.example.com") is None
    assert _validate_redirect_target("http://evil.example.com/portal") is None
    assert _validate_redirect_target("//evil.example.com") is None
    assert _validate_redirect_target("/\\evil.example.com") is None
    assert _validate_redirect_target("/portal.evil.com") is None
    assert _validate_redirect_target("/portalx") is None
    assert _validate_redirect_target("/portal/\r\nSet-Cookie: pwned=1") is None
    assert _validate_redirect_target("/portal/\nLocation: https://evil.example.com") is None


def test_validate_redirect_target_accepts_the_allowlisted_prefix():
    assert _validate_redirect_target("/portal") == "/portal"
    assert _validate_redirect_target("/portal/settings") == "/portal/settings"


@pytest.mark.asyncio
async def test_start_then_callback_never_honors_an_unallowlisted_next(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker)
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(
                user_id=user.user_id, provider="oidc", provider_subject="redirect-test-sub"
            )
        )
        await session.commit()

    start = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/start",
        params={"next": "https://evil.example.com/steal-session"},
        headers={"X-Forwarded-For": ip},
    )
    assert start.status_code == 302, start.text
    state_value = start.cookies.get(SSO_STATE_COOKIE)
    assert state_value
    # The malicious `next` must never even make it into a cookie.
    assert SSO_NEXT_COOKIE not in start.cookies

    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="redirect-test-sub",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=user.email,
        email_verified=True,
    )
    callback = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": state_value},
        cookies={SSO_STATE_COOKIE: state_value},
        headers={"X-Forwarded-For": ip},
    )
    assert callback.status_code == 200, callback.text
    assert callback.json()["redirect_to"] == "/portal"


@pytest.mark.asyncio
async def test_start_then_callback_honors_an_allowlisted_next(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    ip = _unique_ip()
    user = await _insert_user(portal_sessionmaker)
    async with portal_sessionmaker() as session:
        session.add(
            PortalOAuthIdentity(
                user_id=user.user_id, provider="oidc", provider_subject="redirect-test-sub-2"
            )
        )
        await session.commit()

    start = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/start",
        params={"next": "/portal/settings"},
        headers={"X-Forwarded-For": ip},
    )
    state_value = start.cookies.get(SSO_STATE_COOKIE)
    next_value = (start.cookies.get(SSO_NEXT_COOKIE) or "").strip('"')
    assert next_value == "/portal/settings"

    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject="redirect-test-sub-2",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=user.email,
        email_verified=True,
    )
    callback = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "c", "state": state_value},
        cookies={SSO_STATE_COOKIE: state_value, SSO_NEXT_COOKIE: next_value},
        headers={"X-Forwarded-For": ip},
    )
    assert callback.status_code == 200, callback.text
    assert callback.json()["redirect_to"] == "/portal/settings"


@pytest.mark.asyncio
async def test_wecom_and_feishu_are_reachable_through_sso_service_dispatch(
    sso_client: httpx.AsyncClient,
):
    """Phase 5 verification lane note: this test originally pinned a real gap
    -- the WeCom/Feishu adapter classes existed (independently verified below
    in section 10) and satisfied the SsoProvider protocol, but
    ``sso_service._get_provider``'s dispatch had never been wired to
    construct them, so a fully-configured deployment's WeCom/Feishu login
    button would 404 the moment a user clicked it. That dispatch wiring has
    since been added (mirroring ``_get_oidc_provider``'s lazy-cached-instance
    pattern, gated behind ``get_sso_provider_availability()`` exactly as
    notes_for_integrator specified), so both providers now issue a real 302
    redirect to their authorize URL instead of failing with
    ``SsoProviderNotImplementedError``. This test was updated deliberately
    (not broken by accident) to assert the fixed behavior."""
    for provider in ("wecom", "feishu"):
        response = await sso_client.get(
            f"/api/v1/portal/auth/sso/{provider}/start",
            headers={"X-Forwarded-For": _unique_ip()},
            follow_redirects=False,
        )
        assert response.status_code == 302, (provider, response.text)
        assert response.headers["location"], (provider, "empty redirect Location")


# ===========================================================================
# 10. WeCom and Feishu adapters, exercised via stubbed HTTP/SDK responses.
#     No network. Both providers hand over no signed assertion at all --
#     each block below documents and tests what the code relies on instead.
# ===========================================================================



def _wecom_subject(corp_id: str, user_id: str) -> str:
    """Mirror of WeComProvider's subject encoding, for the ambiguity check."""
    return f"wecom:{len(corp_id)}:{corp_id}:{user_id}"


def _install_wecom_http_stub(
    monkeypatch,
    *,
    token_response: dict | None = None,
    getuserinfo_response: dict | None = None,
    getuserinfo_raw_content: bytes | None = None,
    userget_response: dict | None = None,
) -> None:
    async def fake_get(self, url, params=None, **kwargs):  # noqa: ANN001 - test stub
        if url == wecom_module._GETTOKEN_URL:
            payload = token_response or {
                "errcode": 0,
                "access_token": "corp-access-token-abc",
                "expires_in": 7200,
            }
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        if url == wecom_module._GETUSERINFO_URL:
            if getuserinfo_raw_content is not None:
                return httpx.Response(
                    200, content=getuserinfo_raw_content, request=httpx.Request("GET", url)
                )
            return httpx.Response(
                200, json=getuserinfo_response, request=httpx.Request("GET", url)
            )
        if url == wecom_module._USER_GET_URL:
            payload = userget_response if userget_response is not None else {"errcode": 0}
            return httpx.Response(200, json=payload, request=httpx.Request("GET", url))
        raise AssertionError(f"unexpected URL requested in test: {url}")

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)


@pytest.mark.asyncio
async def test_wecom_resolves_identity_for_a_real_corp_member(monkeypatch):
    _install_wecom_http_stub(
        monkeypatch,
        getuserinfo_response={"errcode": 0, "UserId": "zhangsan"},
        userget_response={"errcode": 0, "name": "Zhang San", "email": "zhangsan@corp.example"},
    )
    provider = WeComProvider(corp_id="corp-alpha", agent_id="1000002", app_secret="s3cr3t")

    identity = await provider.resolve_identity(
        code="wecom-code-abc",
        redirect_uri="https://portal.test.local/portal/oauth/callback/wecom",
        provider_state={},
    )
    assert identity.subject == "wecom:10:corp-alpha:zhangsan"
    assert identity.trust_basis is SsoTrustBasis.AUTHENTICATED_API_RESPONSE
    # Never SIGNED_ASSERTION-level trust even though the directory has an
    # email on file -- WeCom has no concept of the user proving ownership.
    assert identity.email_verified is False
    assert identity.email == "zhangsan@corp.example"


@pytest.mark.asyncio
async def test_wecom_rejects_non_member_openid_only_response(monkeypatch):
    """A presenter with no UserId (only an anonymous OpenId) is not a member
    of the configured corp -- fail closed rather than authenticate them at
    lower trust."""
    _install_wecom_http_stub(
        monkeypatch, getuserinfo_response={"errcode": 0, "OpenId": "o-anonymous-visitor"}
    )
    provider = WeComProvider(corp_id="corp-alpha", agent_id="1000002", app_secret="s3cr3t")

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="wecom-code-abc", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_wecom_rejects_nonzero_errcode(monkeypatch):
    _install_wecom_http_stub(
        monkeypatch, getuserinfo_response={"errcode": 40029, "errmsg": "invalid code"}
    )
    provider = WeComProvider(corp_id="corp-alpha", agent_id="1000002", app_secret="s3cr3t")

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="bad-code", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_wecom_rejects_non_json_response(monkeypatch):
    _install_wecom_http_stub(monkeypatch, getuserinfo_raw_content=b"not json at all {{{")
    provider = WeComProvider(corp_id="corp-alpha", agent_id="1000002", app_secret="s3cr3t")

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="c", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_wecom_rejects_valid_json_that_is_not_an_object(monkeypatch):
    _install_wecom_http_stub(monkeypatch, getuserinfo_raw_content=b"[1, 2, 3]")
    provider = WeComProvider(corp_id="corp-alpha", agent_id="1000002", app_secret="s3cr3t")

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="c", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_wecom_never_trusts_the_response_for_which_corp_it_belongs_to(monkeypatch):
    """WeCom's getuserinfo response carries no field identifying which corp
    the presenter was checked against -- there is nothing in the payload
    for this adapter to cross-check a caller-claimed corp against.

    What the code actually relies on INSTEAD: the corp id folded into
    ``subject`` always comes from this adapter *instance's own configured*
    ``corp_id`` (fixed at construction time from ``PortalSettings.wecom_corp_id``),
    never from the API response. This test replays the exact same raw
    response against two differently-configured instances and shows they
    resolve to two DIFFERENT subjects -- proving a response can never be
    silently misattributed to the wrong corp's identity space, precisely
    because the response is never trusted for that field at all.
    """
    _install_wecom_http_stub(
        monkeypatch, getuserinfo_response={"errcode": 0, "UserId": "shared-userid-string"}
    )
    provider_a = WeComProvider(corp_id="corp-A", agent_id="1", app_secret="secret-a")
    provider_b = WeComProvider(corp_id="corp-B", agent_id="2", app_secret="secret-b")

    identity_a = await provider_a.resolve_identity(
        code="c", redirect_uri="https://portal.test.local/x", provider_state={}
    )
    identity_b = await provider_b.resolve_identity(
        code="c", redirect_uri="https://portal.test.local/x", provider_state={}
    )

    assert identity_a.subject == "wecom:6:corp-A:shared-userid-string"
    assert identity_b.subject == "wecom:6:corp-B:shared-userid-string"
    assert identity_a.subject != identity_b.subject

    # And the encoding is unambiguous, which plain `f"{corp}:{user}"` was not:
    # a corp_id containing the separator cannot be made to collide with a
    # different (corp_id, user_id) pair. Two people sharing one
    # PortalOAuthIdentity row is the failure this format prevents.
    colliding_a = _wecom_subject("corp:X", "y")
    colliding_b = _wecom_subject("corp", "X:y")
    assert colliding_a != colliding_b, (
        f"ambiguous subject encoding: {colliding_a!r} collides across different "
        "(corp_id, user_id) pairs"
    )


# -- Feishu -------------------------------------------------------------


def _feishu_token_response(
    *, code: int = 0, access_token: str = "user-access-token-xyz"
) -> CreateOidcAccessTokenResponse:
    resp = CreateOidcAccessTokenResponse()
    resp.code = code
    resp.msg = "ok" if code == 0 else "rejected"
    resp.data = (
        CreateOidcAccessTokenResponseBody.builder().access_token(access_token).build()
        if code == 0
        else None
    )
    return resp


def _feishu_user_info_response(
    *,
    code: int = 0,
    union_id: str | None = "u-999",
    open_id: str | None = "o-999",
    name: str | None = None,
    email: str | None = None,
) -> GetUserInfoResponse:
    resp = GetUserInfoResponse()
    resp.code = code
    resp.msg = "ok" if code == 0 else "rejected"
    if code == 0:
        body = GetUserInfoResponseBody()
        body.union_id = union_id
        body.open_id = open_id
        body.name = name
        body.email = email
        resp.data = body
    else:
        resp.data = None
    return resp


def _install_feishu_stub(
    monkeypatch,
    provider: FeishuProvider,
    *,
    token_response: CreateOidcAccessTokenResponse,
    user_info_response: GetUserInfoResponse,
) -> None:
    monkeypatch.setattr(
        provider._client.authen.v1.oidc_access_token,
        "create",
        lambda request: token_response,
    )
    monkeypatch.setattr(
        provider._client.authen.v1.user_info,
        "get",
        lambda request, option: user_info_response,
    )


@pytest.mark.asyncio
async def test_feishu_resolves_identity_successfully(monkeypatch):
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(access_token="user-tok-1"),
        user_info_response=_feishu_user_info_response(
            union_id="u-999", open_id="o-999", name="Li Si", email="lisi@corp.example"
        ),
    )

    identity = await provider.resolve_identity(
        code="feishu-code", redirect_uri="https://portal.test.local/x", provider_state={}
    )
    assert identity.subject == "u-999"
    assert identity.trust_basis is SsoTrustBasis.AUTHENTICATED_API_RESPONSE
    assert identity.email_verified is False
    assert identity.display_name == "Li Si"


@pytest.mark.asyncio
async def test_feishu_falls_back_to_open_id_when_union_id_absent(monkeypatch):
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(),
        user_info_response=_feishu_user_info_response(union_id=None, open_id="o-only"),
    )

    identity = await provider.resolve_identity(
        code="c", redirect_uri="https://portal.test.local/x", provider_state={}
    )
    assert identity.subject == "o-only"


@pytest.mark.asyncio
async def test_feishu_rejects_user_info_with_no_identifiers_at_all(monkeypatch):
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(),
        user_info_response=_feishu_user_info_response(union_id=None, open_id=None),
    )

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="c", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_feishu_rejects_a_token_exchange_the_provider_itself_refused(monkeypatch):
    """Simulates a code Feishu's own server refuses to exchange (e.g. it was
    never valid for THIS app's credentials, or was already used/is stale).
    There is no app-identifying field in this response for the adapter to
    cross-check -- trust rests entirely on Feishu's own server refusing the
    exchange when the calling app's id/secret do not match what the code
    was issued for. A non-zero ``code`` must never be treated as success."""
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(code=99991663),
        user_info_response=_feishu_user_info_response(),
    )

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="stolen-or-cross-app-code",
            redirect_uri="https://portal.test.local/x",
            provider_state={},
        )


@pytest.mark.asyncio
async def test_feishu_rejects_a_user_info_call_the_provider_itself_refused(monkeypatch):
    """The mirror case at the second call: a user access token that Feishu's
    server itself will not honor for ``user_info`` (e.g. it was actually
    scoped to a different app, or has been revoked) -- again surfaced only
    as a non-zero ``code`` from Feishu's own API, which must be refused
    rather than trusted."""
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(access_token="token-not-valid-for-this-app"),
        user_info_response=_feishu_user_info_response(code=99991663),
    )

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="c", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_feishu_rejects_empty_access_token_in_an_otherwise_successful_response(
    monkeypatch,
):
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(access_token=""),
        user_info_response=_feishu_user_info_response(),
    )

    with pytest.raises(sso_base.SsoIdentityVerificationError):
        await provider.resolve_identity(
            code="c", redirect_uri="https://portal.test.local/x", provider_state={}
        )


@pytest.mark.asyncio
async def test_feishu_never_sets_email_verified_regardless_of_directory_email(monkeypatch):
    provider = FeishuProvider(app_id="app-1", app_secret="secret-1")
    _install_feishu_stub(
        monkeypatch,
        provider,
        token_response=_feishu_token_response(),
        user_info_response=_feishu_user_info_response(
            union_id="u-1", email="someone@corp.example"
        ),
    )

    identity = await provider.resolve_identity(
        code="c", redirect_uri="https://portal.test.local/x", provider_state={}
    )
    assert identity.email == "someone@corp.example"
    assert identity.email_verified is False


# ---------------------------------------------------------------------------
# SSO is a login method for accounts that already exist -- never a signup path.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_sso_identity_creates_no_account_and_grants_no_session(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    """An SSO identity with no existing link must not become an account.

    This pins the phase's strongest security property, and it is structural
    rather than configurable: because no code path creates a ``PortalUser`` from
    an SSO callback, an identity provider cannot mint accounts here, and the
    whole class of "assert a victim's email address to obtain or squat their
    account" is closed by construction rather than by a check that could be
    misconfigured.

    It was previously unpinned. A later convenience feature that provisioned an
    account on first SSO login would silently remove the property with no test
    failing -- which is exactly how this kind of guarantee disappears. If that
    feature is ever wanted deliberately, this test should fail loudly and be
    rewritten alongside an explicit takeover analysis.
    """
    ip = _unique_ip()
    unlinked_email = _unique_email()

    async with portal_sessionmaker() as session:
        before_users = (await session.execute(select(PortalUser))).scalars().all()
        before_identities = (
            (await session.execute(select(PortalOAuthIdentity))).scalars().all()
        )
    before_user_count = len(before_users)
    before_identity_count = len(before_identities)

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject=f"never-seen-{uuid.uuid4().hex}",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=unlinked_email,
        email_verified=True,
    )

    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "real-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
    )

    assert response.status_code >= 400, (
        f"an unlinked SSO identity was accepted with {response.status_code}: "
        f"{response.text}"
    )
    settings = get_portal_settings()
    assert settings.jwt_cookie_name not in response.cookies, (
        "no session cookie may be issued for an identity that has no account"
    )

    async with portal_sessionmaker() as session:
        after_users = (await session.execute(select(PortalUser))).scalars().all()
        after_identities = (
            (await session.execute(select(PortalOAuthIdentity))).scalars().all()
        )
    assert len(after_users) == before_user_count, (
        "the SSO callback created a PortalUser; SSO must never provision accounts"
    )
    assert len(after_identities) == before_identity_count, (
        "the SSO callback wrote a PortalOAuthIdentity without an explicit link"
    )
    assert not any(u.email == unlinked_email for u in after_users), (
        f"an account was created holding the IdP-asserted email {unlinked_email}"
    )


@pytest.mark.asyncio
async def test_unknown_sso_identity_cannot_take_over_an_existing_local_account(
    sso_client: httpx.AsyncClient,
    fake_oidc_provider: _FakeOidcProvider,
    portal_sessionmaker,
):
    """Asserting an existing local account's email grants nothing.

    The takeover shape this closes: an attacker who can make any configured IdP
    assert ``victim@example.com`` -- their own tenant, a self-registered account
    at a public provider, a misconfigured directory -- must not thereby sign in
    as the local user holding that address. Identity resolution is by
    ``(provider, subject)`` only, so the email is never consulted.
    """
    ip = _unique_ip()
    victim = await _insert_user(portal_sessionmaker)

    start_result = await sso_service.start_login(provider_id="oidc", ip=ip)
    fake_oidc_provider.next_identity = VerifiedIdentity(
        provider="oidc",
        subject=f"attacker-controlled-{uuid.uuid4().hex}",
        trust_basis=SsoTrustBasis.SIGNED_ASSERTION,
        email=victim.email,
        email_verified=True,
    )

    response = await sso_client.get(
        "/api/v1/portal/auth/sso/oidc/callback",
        params={"code": "real-code", "state": start_result.state},
        cookies={SSO_STATE_COOKIE: start_result.state},
    )

    assert response.status_code >= 400, (
        f"an identity asserting {victim.email} was accepted with "
        f"{response.status_code}"
    )
    settings = get_portal_settings()
    assert settings.jwt_cookie_name not in response.cookies

    # And nothing was bound to the victim behind the scenes.
    async with portal_sessionmaker() as session:
        rows = (
            (
                await session.execute(
                    select(PortalOAuthIdentity).where(
                        PortalOAuthIdentity.user_id == victim.user_id
                    )
                )
            )
            .scalars()
            .all()
        )
    assert rows == [], "an SSO identity was linked to the victim without confirmation"
