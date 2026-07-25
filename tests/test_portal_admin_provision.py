"""Guardrail tests for the portal module's Phase 5a operator-provisioning
surface: admin-issued invites (``invite_service.py`` + the invite CRUD in
``admin_routes.py``), invite redemption (``auth_routes.py``'s
``POST /auth/invite/redeem``), and the portal-user disable/enable routes in
``admin_routes.py``.

App/fixture plumbing mirrors ``tests/test_portal_agent_bundle.py``/
``tests/test_portal_upload_policy.py``: a real, temp-file-backed portal
SQLite database (not the process-wide singleton), the real ``auth_router``
+ ``admin_router`` mounted at their real production paths
(``/api/v1/portal/auth/...`` / ``/api/v1/portal/admin/...``), and
``core_facade.get_jwt_secret``/``invite_service.portal_async_sessionmaker``
monkeypatched to point at that same test database and a fixed test secret --
exactly the same two monkeypatch points ``test_portal_auth.py`` and
``test_portal_agent_bundle.py`` already establish as this harness's pattern.

Every handler here is exercised through the real production stack: no
fake/stub of ``invite_service`` or ``session_service`` -- only the
database/JWT-secret plumbing is swapped for a test double, per this
project's existing convention for these guardrail suites.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import jwt
import pytest
import pytest_asyncio
from fastapi import APIRouter, FastAPI
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import col, select

from astrbot.dashboard.responses import ApiError, error
from astrbot.portal.api.admin_routes import router as admin_router
from astrbot.portal.api.auth_routes import router as auth_router
from astrbot.portal.auth import security
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import get_portal_db_session
from astrbot.portal.db.models import (
    PortalBaseModel,
    PortalInvite,
    PortalSession,
    PortalUser,
)
from astrbot.portal.services import core_facade, invite_service, session_service

PORTAL_TEST_SECRET = "portal-admin-provision-test-secret-32b!"
DASHBOARD_JWT_SECRET = "dashboard-admin-provision-test-secret32!"

# A password that satisfies ``validate_dashboard_password`` (>=8 chars, one
# upper, one lower, one digit) -- reused verbatim from the other Phase 1-4
# suites' own register/redemption calls.
STRONG_PASSWORD = "Sup3rSecret!42"
WEAK_PASSWORD = "weak"


# ---------------------------------------------------------------------------
# App / client plumbing.
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def portal_sessionmaker(tmp_path):
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path}/portal-admin-provision-test.db",
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
def app(portal_sessionmaker, monkeypatch) -> FastAPI:
    """Mounts ``auth_router``/``admin_router`` at their real production
    paths (``/api/v1/portal/auth/...`` / ``/api/v1/portal/admin/...``) --
    reconstructed here rather than importing the full
    ``astrbot.portal.api.router.portal_router`` aggregate, since that
    aggregate also pulls in ``chat_routes``/``file_routes``/``kb_routes``/
    ``agent_routes``, none of which this file's assignment needs or owns."""
    application = FastAPI()
    portal_agg = APIRouter(prefix="/portal")
    portal_agg.include_router(auth_router)
    portal_agg.include_router(admin_router)
    application.include_router(portal_agg, prefix="/api/v1")

    application.state.portal_jwt_secret = PORTAL_TEST_SECRET
    # Only needed so require_system_scope (admin routes' gate) can decode a
    # real operator dashboard JWT -- see test_portal_kb_whitelist.py's/
    # test_portal_agent_bundle.py's identical fixture for the same rationale.
    application.state.jwt_secret = DASHBOARD_JWT_SECRET

    @application.exception_handler(ApiError)
    async def _api_error_handler(_request, exc: ApiError):
        return JSONResponse(error(exc.message, exc.data), status_code=exc.status_code)

    async def _override_db_session():
        async with portal_sessionmaker() as session:
            yield session

    application.dependency_overrides[get_portal_db_session] = _override_db_session
    monkeypatch.setattr(invite_service, "portal_async_sessionmaker", portal_sessionmaker)

    # session_service.issue_token_pair calls core_facade.get_jwt_secret(),
    # which (in production) touches the real, process-wide SharedPreferences
    # instance -- patch it to a fixed test secret, matching
    # test_portal_auth.py's own fixture.
    async def _fake_get_jwt_secret() -> str:
        return PORTAL_TEST_SECRET

    monkeypatch.setattr(core_facade, "get_jwt_secret", _fake_get_jwt_secret)

    return application


@pytest_asyncio.fixture
async def client(app: FastAPI):
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------


def _unique_email() -> str:
    return f"invitee-{uuid.uuid4().hex}@example.test"


def _unique_ip() -> str:
    return f"203.0.113.{uuid.uuid4().int % 250 + 1}"


def _admin_headers(username: str = "test-operator") -> dict:
    token = jwt.encode({"username": username}, DASHBOARD_JWT_SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}"}


def _portal_user_auth(user_id: str) -> tuple[dict, dict]:
    """Mint a real portal end-user access-JWT + CSRF cookie/header pair,
    exactly like ``test_portal_agent_bundle.py``'s own ``_auth`` helper."""
    settings = get_portal_settings()
    token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    csrf = security.generate_csrf_token()
    cookies = {settings.jwt_cookie_name: token, settings.csrf_cookie_name: csrf}
    headers = {settings.csrf_header_name: csrf}
    return cookies, headers


async def _insert_user(
    sessionmaker, *, user_id: str, email: str, status: str = "active"
) -> None:
    async with sessionmaker() as session:
        session.add(
            PortalUser(
                user_id=user_id,
                email=email,
                password_hash=None,
                display_name=email.split("@")[0],
                auth_method="local",
                role="user",
                status=status,
            )
        )
        await session.commit()


async def _create_invite(client: httpx.AsyncClient, *, email: str | None = None) -> dict:
    response = await client.post(
        "/api/v1/portal/admin/invites",
        json={"email": email or _unique_email()},
        headers=_admin_headers(),
    )
    assert response.status_code == 200, response.text
    return response.json()


async def _redeem(
    client: httpx.AsyncClient,
    *,
    token: str,
    password: str = STRONG_PASSWORD,
    display_name: str | None = None,
    ip: str | None = None,
) -> httpx.Response:
    """POST to the redeem endpoint.

    ``ip`` defaults to a freshly-generated, never-before-used address (via
    ``X-Forwarded-For``) on every call unless the caller passes one
    explicitly. This is deliberate, mirroring ``test_portal_auth.py``'s own
    per-test unique-IP convention: ``get_login_throttle()`` is a
    process-wide singleton shared across this whole test *file* (and,
    within one pytest session, every other portal test module too), so
    without a fresh IP per call, unrelated tests/attempts would silently
    drain each other's IP-dimension token bucket and turn an expected 200
    into a spurious 429. Tests that specifically want to exercise the
    throttle (the same account key hit repeatedly) still work correctly
    with a fresh IP per attempt, since it is the ACCOUNT dimension --
    keyed by the invite_id, not the IP -- that they intend to saturate.
    """
    payload: dict = {"token": token, "password": password}
    if display_name is not None:
        payload["display_name"] = display_name
    headers = {"X-Forwarded-For": ip or _unique_ip()}
    return await client.post(
        "/api/v1/portal/auth/invite/redeem", json=payload, headers=headers
    )


# ---------------------------------------------------------------------------
# 1. Operator gate: only a real operator dashboard JWT can create an invite;
#    an ordinary portal end-user token is rejected, and so is no token at
#    all. The reverse also holds: an operator token must not authenticate on
#    the end-user portal routes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_invite_with_no_token_is_rejected(client: httpx.AsyncClient):
    response = await client.post(
        "/api/v1/portal/admin/invites", json={"email": _unique_email()}
    )
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_create_invite_rejects_portal_end_user_token(
    client: httpx.AsyncClient, portal_sessionmaker
):
    """A real portal end-user access-JWT, presented the way an attacker
    would naturally try it against an admin-looking endpoint (as an
    ``Authorization: Bearer`` header), must not grant access.

    ``require_scope`` decodes it against the *dashboard's* secret, fails
    (wrong key entirely), and -- because the header uses the ``Bearer``
    scheme -- falls through to treating the token value as an API key
    candidate, which is rejected with 403 ("Insufficient API key scope")
    rather than 401. This is the exact, already-verified behavior
    documented in this phase's own security_decisions ("a real portal
    end-user JWT ... is rejected with 403 by these routes"); either way,
    the point under test -- a portal token never authenticates as an
    operator -- holds.
    """
    user_id = str(uuid.uuid4())
    await _insert_user(portal_sessionmaker, user_id=user_id, email=_unique_email())
    portal_token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )

    response = await client.post(
        "/api/v1/portal/admin/invites",
        json={"email": _unique_email()},
        headers={"Authorization": f"Bearer {portal_token}"},
    )
    assert response.status_code == 403, response.text

    # And the SAME token, presented instead the way a browser would send it
    # (as the portal's own cookie, no Authorization header at all) is
    # rejected too -- require_system_scope never looks at that cookie name,
    # so this is simply "no operator credential presented" -> 401.
    cookies, _headers = _portal_user_auth(user_id)
    cookie_only_response = await client.post(
        "/api/v1/portal/admin/invites",
        json={"email": _unique_email()},
        cookies=cookies,
    )
    assert cookie_only_response.status_code == 401, cookie_only_response.text


@pytest.mark.asyncio
async def test_admin_invite_and_user_routes_all_reject_missing_and_portal_token(
    client: httpx.AsyncClient, portal_sessionmaker
):
    user_id = str(uuid.uuid4())
    await _insert_user(portal_sessionmaker, user_id=user_id, email=_unique_email())
    portal_token = security.mint_access_token(
        user_id=user_id, session_id=str(uuid.uuid4()), secret=PORTAL_TEST_SECRET
    )
    portal_bearer = {"Authorization": f"Bearer {portal_token}"}

    for method, path, kwargs in (
        ("GET", "/api/v1/portal/admin/invites", {}),
        (
            "POST",
            f"/api/v1/portal/admin/invites/{uuid.uuid4()}/revoke",
            {},
        ),
        ("GET", "/api/v1/portal/admin/users", {}),
        ("POST", f"/api/v1/portal/admin/users/{uuid.uuid4()}/disable", {}),
        ("POST", f"/api/v1/portal/admin/users/{uuid.uuid4()}/enable", {}),
    ):
        no_token_resp = await client.request(method, path, **kwargs)
        assert no_token_resp.status_code == 401, (method, path, no_token_resp.text)

        # See test_create_invite_rejects_portal_end_user_token's docstring
        # for why a Bearer-presented portal token yields 403, not 401, here
        # -- both are "rejected", just via a different branch of
        # require_scope's fallback-to-API-key logic.
        portal_token_resp = await client.request(
            method, path, headers=portal_bearer, **kwargs
        )
        assert portal_token_resp.status_code == 403, (method, path, portal_token_resp.text)


@pytest.mark.asyncio
async def test_operator_dashboard_token_does_not_authenticate_end_user_routes(
    client: httpx.AsyncClient,
):
    """The reverse direction: an operator's dashboard JWT, even if smuggled
    into the portal's own cookie name, must not authenticate as a portal
    end user (different signing secret, different claim shape -- mirrors
    ``test_portal_auth.py``'s
    ``test_dashboard_jwt_does_not_authenticate_as_portal_user``)."""
    dashboard_token = jwt.encode(
        {"username": "dashboard-admin"}, DASHBOARD_JWT_SECRET, algorithm="HS256"
    )

    # Not even presented as a bearer token: get_current_user only ever reads
    # the astrbot_portal_jwt cookie, so a bare Authorization header is a 401
    # for a completely different reason (no cookie at all) -- the
    # security-relevant case is the cookie-smuggling attempt below.
    no_cookie_resp = await client.get(
        "/api/v1/portal/auth/me", headers={"Authorization": f"Bearer {dashboard_token}"}
    )
    assert no_cookie_resp.status_code == 401, no_cookie_resp.text

    smuggled_cookie_resp = await client.get(
        "/api/v1/portal/auth/me",
        cookies={"astrbot_portal_jwt": dashboard_token},
    )
    assert smuggled_cookie_resp.status_code == 401, smuggled_cookie_resp.text


# ---------------------------------------------------------------------------
# 2. Redeeming a token twice: the second attempt returns 410, and the
#    account is not modified a second time.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_twice_second_attempt_returns_410_and_account_unchanged(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    token = entry["invite_token"]
    user_id = entry["user_id"]

    first = await _redeem(client, token=token, display_name="First Name")
    assert first.status_code == 200, first.text
    assert first.json()["user_id"] == user_id

    async with portal_sessionmaker() as session:
        user_after_first = await session.get(PortalUser, user_id)
        password_hash_after_first = user_after_first.password_hash
        display_name_after_first = user_after_first.display_name
        status_after_first = user_after_first.status

    # Replay with a DIFFERENT password/display_name -- if the replay were
    # somehow honored, this would be directly observable below.
    second = await _redeem(
        client, token=token, password="TotallyDifferent9!", display_name="Attacker Name"
    )
    assert second.status_code == 410, second.text

    async with portal_sessionmaker() as session:
        user_after_second = await session.get(PortalUser, user_id)
        assert user_after_second.password_hash == password_hash_after_first
        assert user_after_second.display_name == display_name_after_first
        assert user_after_second.status == status_after_first == "active"


# ---------------------------------------------------------------------------
# 3. An expired invite is refused; an unknown token is refused. The
#    responses do not let a stranger distinguish "never existed" from a
#    wrong secret for a real invite -- per invite_service.py's own
#    documented verification order, both collapse to the identical
#    InviteInvalidError/401.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_unknown_invite_id_and_wrong_secret_for_real_one_are_identical(
    client: httpx.AsyncClient,
):
    entry = await _create_invite(client)
    real_invite_id = entry["invite_id"]

    wrong_secret_for_real_invite = await _redeem(
        client, token=f"{real_invite_id}.completely-wrong-secret"
    )
    never_existed = await _redeem(
        client, token=f"{uuid.uuid4()}.also-a-made-up-secret"
    )

    assert wrong_secret_for_real_invite.status_code == 401, wrong_secret_for_real_invite.text
    assert never_existed.status_code == 401, never_existed.text
    assert wrong_secret_for_real_invite.status_code == never_existed.status_code
    assert wrong_secret_for_real_invite.json() == never_existed.json(), (
        "a wrong secret against a REAL invite_id must be indistinguishable "
        "from an invite_id that never existed at all -- otherwise the "
        "invite_id namespace becomes probeable"
    )


@pytest.mark.asyncio
async def test_redeem_malformed_token_is_also_the_same_generic_401(
    client: httpx.AsyncClient,
):
    """No delimiter at all -- the token doesn't even parse into
    (invite_id, secret) -- must still be the same generic response."""
    response = await _redeem(client, token="not-even-a-real-token-shape")
    assert response.status_code == 401, response.text


@pytest.mark.asyncio
async def test_redeem_expired_invite_is_refused_with_410(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    invite_id = entry["invite_id"]

    # Force expiry directly on the persisted row -- faster and more precise
    # than overriding invite_token_ttl_seconds and sleeping.
    async with portal_sessionmaker() as session:
        row = await session.get(PortalInvite, invite_id)
        row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        session.add(row)
        await session.commit()

    response = await _redeem(client, token=entry["invite_token"])
    assert response.status_code == 410, response.text

    # And the account was never activated by the expired attempt.
    async with portal_sessionmaker() as session:
        user = await session.get(PortalUser, entry["user_id"])
        assert user.status == "invited"
        assert user.password_hash is None


# ---------------------------------------------------------------------------
# 4. The stored token is a hash, not plaintext.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persisted_invite_row_never_stores_the_plaintext_token(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    plaintext_token = entry["invite_token"]
    invite_id = entry["invite_id"]
    plaintext_secret = plaintext_token.split(".", 1)[1]

    async with portal_sessionmaker() as session:
        row = await session.get(PortalInvite, invite_id)

    assert row is not None
    assert row.token_hash != plaintext_token
    assert row.token_hash != plaintext_secret
    assert plaintext_secret not in row.token_hash
    assert plaintext_token not in row.token_hash
    # A PBKDF2-style hash from hash_dashboard_password is a structured,
    # much longer string than the raw secret -- a real hash, not merely a
    # different-looking string.
    assert len(row.token_hash) > len(plaintext_secret)

    # And the hash actually verifies against the real secret (round-trips
    # through the same primitive the service uses) -- proves this is a
    # genuine verifiable hash, not e.g. an unrelated random string.
    assert security.verify_refresh_secret(row.token_hash, plaintext_secret)
    assert not security.verify_refresh_secret(row.token_hash, "some-other-secret")


# ---------------------------------------------------------------------------
# 5. Redemption creates/activates the user with the right
#    auth_method/status, applies the password policy, and issues a working
#    session (the redeemed user can then call an authenticated endpoint).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_redeem_activates_user_with_correct_fields_and_issues_working_session(
    client: httpx.AsyncClient, portal_sessionmaker
):
    email = _unique_email()
    entry = await _create_invite(client, email=email)

    # Before redemption: pre-created, inert resting state.
    async with portal_sessionmaker() as session:
        pre = await session.get(PortalUser, entry["user_id"])
        assert pre.status == "invited"
        assert pre.auth_method == "invited"
        assert pre.password_hash is None

    response = await _redeem(client, token=entry["invite_token"], display_name="Real Name")
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["email"] == email
    assert body["display_name"] == "Real Name"
    assert body["role"] == "user"
    assert body["status"] == "active"

    async with portal_sessionmaker() as session:
        post = await session.get(PortalUser, entry["user_id"])
        assert post.status == "active"
        # auth_method: the account remains an invited-provenance account --
        # invite_service.redeem_invite only ever updates password_hash and
        # status (see its own docstring's "atomic single-use activation"
        # step); auth_method is not one of the fields it touches.
        assert post.auth_method == "invited"
        assert post.password_hash is not None
        assert post.password_hash != STRONG_PASSWORD

    # The session actually works: the returned auth cookies authenticate a
    # real, subsequent authenticated request.
    settings = get_portal_settings()
    access_cookie = response.cookies.get(settings.jwt_cookie_name)
    assert access_cookie
    me_response = await client.get(
        "/api/v1/portal/auth/me", cookies={settings.jwt_cookie_name: access_cookie}
    )
    assert me_response.status_code == 200, me_response.text
    assert me_response.json()["user_id"] == entry["user_id"]
    assert me_response.json()["status"] == "active"


@pytest.mark.asyncio
async def test_redeem_rejects_password_failing_the_policy(client: httpx.AsyncClient):
    entry = await _create_invite(client)
    response = await _redeem(client, token=entry["invite_token"], password=WEAK_PASSWORD)
    assert response.status_code == 400, response.text


@pytest.mark.asyncio
async def test_redeem_weak_password_does_not_burn_the_invite(
    client: httpx.AsyncClient, portal_sessionmaker
):
    """A merely-weak password must never consume the invite -- the invitee
    can retry with a stronger one on the same link."""
    entry = await _create_invite(client)

    weak_attempt = await _redeem(client, token=entry["invite_token"], password=WEAK_PASSWORD)
    assert weak_attempt.status_code == 400, weak_attempt.text

    strong_attempt = await _redeem(client, token=entry["invite_token"])
    assert strong_attempt.status_code == 200, strong_attempt.text


# ---------------------------------------------------------------------------
# 6. Concurrency: two simultaneous redemptions of the same token -- exactly
#    one succeeds. Mirrors test_portal_upload_policy.py's own
#    concurrent-quota test pattern (asyncio.gather over the same client).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_redemption_of_the_same_token_exactly_one_succeeds(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    token = entry["invite_token"]
    # Exactly the login-throttle's per-account bucket capacity (see
    # PortalSettings.auth_rate_limit_capacity) -- high enough to be a real
    # concurrency stress test, but not so high it spuriously 429s some
    # attempts via the (separate, not-under-test-here) rate limiter before
    # they ever reach the single-use redemption race.
    attempts = 5

    responses = await asyncio.gather(
        *(_redeem(client, token=token, password=STRONG_PASSWORD) for _ in range(attempts))
    )

    succeeded = [r for r in responses if r.status_code == 200]
    lost_race = [r for r in responses if r.status_code in (410, 409)]

    assert len(succeeded) == 1, (
        f"expected exactly one concurrent redemption to succeed, got "
        f"{len(succeeded)} (statuses: {[r.status_code for r in responses]})"
    )
    assert len(succeeded) + len(lost_race) == attempts, (
        f"every concurrent attempt must either win or lose cleanly, got "
        f"{[r.status_code for r in responses]}"
    )

    async with portal_sessionmaker() as session:
        user = await session.get(PortalUser, entry["user_id"])
        assert user.status == "active"
        assert user.password_hash is not None

        invite_row = await session.get(PortalInvite, entry["invite_id"])
        assert invite_row.consumed_at is not None


# ---------------------------------------------------------------------------
# 7. Rate limiting applies to redemption, as it does to login.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_repeated_failed_redemption_attempts_trigger_rate_limiting(
    client: httpx.AsyncClient,
):
    entry = await _create_invite(client)
    invite_id = entry["invite_id"]

    statuses = []
    for _ in range(6):
        response = await _redeem(client, token=f"{invite_id}.wrong-secret-attempt")
        statuses.append(response.status_code)

    assert statuses[:5] == [401] * 5, statuses
    assert statuses[5] == 429, statuses


# ---------------------------------------------------------------------------
# 8. Revoking a pending invite makes it unusable; revoking a consumed one
#    does not resurrect anything.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoke_pending_invite_makes_it_unusable(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)

    revoke_response = await client.post(
        f"/api/v1/portal/admin/invites/{entry['invite_id']}/revoke",
        headers=_admin_headers(),
    )
    assert revoke_response.status_code == 200, revoke_response.text
    assert revoke_response.json()["status"] == "revoked"

    redeem_response = await _redeem(client, token=entry["invite_token"])
    assert redeem_response.status_code == 410, redeem_response.text

    async with portal_sessionmaker() as session:
        user = await session.get(PortalUser, entry["user_id"])
        assert user.status == "invited"
        assert user.password_hash is None


@pytest.mark.asyncio
async def test_revoke_already_consumed_invite_does_not_resurrect_anything(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    redeemed = await _redeem(client, token=entry["invite_token"])
    assert redeemed.status_code == 200, redeemed.text

    async with portal_sessionmaker() as session:
        user_before = await session.get(PortalUser, entry["user_id"])
        password_hash_before = user_before.password_hash

    revoke_response = await client.post(
        f"/api/v1/portal/admin/invites/{entry['invite_id']}/revoke",
        headers=_admin_headers(),
    )
    # invite_service.admin_revoke_invite raises InviteAlreadyConsumedError
    # for this case -- revoking an already-activated account's invite is not
    # something "revoking the invite" could retroactively undo (see that
    # function's own docstring); admin_routes.py maps it to 410.
    assert revoke_response.status_code == 410, revoke_response.text

    async with portal_sessionmaker() as session:
        user_after = await session.get(PortalUser, entry["user_id"])
        # The account is completely unaffected by the failed revoke attempt
        # -- still active, same password, not reverted to 'invited'.
        assert user_after.status == "active"
        assert user_after.password_hash == password_hash_before

        invite_row = await session.get(PortalInvite, entry["invite_id"])
        assert invite_row.revoked_at is None, (
            "an already-consumed invite must not be marked revoked by a "
            "failed revoke attempt either -- the original consumed_at "
            "audit trail stays exactly as it was"
        )


# ---------------------------------------------------------------------------
# 9. Disabling a user actually cuts off access: an already-issued access
#    token stops working, and the refresh family cannot mint a new one.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disable_user_breaks_both_the_access_token_and_the_refresh_family(
    client: httpx.AsyncClient,
):
    entry = await _create_invite(client)
    redeemed = await _redeem(client, token=entry["invite_token"])
    assert redeemed.status_code == 200, redeemed.text

    settings = get_portal_settings()
    access_cookie = redeemed.cookies.get(settings.jwt_cookie_name)
    refresh_cookie = redeemed.cookies.get(settings.refresh_cookie_name)
    assert access_cookie
    assert refresh_cookie

    # Sanity: both cookies actually work BEFORE disabling.
    pre_me = await client.get(
        "/api/v1/portal/auth/me", cookies={settings.jwt_cookie_name: access_cookie}
    )
    assert pre_me.status_code == 200, pre_me.text

    disable_response = await client.post(
        f"/api/v1/portal/admin/users/{entry['user_id']}/disable",
        headers=_admin_headers(),
    )
    assert disable_response.status_code == 200, disable_response.text
    assert disable_response.json()["status"] == "disabled"

    # The already-issued access token stops working immediately (get_current_user
    # re-reads PortalUser.status from the DB on every call -- never trusts
    # only the JWT payload).
    post_me = await client.get(
        "/api/v1/portal/auth/me", cookies={settings.jwt_cookie_name: access_cookie}
    )
    assert post_me.status_code == 403, post_me.text

    # The refresh family cannot mint a new access token either -- disabling
    # revokes every PortalSession row for this user, so the refresh cookie
    # is now recognized as an already-revoked/replayed session.
    refresh_response = await client.post(
        "/api/v1/portal/auth/refresh",
        cookies={settings.refresh_cookie_name: refresh_cookie},
    )
    assert refresh_response.status_code == 401, refresh_response.text


@pytest.mark.asyncio
async def test_disable_user_revokes_sessions_across_every_family(
    client: httpx.AsyncClient, portal_sessionmaker
):
    """A user may hold more than one independently-issued refresh family
    (e.g. two devices/browsers) -- disabling must kill all of them, not just
    whichever one happens to be presented."""
    entry = await _create_invite(client)
    redeemed = await _redeem(client, token=entry["invite_token"])
    assert redeemed.status_code == 200, redeemed.text
    user_id = entry["user_id"]

    async with portal_sessionmaker() as session:
        second_pair = await session_service.issue_token_pair(session, user_id=user_id)

    await client.post(
        f"/api/v1/portal/admin/users/{user_id}/disable", headers=_admin_headers()
    )

    async with portal_sessionmaker() as session:
        rows = (
            await session.execute(
                select(PortalSession).where(col(PortalSession.user_id) == user_id)
            )
        ).scalars().all()
        assert len(rows) >= 2
        assert all(row.revoked_at is not None for row in rows)

    settings = get_portal_settings()
    refresh_response = await client.post(
        "/api/v1/portal/auth/refresh",
        cookies={settings.refresh_cookie_name: second_pair.refresh_token},
    )
    assert refresh_response.status_code == 401, refresh_response.text


@pytest.mark.asyncio
async def test_enable_reactivates_a_disabled_user(
    client: httpx.AsyncClient, portal_sessionmaker
):
    entry = await _create_invite(client)
    redeemed = await _redeem(client, token=entry["invite_token"])
    assert redeemed.status_code == 200, redeemed.text

    await client.post(
        f"/api/v1/portal/admin/users/{entry['user_id']}/disable", headers=_admin_headers()
    )
    enable_response = await client.post(
        f"/api/v1/portal/admin/users/{entry['user_id']}/enable", headers=_admin_headers()
    )
    assert enable_response.status_code == 200, enable_response.text
    assert enable_response.json()["status"] == "active"

    async with portal_sessionmaker() as session:
        user = await session.get(PortalUser, entry["user_id"])
        assert user.status == "active"


@pytest.mark.asyncio
async def test_enable_refuses_an_account_still_awaiting_invite_acceptance(
    client: httpx.AsyncClient,
):
    entry = await _create_invite(client)  # never redeemed -> still 'invited'
    response = await client.post(
        f"/api/v1/portal/admin/users/{entry['user_id']}/enable", headers=_admin_headers()
    )
    assert response.status_code == 409, response.text


# ---------------------------------------------------------------------------
# 10. Listing invites never returns a plaintext token after issuance.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_invites_never_returns_a_plaintext_token(
    client: httpx.AsyncClient,
):
    entry = await _create_invite(client)
    plaintext_token = entry["invite_token"]
    plaintext_secret = plaintext_token.split(".", 1)[1]

    list_response = await client.get(
        "/api/v1/portal/admin/invites", headers=_admin_headers()
    )
    assert list_response.status_code == 200, list_response.text
    body = list_response.json()

    invites = body["invites"]
    assert any(inv["invite_id"] == entry["invite_id"] for inv in invites)
    for inv in invites:
        assert "invite_token" not in inv
        assert "token_hash" not in inv
        assert "token" not in inv

    serialized = str(body)
    assert plaintext_token not in serialized
    assert plaintext_secret not in serialized


@pytest.mark.asyncio
async def test_list_invites_reflects_computed_status_across_the_lifecycle(
    client: httpx.AsyncClient,
):
    pending_entry = await _create_invite(client)
    consumed_entry = await _create_invite(client)
    revoked_entry = await _create_invite(client)

    redeemed = await _redeem(client, token=consumed_entry["invite_token"])
    assert redeemed.status_code == 200, redeemed.text

    revoke_resp = await client.post(
        f"/api/v1/portal/admin/invites/{revoked_entry['invite_id']}/revoke",
        headers=_admin_headers(),
    )
    assert revoke_resp.status_code == 200, revoke_resp.text

    list_response = await client.get(
        "/api/v1/portal/admin/invites", headers=_admin_headers()
    )
    by_id = {inv["invite_id"]: inv for inv in list_response.json()["invites"]}

    assert by_id[pending_entry["invite_id"]]["status"] == "pending"
    assert by_id[consumed_entry["invite_id"]]["status"] == "consumed"
    assert by_id[revoked_entry["invite_id"]]["status"] == "revoked"
