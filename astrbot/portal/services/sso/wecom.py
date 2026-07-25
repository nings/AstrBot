"""WeCom (企业微信) SSO adapter.

Trust basis: :attr:`~astrbot.portal.services.sso.base.SsoTrustBasis.AUTHENTICATED_API_RESPONSE`
-- **not** ``SIGNED_ASSERTION``. This is the single most important fact about this
file, so it is worth being explicit about what that means in practice: WeCom's
web-login callback hands back a bare ``code``. There is no signed token here at
all -- no JWT, no JWS, nothing this portal could verify against a published key.
Instead, this adapter exchanges that ``code`` for identity by calling WeCom's
``user/getuserinfo`` HTTP API *using this app's own corp access token*, which
itself was obtained using this app's confidential ``wecom_app_secret``. The
entire trust chain therefore rests on exactly two things: (1) ``wecom_app_secret``
never leaking, and (2) ``qyapi.weixin.qq.com`` actually being WeCom's real API
server (TLS). There is no cryptographic proof, independent of trusting that live
API call, that the identity in the response is genuine -- unlike
:mod:`astrbot.portal.services.sso.oidc`, which verifies a signature against a
JWKS the portal never has to trust blindly at request time. See
``astrbot.portal.services.sso.base``'s module docstring for why this distinction
is structurally enforced (``sso_service.py`` cross-checks ``trust_basis`` against
this class's own declared attribute), not just documented here.

Stable subject: ``f"{corp_id}:{UserId}"``, **not** WeCom's ``OpenId`` and never an
email. WeCom's ``user/getuserinfo`` response contains ``UserId`` (this corp's own
internal member id -- the "userid" the corp's address book uses) only when the
presenting account is an actual **member of the configured corp**; for anyone
else it returns only an ``OpenId`` (an anonymous WeCom identity with no
enterprise membership at all). Corp membership is exactly the trust boundary a
"WeCom SSO for our enterprise's portal" feature is supposed to enforce, so a
response with no ``UserId`` is rejected outright (:class:`SsoIdentityVerificationError`)
rather than silently falling back to authenticating a non-member via ``OpenId``.
``UserId`` by itself is only unique *within one corp* (WeCom's own docs are
explicit about this -- the same literal userid string could theoretically mean a
different person under a different corp), so the corp id is folded into the
subject to remove that ambiguity permanently, even though in the common case a
single portal deployment only ever configures one ``wecom_corp_id``: if that
config value is ever repointed at a different corp, this prevents a
```VerifiedIdentity``` collision against a previously linked-but-unrelated person
whose corp happened to reuse the same userid string.

Credential separation (rule 6 of this phase's engineering rules): ``wecom_corp_id``
/``wecom_agent_id``/``wecom_app_secret`` (``astrbot.portal.config.PortalSettings``)
are a **separate** app registration used only for this web-login flow. This
module never reads, imports, or otherwise touches
``astrbot/core/platform/sources/wecom/``, which holds the operator's IM-bot
credentials for an entirely different purpose (receiving chat messages). A
compromise of one must never be a compromise of the other.

Why this file is built entirely on ``httpx`` and never touches
``wechatpy.enterprise``/``wechatpy.oauth`` (both explored first, per the
assignment): two independent, verified defects in wechatpy 1.8.18 make it
unsuitable here rather than merely inconvenient --

1. **No hard timeout on the corp-access-token fetch.** ``BaseWeChatClient``
   accepts a ``timeout=`` constructor argument and honours it on every ordinary
   API call (``_request`` passes ``kwargs['timeout'] = self.timeout``), but its
   own ``_fetch_access_token`` -- the method that actually calls WeCom's
   ``gettoken`` endpoint -- calls ``self._http.get(url=url, params=params)``
   with **no timeout argument at all**, and the automatic retry-on-expired-token
   branch inside ``_handle_result`` calls ``self.fetch_access_token()`` (same
   codepath) on WeCom errcodes 40014/42001. A hung ``gettoken`` endpoint could
   therefore stall a request indefinitely despite the client being constructed
   with an explicit timeout -- directly contradicting this phase's "apply a hard
   timeout to every outbound call" requirement in a way that cannot be worked
   around by passing a different constructor argument.
2. **``WeChatOAuth.authorize_url()`` builds the wrong flow for a browser-based
   web login.** It targets ``open.weixin.qq.com/connect/oauth2/authorize``,
   which is the in-WeCom-client-embedded-webview redirect (usable only when the
   page is opened *inside* the WeCom mobile/desktop app itself) -- not reachable
   from an ordinary external browser hitting the portal's login page -- and it
   never includes ``agentid`` in the URL it builds, which WeCom's current docs
   require even for that in-client flow. The correct construct for "a normal
   browser signs in via WeCom" is the separate scan-QR
   ``login.work.weixin.qq.com/wwlogin/sso/login?login_type=CorpApp`` endpoint,
   which this adapter builds directly (see :meth:`build_authorization_request`).
   ``wechatpy.oauth`` (the sibling top-level module, distinct from
   ``wechatpy.enterprise``) was also examined and is unrelated to WeCom entirely
   -- it targets the *public-account* (``mp.weixin.qq.com``) web-authorization
   product, a different Tencent product with a different endpoint/credential
   shape (``appid``/``secret`` -> ``sns/oauth2/access_token`` -> ``openid``), not
   WeCom's enterprise self-built-app member login.

Given (1), reusing ``wechatpy.enterprise.WeChatClient`` for the *token-exchange*
half of this flow (even while hand-building the authorize URL for (2)) would
still leave an unbounded-timeout hole on every request that needs a corp access
token -- i.e. every request this adapter makes. So this file manages the corp
access token itself (fetch, cache with a refresh margin, single retry-on-expiry)
over plain ``httpx`` with an explicit timeout on every call, exactly as the
assignment's fallback clause anticipates ("fall back to plain httpx calls for
anything the SDK does not cover, since WeCom's endpoints are ordinary HTTP
JSON"). See ``gaps``/``notes_for_integrator`` in this phase's handoff report for
the full writeup of both findings.

Flow implemented, end to end:

1. :meth:`build_authorization_request` -- builds the WeCom scan-login
   (``企业微信授权登录``) URL with a fresh, unguessable ``state``. No PKCE/nonce
   concept exists for this provider (there is no signed assertion to bind one
   to); ``state`` is still generated and checked by ``sso_service.py`` exactly
   like every other provider, since this flow is still CSRF-prone (an attacker
   could otherwise trick a victim's browser into completing *the attacker's*
   WeCom login into the victim's session).
2. :meth:`resolve_identity` -- fetches (or reuses a cached) corp access token,
   calls ``user/getuserinfo`` with the callback ``code``, requires a ``UserId``
   (rejects non-member ``OpenId``-only responses), best-effort enriches with
   ``user/get`` for a display name / corp-directory email (failure here is
   logged and swallowed, not fatal -- a stable subject is what matters for
   security; a display name is a nicety), and returns the resolved
   :class:`~astrbot.portal.services.sso.base.VerifiedIdentity` with
   ``email_verified`` always ``False`` (WeCom has no concept of a user proving
   ownership of their corp-directory email -- it is an admin-set attribute, not
   a verified claim -- so it can never be trusted at
   :attr:`~astrbot.portal.services.sso.base.SsoTrustBasis.SIGNED_ASSERTION`-only
   confidence, see this module's own trust-basis section above).
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx

from astrbot.core import logger
from astrbot.portal.config import SSO_PROVIDER_WECOM
from astrbot.portal.services.sso.base import (
    AuthorizationRequest,
    SsoIdentityVerificationError,
    SsoTemporaryError,
    SsoTrustBasis,
    VerifiedIdentity,
)

_HTTP_TIMEOUT_SECONDS = 10.0
"""Hard timeout applied to every single outbound HTTP call this adapter makes
(corp-token fetch, ``getuserinfo``, ``user/get`` enrichment) -- see this
module's docstring for why this cannot be delegated to wechatpy's own
constructor-level ``timeout=`` here."""

_LOGIN_AUTHORIZE_URL = "https://login.work.weixin.qq.com/wwlogin/sso/login"
"""WeCom's third-party-web-login (scan-QR) page -- the correct construct for a
browser reaching the portal's login page from outside the WeCom client. See
module docstring point (2)."""

_GETTOKEN_URL = "https://qyapi.weixin.qq.com/cgi-bin/gettoken"
_GETUSERINFO_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/getuserinfo"
_USER_GET_URL = "https://qyapi.weixin.qq.com/cgi-bin/user/get"

_TOKEN_REFRESH_MARGIN_SECONDS = 60
"""Refresh the cached corp access token this many seconds before its reported
expiry, so a request in flight right at the boundary doesn't race an
already-expired token. WeCom's own tokens are valid ~7200s; a 60s margin is
negligible against that but enough to absorb ordinary request latency."""

_RETRYABLE_TOKEN_ERRCODES = frozenset({40014, 42001})
"""WeCom errcodes meaning "your access_token is invalid/expired" -- worth
exactly one refresh-and-retry (the token may have been invalidated out from
under our cache, e.g. another process on the same corp+secret refreshed it),
never an unbounded retry loop."""


class WeComProvider:
    """WeCom (企业微信) SSO adapter. See module docstring for the trust basis
    and why this is built on ``httpx`` rather than ``wechatpy``."""

    provider_id = SSO_PROVIDER_WECOM
    trust_basis = SsoTrustBasis.AUTHENTICATED_API_RESPONSE

    def __init__(
        self,
        *,
        corp_id: str,
        agent_id: str,
        app_secret: str,
        http_timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not corp_id or not agent_id or not app_secret:
            raise ValueError(
                "WeComProvider requires corp_id, agent_id, and app_secret"
            )
        try:
            # Validate numeric shape (per PortalSettings.wecom_agent_id's own
            # documented "caller int()-casts this" contract) even though the
            # value is only ever used as a URL query string here -- a
            # non-numeric agentid is a configuration error worth failing on
            # loudly rather than silently sending a malformed login URL.
            agent_id_int = int(agent_id)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"wecom_agent_id must be numeric, got {agent_id!r}"
            ) from exc

        self._corp_id = corp_id
        self._agent_id = str(agent_id_int)
        self._app_secret = app_secret
        self._timeout = http_timeout_seconds

        self._token_lock = asyncio.Lock()
        self._cached_token: str | None = None
        self._cached_token_expires_at: float = 0.0

    # -- SsoProvider protocol --------------------------------------------

    async def build_authorization_request(
        self, *, redirect_uri: str
    ) -> AuthorizationRequest:
        state = secrets.token_urlsafe(32)
        params = {
            "login_type": "CorpApp",
            "appid": self._corp_id,
            "agentid": self._agent_id,
            "redirect_uri": redirect_uri,
            "state": state,
        }
        authorize_url = f"{_LOGIN_AUTHORIZE_URL}?{urlencode(params)}"
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
                "WeCom callback did not include an authorization code"
            )

        access_token = await self._get_access_token()
        data = await self._call_getuserinfo(access_token, code)
        errcode = data.get("errcode", 0)

        if errcode in _RETRYABLE_TOKEN_ERRCODES:
            # Our cached token was invalidated out from under us (e.g. a
            # concurrent refresh elsewhere on the same corp+secret) -- refresh
            # once and retry exactly once, never in an unbounded loop.
            access_token = await self._get_access_token(force_refresh=True)
            data = await self._call_getuserinfo(access_token, code)
            errcode = data.get("errcode", 0)

        if errcode != 0:
            logger.warning(
                f"[portal][sso][wecom] getuserinfo rejected the callback code: errcode={errcode}"
            )
            raise SsoIdentityVerificationError(
                f"WeCom rejected the authorization code (errcode={errcode})"
            )

        user_id = data.get("UserId")
        if not user_id or not isinstance(user_id, str):
            # Only an OpenId came back: this presenter is not a recognized
            # member of the configured corp. Portal login is scoped to actual
            # enterprise members -- fail closed rather than accept an
            # anonymous WeCom identity with no enterprise affiliation at all.
            raise SsoIdentityVerificationError(
                "WeCom identity is not a member of the configured enterprise "
                "(no UserId in the response) -- portal login requires an "
                "existing member of this corp"
            )

        # Length-prefixed rather than `f"{corp_id}:{user_id}"`. Plain
        # concatenation is ambiguous: a ':' appearing in either half lets two
        # different (corp_id, user_id) pairs render as the same string and so
        # collapse onto one `PortalOAuthIdentity` row -- two different people
        # sharing an account. Neither half is attacker-controlled today
        # (`corp_id` is server configuration and WeCom userids are a restricted
        # charset), so this is defence against a future change rather than a live
        # hole; it is done now because the subject is a stored identifier and
        # changing its format after any deployment would orphan every existing
        # link row.
        subject = f"wecom:{len(self._corp_id)}:{self._corp_id}:{user_id}"
        display_name, email = await self._enrich_user(access_token, user_id)

        return VerifiedIdentity(
            provider=self.provider_id,
            subject=subject,
            trust_basis=self.trust_basis,
            email=email,
            # WeCom has no concept of the *user* having verified ownership of
            # this email -- it is an admin-set corp-directory attribute, not a
            # claim the presenter proved. Always False, per this module's
            # trust-basis docstring and the base module's contract.
            email_verified=False,
            display_name=display_name,
            raw={"user_id": user_id},
        )

    # -- internals ---------------------------------------------------------

    async def _get_access_token(self, *, force_refresh: bool = False) -> str:
        """Fetch-or-return-cached corp access token, over plain ``httpx``
        with a hard timeout on the fetch itself -- see module docstring point
        (1) for why this is never delegated to wechatpy."""
        now = time.monotonic()
        if (
            not force_refresh
            and self._cached_token is not None
            and now < self._cached_token_expires_at
        ):
            return self._cached_token

        async with self._token_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and self._cached_token is not None
                and now < self._cached_token_expires_at
            ):
                return self._cached_token

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(
                        _GETTOKEN_URL,
                        params={
                            "corpid": self._corp_id,
                            "corpsecret": self._app_secret,
                        },
                    )
                    response.raise_for_status()
            except httpx.HTTPError as exc:
                raise SsoTemporaryError(
                    f"WeCom corp-token request failed: {exc}"
                ) from exc

            try:
                data = response.json()
            except ValueError as exc:
                raise SsoIdentityVerificationError(
                    "WeCom corp-token response is not valid JSON"
                ) from exc

            if not isinstance(data, dict) or data.get("errcode", 0) != 0:
                errcode = data.get("errcode") if isinstance(data, dict) else None
                logger.warning(
                    f"[portal][sso][wecom] corp-token request rejected: errcode={errcode}"
                )
                raise SsoIdentityVerificationError(
                    f"WeCom corp-token request was rejected (errcode={errcode})"
                )

            token = data.get("access_token")
            if not token or not isinstance(token, str):
                raise SsoIdentityVerificationError(
                    "WeCom corp-token response is missing access_token"
                )

            try:
                expires_in = int(data.get("expires_in", 7200))
            except (TypeError, ValueError):
                expires_in = 7200

            self._cached_token = token
            self._cached_token_expires_at = now + max(
                expires_in - _TOKEN_REFRESH_MARGIN_SECONDS, 30
            )
            return token

    async def _call_getuserinfo(self, access_token: str, code: str) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _GETUSERINFO_URL,
                    params={"access_token": access_token, "code": code},
                )
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise SsoTemporaryError(
                f"WeCom getuserinfo request failed: {exc}"
            ) from exc

        try:
            data = response.json()
        except ValueError as exc:
            raise SsoIdentityVerificationError(
                "WeCom getuserinfo response is not valid JSON"
            ) from exc

        if not isinstance(data, dict):
            raise SsoIdentityVerificationError(
                "WeCom getuserinfo response is not a JSON object"
            )
        return data

    async def _enrich_user(
        self, access_token: str, user_id: str
    ) -> tuple[str | None, str | None]:
        """Best-effort ``user/get`` lookup for a display name / corp-directory
        email. Failure here (missing API scope, transient error, ...) is
        logged and swallowed rather than failing the whole login -- the
        security-relevant part of the identity (the stable ``subject``) has
        already been established by :meth:`resolve_identity` before this is
        ever called; a display name is a cosmetic nicety, not a trust
        decision."""
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.get(
                    _USER_GET_URL,
                    params={"access_token": access_token, "userid": user_id},
                )
                response.raise_for_status()
            data = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning(
                f"[portal][sso][wecom] user/get enrichment failed (non-fatal): {exc}"
            )
            return None, None

        if not isinstance(data, dict) or data.get("errcode", 0) != 0:
            errcode = data.get("errcode") if isinstance(data, dict) else None
            logger.warning(
                f"[portal][sso][wecom] user/get enrichment rejected (non-fatal): errcode={errcode}"
            )
            return None, None

        name = data.get("name")
        email = data.get("email")
        name = name if isinstance(name, str) and name else None
        email = email if isinstance(email, str) and email else None
        return name, email


__all__ = ["WeComProvider"]
