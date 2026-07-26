"""Guardrails for the portal's cross-origin deployment support.

The portal SPA used to be served by this process at ``/portal``, so every
call it made was same-origin and no CORS layer existed. The SPA now ships as
its own project and may be deployed on a separate origin, which introduces
three things that are easy to get subtly, silently wrong:

* **CORS scope.** The allowlist must reach the end-user portal API and
  nothing else -- not the core dashboard API, and not the portal's own
  operator-only ``/admin`` sub-tree. Both are authenticated by the core
  dashboard's ``astrbot_dashboard_jwt`` *cookie*, i.e. ambient authority a
  permitted origin could otherwise drive from a logged-in operator's browser.
* **Cookie attributes.** ``SameSite=Lax`` cookies are simply not attached to
  cross-site XHR, so a cross-origin deployment left on the default would look
  like "login succeeds, every later request 401s".
* **Fail-closed configuration.** A misconfigured deployment must refuse to
  start rather than quietly running with a wider or narrower surface than the
  operator believes.

The default (empty ``cors_allowed_origins``) must remain a genuine no-op: no
middleware, no headers, no ``Vary``.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI, Response
from fastapi.testclient import TestClient
from starlette.requests import Request

from astrbot.portal.api import auth_routes
from astrbot.portal.api.cors import install_portal_cors, is_cors_eligible_path
from astrbot.portal.config import (
    DEFAULT_PORTAL_SETTINGS,
    build_portal_settings,
    configure_portal_settings,
    reset_portal_settings,
)
from astrbot.portal.services.session_service import TokenPair

SPA_ORIGIN = "https://chat.example.com"

# Every path the middleware has to classify, with the answer it must give.
# The two lookalikes are the point: a bare `startswith` gets both wrong.
ELIGIBILITY_CASES = [
    ("/api/v1/portal/auth/login", True),
    ("/api/v1/portal/chat/sessions", True),
    ("/api/v1/portal", True),
    ("/api/v1/portal/admin/users", False),
    ("/api/v1/portal/admin", False),
    ("/api/v1/portal/administrators", True),  # not the admin sub-tree
    ("/api/v1/portal-internal/x", False),  # not the portal surface
    ("/api/v1/stats", False),
    ("/api/v1/auth/login", False),
]


@pytest.fixture(autouse=True)
def _restore_settings():
    """No test may leak its override into the next -- ``get_portal_settings``
    is process-wide state read on nearly every request path."""
    yield
    reset_portal_settings()


def _build_app(raw_config: dict | None):
    settings = build_portal_settings(raw_config)
    configure_portal_settings(settings)
    app = FastAPI()
    for path, _ in ELIGIBILITY_CASES:
        app.add_api_route(path, lambda: {"ok": True}, methods=["GET", "POST"])
    installed = install_portal_cors(app, settings)
    return TestClient(app), installed


# ---------------------------------------------------------------------------
# Path scoping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path,eligible", ELIGIBILITY_CASES)
def test_cors_eligibility_by_path(path: str, eligible: bool):
    assert is_cors_eligible_path(path) is eligible


# ---------------------------------------------------------------------------
# Same-origin default: the CORS layer must not exist at all
# ---------------------------------------------------------------------------


def test_no_origins_configured_installs_no_middleware():
    client, installed = _build_app(None)
    assert installed is False

    response = client.get("/api/v1/portal/auth/login", headers={"Origin": SPA_ORIGIN})

    assert response.status_code == 200
    # Not merely "origin not allowed" -- no CORS machinery ran at all, so
    # there is no Vary: Origin implying a cross-origin deployment either.
    assert "access-control-allow-origin" not in response.headers
    assert "access-control-allow-credentials" not in response.headers
    assert "origin" not in response.headers.get("vary", "").lower()


def test_empty_origin_list_installs_no_middleware():
    _, installed = _build_app({"cors_allowed_origins": []})
    assert installed is False


# ---------------------------------------------------------------------------
# Cross-origin deployment: headers land on exactly the right paths
# ---------------------------------------------------------------------------


@pytest.fixture
def cross_origin_client():
    client, installed = _build_app(
        {"cors_allowed_origins": [SPA_ORIGIN], "cross_origin_cookies": True}
    )
    assert installed is True
    return client


@pytest.mark.parametrize("path,eligible", ELIGIBILITY_CASES)
def test_cors_headers_only_on_eligible_paths(cross_origin_client, path, eligible):
    response = cross_origin_client.get(path, headers={"Origin": SPA_ORIGIN})

    allow_origin = response.headers.get("access-control-allow-origin")
    if eligible:
        assert allow_origin == SPA_ORIGIN
        assert response.headers.get("access-control-allow-credentials") == "true"
    else:
        assert allow_origin is None
        assert response.headers.get("access-control-allow-credentials") is None


def test_preflight_allows_the_csrf_header(cross_origin_client):
    """The SPA echoes the CSRF cookie back in ``X-Portal-CSRF-Token`` on every
    unsafe request. If preflight does not permit that header, reads keep
    working and every write breaks."""
    response = cross_origin_client.options(
        "/api/v1/portal/chat/sessions",
        headers={
            "Origin": SPA_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type,x-portal-csrf-token",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == SPA_ORIGIN
    assert response.headers["access-control-allow-credentials"] == "true"
    allowed = response.headers["access-control-allow-headers"].lower()
    assert "x-portal-csrf-token" in allowed
    assert "content-type" in allowed
    allowed_methods = response.headers["access-control-allow-methods"]
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert method in allowed_methods


def test_preflight_is_not_answered_for_the_admin_subtree(cross_origin_client):
    """``/api/v1/portal/admin/*`` is gated by the operator's dashboard cookie,
    so it must never become reachable from a portal SPA origin."""
    response = cross_origin_client.options(
        "/api/v1/portal/admin/users",
        headers={
            "Origin": SPA_ORIGIN,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert response.headers.get("access-control-allow-origin") is None


def test_unlisted_origin_is_refused(cross_origin_client):
    response = cross_origin_client.get(
        "/api/v1/portal/auth/login", headers={"Origin": "https://evil.example.com"}
    )
    assert response.headers.get("access-control-allow-origin") is None


# ---------------------------------------------------------------------------
# Fail-closed configuration
# ---------------------------------------------------------------------------


def test_wildcard_origin_is_rejected():
    """A wildcard cannot be paired with credentialed requests -- browsers
    reject the response outright, and it would expose every logged-in portal
    session to any site on the internet."""
    with pytest.raises(ValueError, match=r"may not contain '\*'"):
        build_portal_settings({"cors_allowed_origins": ["*"]})


@pytest.mark.parametrize(
    "origin",
    [
        "https://chat.example.com/",  # trailing slash never matches an Origin header
        "https://chat.example.com/app",  # path
        "chat.example.com",  # no scheme
        "ftp://chat.example.com",  # wrong scheme
        "https://",  # no host
    ],
)
def test_malformed_origins_are_rejected(origin: str):
    with pytest.raises(ValueError):
        build_portal_settings({"cors_allowed_origins": [origin]})


def test_cross_origin_cookies_without_an_allowlist_is_rejected():
    with pytest.raises(ValueError, match="cors_allowed_origins is empty"):
        build_portal_settings({"cross_origin_cookies": True})


def test_cross_origin_cookies_requires_https_origins():
    """``SameSite=None`` is only honoured with ``Secure``, which browsers only
    accept over a secure context."""
    with pytest.raises(ValueError, match="requires every entry"):
        build_portal_settings(
            {
                "cors_allowed_origins": ["http://chat.example.com"],
                "cross_origin_cookies": True,
            }
        )


def test_localhost_is_exempt_from_the_https_requirement():
    """Local development must stay workable: browsers treat loopback as a
    secure context."""
    settings = build_portal_settings(
        {
            "cors_allowed_origins": ["http://localhost:5173"],
            "cross_origin_cookies": True,
        }
    )
    assert settings.cors_allowed_origins == ("http://localhost:5173",)


def test_unknown_setting_key_is_rejected():
    """A typo'd setting must not silently leave the value at its default."""
    with pytest.raises(ValueError, match="unknown portal setting"):
        build_portal_settings({"cors_alowed_origins": []})


@pytest.mark.parametrize(
    "key", ["jwt_cookie_name", "csrf_cookie_name", "csrf_header_name", "refresh_cookie_path"]
)
def test_wire_protocol_fields_cannot_be_overridden(key: str):
    """These are a contract with the frontend, not a preference -- renaming
    one from config would break every client while looking like a valid
    setting."""
    with pytest.raises(ValueError, match="may not be overridden"):
        build_portal_settings({key: "anything"})


def test_wrongly_typed_value_is_rejected():
    with pytest.raises(ValueError, match="must be a boolean"):
        build_portal_settings({"cross_origin_cookies": "yes"})


def test_blank_optional_string_reads_as_unset():
    """The shipped config template uses ``""`` placeholders; a blank OIDC
    issuer must read as absent rather than as a value that passes the
    ``bool(...)`` availability checks by accident."""
    settings = build_portal_settings({"oidc_issuer": "  ", "sso_redirect_base_url": ""})
    assert settings.oidc_issuer is None
    assert settings.sso_redirect_base_url is None


def test_sso_redirect_base_url_is_now_reachable_from_config():
    """Previously unreachable dead configuration -- ``get_portal_settings``
    returned a frozen constant, so the entire SSO surface was unusable."""
    settings = build_portal_settings({"sso_redirect_base_url": SPA_ORIGIN})
    assert settings.sso_redirect_base_url == SPA_ORIGIN
    assert DEFAULT_PORTAL_SETTINGS.sso_redirect_base_url is None


# ---------------------------------------------------------------------------
# Cookie attributes
# ---------------------------------------------------------------------------


def _make_request(scheme: str = "http", headers: list | None = None) -> Request:
    return Request(
        {
            "type": "http",
            "method": "POST",
            "scheme": scheme,
            "path": "/api/v1/portal/auth/login",
            "headers": headers or [],
            "query_string": b"",
            "client": ("127.0.0.1", 1234),
        }
    )


def _set_cookies_and_collect(raw_config: dict | None, request: Request) -> dict[str, str]:
    configure_portal_settings(build_portal_settings(raw_config))
    response = Response()
    now = datetime.now(timezone.utc)
    auth_routes._set_auth_cookies(
        request,
        response,
        TokenPair(
            access_token="access-token-value",
            refresh_token="session-id.refresh-secret",
            session_id="session-id",
            family_id="family-id",
            access_expires_at=now + timedelta(minutes=15),
            refresh_expires_at=now + timedelta(days=30),
        ),
        "csrf-token-value",
    )
    return {
        header.split("=", 1)[0]: header
        for header in response.headers.getlist("set-cookie")
    }


ALL_COOKIE_NAMES = (
    "astrbot_portal_jwt",
    "astrbot_portal_refresh",
    "astrbot_portal_csrf",
)


def test_same_origin_cookies_stay_samesite_lax():
    cookies = _set_cookies_and_collect(None, _make_request())

    assert set(cookies) == set(ALL_COOKIE_NAMES)
    for name in ALL_COOKIE_NAMES:
        header = cookies[name].lower()
        assert "samesite=lax" in header, name
        # Plain http, unproxied -> Secure is not justified.
        assert "secure" not in header, name


def test_same_origin_cookies_are_secure_behind_an_https_proxy():
    request = _make_request(headers=[(b"x-forwarded-proto", b"https")])
    cookies = _set_cookies_and_collect(None, request)

    for name in ALL_COOKIE_NAMES:
        header = cookies[name].lower()
        assert "samesite=lax" in header, name
        assert "secure" in header, name


def test_cross_origin_cookies_are_samesite_none_and_secure():
    cookies = _set_cookies_and_collect(
        {"cors_allowed_origins": [SPA_ORIGIN], "cross_origin_cookies": True},
        _make_request(),
    )

    assert set(cookies) == set(ALL_COOKIE_NAMES)
    for name in ALL_COOKIE_NAMES:
        header = cookies[name].lower()
        assert "samesite=none" in header, name
        # Forced on even though this request arrived over plain http: a
        # SameSite=None cookie without Secure is rejected by every browser,
        # which would present as "login silently does nothing".
        assert "secure" in header, name


# ---------------------------------------------------------------------------
# Post-SSO redirect allowlist
#
# This used to be a hardcoded `/portal` -- the path AstrBot itself served the
# SPA at. The SPA is now a separate deployment that picks its own base, so the
# allowlist derives from `sso_redirect_base_url`'s path instead. The security
# property must survive that: no configuration, including a root-served SPA
# (empty prefix), may accept an off-origin destination.
# ---------------------------------------------------------------------------

OPEN_REDIRECT_PAYLOADS = [
    "//evil.example.com/x",  # scheme-relative
    "/\\evil.example.com",  # backslash normalized to // by some agents
    "https://evil.example.com",  # absolute
    "http://evil.example.com/x",
    "evil.example.com",  # no leading slash
    "/portal\r\n/x",  # header/control-char smuggling
    "/portal\tx",
    "",
]


@pytest.mark.parametrize(
    "base_url,prefix",
    [
        (None, "/portal"),  # unset -> legacy default
        ("https://chat.example.com", ""),  # SPA at origin root
        ("https://bot.example.com/portal", "/portal"),
        ("https://x.example.com/apps/chat/", "/apps/chat"),  # trailing slash trimmed
    ],
)
def test_redirect_prefix_derives_from_sso_base_url(base_url, prefix):
    configure_portal_settings(
        build_portal_settings({"sso_redirect_base_url": base_url} if base_url else None)
    )
    assert auth_routes._safe_redirect_prefix() == prefix


@pytest.mark.parametrize(
    "base_url,candidate,expected",
    [
        # SPA at origin root: any on-origin path is one of its own pages.
        ("https://chat.example.com", "/chat", "/chat"),
        ("https://chat.example.com", "/invite/accept", "/invite/accept"),
        # SPA under a sub-path: only that sub-path's own space.
        ("https://bot.example.com/portal", "/portal/chat", "/portal/chat"),
        ("https://bot.example.com/portal", "/chat", None),
        # Path-boundary check: /portalx must not pass a /portal allowlist.
        ("https://bot.example.com/portal", "/portalx/evil", None),
        ("https://x.example.com/apps/chat", "/apps/chat/chat", "/apps/chat/chat"),
        ("https://x.example.com/apps/chat", "/apps/chatx", None),
    ],
)
def test_redirect_target_validation_respects_the_configured_base(
    base_url, candidate, expected
):
    configure_portal_settings(build_portal_settings({"sso_redirect_base_url": base_url}))
    assert auth_routes._validate_redirect_target(candidate) == expected


@pytest.mark.parametrize(
    "base_url",
    [None, "https://chat.example.com", "https://bot.example.com/portal"],
)
@pytest.mark.parametrize("payload", OPEN_REDIRECT_PAYLOADS)
def test_open_redirect_payloads_rejected_under_every_base(base_url, payload):
    """The empty-prefix case is the one worth pinning: with no path to match
    against, the leading-slash / no-netloc / no-control-char guards are the
    only thing standing between an SSO callback and an open redirect."""
    configure_portal_settings(
        build_portal_settings({"sso_redirect_base_url": base_url} if base_url else None)
    )
    assert auth_routes._validate_redirect_target(payload) is None


def test_clearing_cookies_matches_how_they_were_set():
    """``delete_cookie`` must reproduce the same attributes, or the browser
    keeps the original cookie and logout does not actually log out."""
    configure_portal_settings(
        build_portal_settings(
            {"cors_allowed_origins": [SPA_ORIGIN], "cross_origin_cookies": True}
        )
    )
    response = Response()
    auth_routes._clear_auth_cookies(_make_request(), response)

    headers = response.headers.getlist("set-cookie")
    assert len(headers) == len(ALL_COOKIE_NAMES)
    for header in headers:
        lowered = header.lower()
        assert "samesite=none" in lowered
        assert "secure" in lowered
