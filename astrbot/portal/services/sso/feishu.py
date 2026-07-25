"""Feishu (飞书) SSO adapter.

Trust basis: :attr:`~astrbot.portal.services.sso.base.SsoTrustBasis.AUTHENTICATED_API_RESPONSE`
-- **not** ``SIGNED_ASSERTION``, for the same structural reason as
:mod:`astrbot.portal.services.sso.wecom` (see that module's docstring for the
long version). Concretely for Feishu: the browser is sent to
``open-apis/authen/v1/authorize`` and comes back with a bare ``code``; this
adapter exchanges that code for a **user access token** via
``open-apis/authen/v1/oidc/access_token`` (an app-authenticated API call, using
this app's own ``app_id``/``app_secret``), then calls
``open-apis/authen/v1/user_info`` with that user access token to get a profile.
Despite the endpoint name containing "oidc", **no ``id_token`` is ever returned
anywhere in this flow** -- there is no JWT, no signature, nothing this portal
verifies cryptographically. Every step of trust here rests on ``feishu_app_secret``
staying confidential and on ``open.feishu.cn`` genuinely being Feishu's API server
(TLS) -- exactly the ``AUTHENTICATED_API_RESPONSE`` shape, never
``SIGNED_ASSERTION``. ``sso_service.py`` structurally enforces this distinction
(see ``astrbot.portal.services.sso.base``'s module docstring) rather than
trusting this docstring alone.

Stable subject: Feishu's ``union_id``, falling back to ``open_id`` only if
``union_id`` is absent from an otherwise-successful response (never Feishu's
``user_id`` or any email field). Feishu documents three different identifiers
for the "same" underlying person, at three different scopes:

* ``open_id`` -- unique per user *per app*. Always present in the base
  ``user_info`` response with no extra permission grant required.
* ``union_id`` -- unique per user *across every app registered under the same
  developer/ISV account*. Also always present in the base response with no
  extra permission grant required. This is Feishu's own purpose-built answer to
  "identify the same person consistently," which is exactly the property a
  stable SSO subject needs.
* ``user_id`` -- unique per user *within one tenant's contacts*, but only
  populated if the app has been separately granted a tenant-contacts read
  permission in the Feishu admin console. A login-only app registration may
  well not have that permission, in which case this field is silently empty --
  making it an unreliable primary choice for a generic adapter that cannot
  assume any particular permission grant.

``union_id`` is picked over ``open_id`` as the primary choice because it is the
identifier Feishu itself designed for cross-app person-identification, and it
costs nothing here (no extra permission needed, always present alongside
``open_id`` in the same response) -- but since this portal deployment only ever
registers one ``feishu_app_id``, ``open_id`` would be equally stable in
practice, hence it is kept as a fallback rather than treated as invalid. Neither
is ever an email address, and neither can be reassigned to a different person
by an administrator the way an email or username can.

Credential separation (rule 6): ``feishu_app_id``/``feishu_app_secret``
(``astrbot.portal.config.PortalSettings``) are a **separate** app registration
used only for this web-login flow. This module never reads, imports, or
otherwise touches ``astrbot/core/platform/sources/lark/``, which holds the
operator's IM-bot credentials for an entirely different purpose (receiving chat
messages). A compromise of one must never be a compromise of the other.

Why this adapter calls ``lark_oapi``'s **synchronous** ``.create()``/``.get()``
methods via ``asyncio.to_thread`` rather than its nominally-async
``.acreate()``/``.aget()`` counterparts: ``lark_oapi`` 1.4.x's async request path
(``Client.arequest`` / the generated ``acreate``/``aget`` wrappers) still calls
``lark_oapi.core.token.verify()`` synchronously before ever awaiting anything,
and ``verify()`` -- when the app access token is not already cached -- fetches
it via ``TokenManager.get_self_app_token()``, which unconditionally calls
``Transport.execute`` (the **blocking**, ``requests``-based transport), never
``Transport.aexecute`` (the ``httpx``-based one), regardless of which of the two
public methods triggered it. So the "async" methods are only genuinely async
for the *second* network call in any flow where the app token needs a fresh
fetch, and can silently block the event loop for the first one -- exactly the
kind of stall this phase's "hard timeout on every outbound call" requirement is
meant to prevent from taking down other in-flight requests on the same worker,
even though the timeout itself (``conf.timeout``, threaded through to both
``Transport.execute`` and ``Transport.aexecute`` identically) is honoured either
way. Routing every call -- both the token exchange and the user-info fetch --
through the synchronous methods via ``asyncio.to_thread`` uniformly sidesteps
this asymmetry entirely (mirrors the exact pattern
:mod:`astrbot.portal.services.sso.oidc` already uses for ``jwt.PyJWKClient``'s
own blocking JWKS fetch). See this phase's handoff report for the full
writeup.

Also note: ``lark_oapi.api.authen.v1.resource.authorize.Authorize.get()`` (the
SDK's generated binding for ``GET /open-apis/authen/v1/authorize``) is
deliberately **not called** anywhere in this file. That endpoint is a
browser-redirect-only page (the user's browser is sent there directly; Feishu
renders a login/consent page and redirects back to ``redirect_uri`` with a
``code`` once the user approves) -- it was never meant to be invoked as a
server-to-server JSON API call the way the rest of ``lark_oapi``'s generated
surface is, and calling it that way would just fetch Feishu's HTML login page
and fail to parse as the ``GetAuthorizeResponse`` shape the binding expects.
This adapter instead builds that URL directly (see
:meth:`build_authorization_request`), exactly as Feishu's own integration guide
documents doing by hand.

Flow implemented, end to end:

1. :meth:`build_authorization_request` -- builds the
   ``open-apis/authen/v1/authorize`` browser-redirect URL with a fresh,
   unguessable ``state``. No PKCE/nonce concept applies here (there is no
   signed assertion to bind one to); ``state`` is still generated and checked
   by ``sso_service.py`` like every other provider, since this flow remains
   CSRF-prone without it.
2. :meth:`resolve_identity` -- exchanges the callback ``code`` for a user
   access token via ``authen/v1/oidc/access_token`` (app-authenticated; the SDK
   auto-fetches/caches the app access token from ``feishu_app_id``/
   ``feishu_app_secret``), then calls ``authen/v1/user_info`` with that user
   access token to resolve profile fields, and returns the
   :class:`~astrbot.portal.services.sso.base.VerifiedIdentity` with
   ``email_verified`` always ``False`` (Feishu's ``email``/``enterprise_email``
   fields are directory attributes, not something the presenter proved
   ownership of via any verification step -- see this module's trust-basis
   section above).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Mapping
from urllib.parse import urlencode

import lark_oapi as lark
import requests
from lark_oapi.api.authen.v1 import (
    CreateOidcAccessTokenRequest,
    CreateOidcAccessTokenRequestBody,
    GetUserInfoRequest,
    GetUserInfoResponseBody,
)
from lark_oapi.core.exception import (
    NoAuthorizationException,
    ObtainAccessTokenException,
)
from lark_oapi.core.model import RequestOption

from astrbot.core import logger
from astrbot.portal.config import SSO_PROVIDER_FEISHU
from astrbot.portal.services.sso.base import (
    AuthorizationRequest,
    SsoIdentityVerificationError,
    SsoTemporaryError,
    SsoTrustBasis,
    VerifiedIdentity,
)

_HTTP_TIMEOUT_SECONDS = 10.0
"""Hard timeout threaded through to ``lark_oapi``'s ``Config.timeout``, applied
by its own ``Transport`` to every outbound call this adapter triggers
(app-token fetch, the oidc code exchange, the user_info call)."""

_AUTHORIZE_URL = "https://open.feishu.cn/open-apis/authen/v1/authorize"
"""Browser-redirect-only login page -- see module docstring for why this is
built by hand rather than via ``lark_oapi``'s generated ``Authorize.get()``
binding."""


class FeishuProvider:
    """Feishu (飞书) SSO adapter. See module docstring for the trust basis and
    the ``lark_oapi`` sync-vs-async call-path rationale."""

    provider_id = SSO_PROVIDER_FEISHU
    trust_basis = SsoTrustBasis.AUTHENTICATED_API_RESPONSE

    def __init__(
        self,
        *,
        app_id: str,
        app_secret: str,
        http_timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not app_id or not app_secret:
            raise ValueError("FeishuProvider requires both app_id and app_secret")

        self._app_id = app_id
        # enable_set_token=True is required so this adapter can hand the SDK a
        # *user* access token it obtained itself (for the user_info call)
        # rather than only ever letting the SDK manage tenant/app tokens on its
        # own -- see lark_oapi.core.token.verify()'s branching, and this
        # module's docstring. The app-token path (used for the oidc code
        # exchange) is unaffected by this flag: verify() still auto-fetches it
        # from app_id/app_secret whenever the caller does not supply one via
        # RequestOption, which this adapter never does for that call.
        self._client = (
            lark.Client.builder()
            .app_id(app_id)
            .app_secret(app_secret)
            .timeout(http_timeout_seconds)
            .enable_set_token(True)
            .build()
        )

    # -- SsoProvider protocol --------------------------------------------

    async def build_authorization_request(
        self, *, redirect_uri: str
    ) -> AuthorizationRequest:
        state = secrets.token_urlsafe(32)
        params = {
            "app_id": self._app_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        authorize_url = f"{_AUTHORIZE_URL}?{urlencode(params)}"
        return AuthorizationRequest(
            authorize_url=authorize_url, state=state, provider_state={}
        )

    async def resolve_identity(
        self,
        *,
        code: str,
        redirect_uri: str,
        provider_state: Mapping[str, str],
    ) -> VerifiedIdentity:
        if not code:
            raise SsoIdentityVerificationError(
                "Feishu callback did not include an authorization code"
            )

        user_access_token = await self._exchange_code(code)
        profile = await self._get_user_info(user_access_token)

        subject = profile.union_id or profile.open_id
        if not subject:
            raise SsoIdentityVerificationError(
                "Feishu user_info response contained neither union_id nor open_id"
            )

        display_name = profile.name or profile.en_name
        display_name = display_name if isinstance(display_name, str) and display_name else None

        email = profile.email or profile.enterprise_email
        email = email if isinstance(email, str) and email else None

        return VerifiedIdentity(
            provider=self.provider_id,
            subject=subject,
            trust_basis=self.trust_basis,
            email=email,
            # Feishu's email/enterprise_email are tenant-directory attributes,
            # never something the presenter proved ownership of via a
            # verification step -- always False, per this module's trust-basis
            # section and the base module's contract.
            email_verified=False,
            display_name=display_name,
            raw={"open_id": profile.open_id, "union_id": profile.union_id},
        )

    # -- internals ---------------------------------------------------------

    async def _exchange_code(self, code: str) -> str:
        request = (
            CreateOidcAccessTokenRequest.builder()
            .request_body(
                CreateOidcAccessTokenRequestBody.builder()
                .grant_type("authorization_code")
                .code(code)
                .build()
            )
            .build()
        )
        try:
            # See module docstring: the sync method, off-thread, uniformly --
            # not `.acreate()`.
            response = await asyncio.to_thread(
                self._client.authen.v1.oidc_access_token.create, request
            )
        except (NoAuthorizationException, ObtainAccessTokenException) as exc:
            raise SsoIdentityVerificationError(
                f"Feishu app-credential check failed during token exchange: {exc}"
            ) from exc
        except requests.exceptions.RequestException as exc:
            raise SsoTemporaryError(
                f"Feishu token exchange request failed: {exc}"
            ) from exc

        if not response.success() or response.data is None or not response.data.access_token:
            logger.warning(
                f"[portal][sso][feishu] oidc token exchange rejected: code={response.code}"
            )
            raise SsoIdentityVerificationError(
                f"Feishu rejected the authorization code during token exchange (code={response.code})"
            )
        return response.data.access_token

    async def _get_user_info(self, user_access_token: str) -> GetUserInfoResponseBody:
        request = GetUserInfoRequest.builder().build()
        option = RequestOption.builder().user_access_token(user_access_token).build()
        try:
            response = await asyncio.to_thread(
                self._client.authen.v1.user_info.get, request, option
            )
        except requests.exceptions.RequestException as exc:
            raise SsoTemporaryError(f"Feishu user_info request failed: {exc}") from exc

        if not response.success() or response.data is None:
            logger.warning(
                f"[portal][sso][feishu] user_info rejected: code={response.code}"
            )
            raise SsoIdentityVerificationError(
                f"Feishu rejected the user_info request (code={response.code})"
            )
        return response.data


__all__ = ["FeishuProvider"]
