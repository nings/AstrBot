"""Generic OpenID Connect authorization-code adapter -- built on ``pyjwt`` and
``httpx`` only, per this phase's explicit decision (``authlib`` deliberately
NOT added; see this phase's handoff report if that decision is ever revisited).

Trust basis: :attr:`~astrbot.portal.services.sso.base.SsoTrustBasis.SIGNED_ASSERTION`.
Every identity this adapter returns is backed by an ``id_token`` whose
signature was verified against the issuer's own published JWKS, with
``iss``/``aud``/``exp``/``iat``/``nonce`` all checked -- see
:meth:`OidcProvider.resolve_identity` for the exact order and the
algorithm-confusion defense, which is the single most important property of
this file (see module-level ``_ALLOWED_SIGNING_ALGORITHMS`` comment).

Flow implemented, end to end:

1. :meth:`build_authorization_request` -- fetches (and caches) the
   provider's discovery document, generates ``state``/``nonce``/PKCE
   ``code_verifier``+``code_challenge`` (S256), and builds the
   authorization-endpoint redirect URL.
2. :meth:`resolve_identity` -- exchanges the authorization code for tokens
   over ``httpx`` with client authentication and a hard timeout, then
   verifies the returned ``id_token`` via ``jwt.PyJWKClient`` (signature)
   plus explicit ``iss``/``aud``/``exp``/``iat``/``nbf``/``nonce`` checks
   with a bounded clock-skew allowance, and returns the resolved
   :class:`~astrbot.portal.services.sso.base.VerifiedIdentity` -- ``sub``,
   never the email, as the stable subject.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import secrets
import time
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt

from astrbot.core import logger
from astrbot.portal.config import SSO_PROVIDER_OIDC
from astrbot.portal.services.sso.base import (
    AuthorizationRequest,
    SsoDiscoveryError,
    SsoIdentityVerificationError,
    SsoTemporaryError,
    SsoTrustBasis,
    VerifiedIdentity,
)

_HTTP_TIMEOUT_SECONDS = 10.0
"""Hard timeout applied to every outbound HTTP call this adapter makes
(discovery fetch, token exchange, JWKS fetch via PyJWKClient) -- a slow or
hanging IdP must never be able to tie up a portal worker indefinitely."""

_DISCOVERY_CACHE_TTL_SECONDS = 3600
"""How long a fetched discovery document is reused before being re-fetched.
Long enough that every request does not hit the network, short enough that
a provider rotating its endpoints/JWKS URI is picked up within the hour
without a process restart."""

_JWKS_CLIENT_LIFESPAN_SECONDS = 300
"""``PyJWKClient``'s own internal signing-key cache TTL (its ``lifespan``
constructor arg) -- separate from, and much shorter than, the discovery
cache above, since keys rotate far more often than endpoint URLs and a
stale key would hard-fail verification (a security failure mode, not a
silent one) rather than a stale endpoint (which would 404)."""

_CLOCK_SKEW_SECONDS = 120
"""Bounded clock-skew allowance applied uniformly to ``exp``/``iat``/``nbf``
checks (PyJWT's single ``leeway`` parameter covers all three). Generous
enough to absorb ordinary NTP drift between this host and the IdP, narrow
enough that a token genuinely outside its validity window is still
rejected."""

_ALLOWED_SIGNING_ALGORITHMS: tuple[str, ...] = (
    "RS256",
    "RS384",
    "RS512",
    "ES256",
    "ES384",
    "ES512",
    "PS256",
    "PS384",
    "PS512",
)
"""Static, hardcoded allowlist of acceptable ``id_token`` signing algorithms
-- asymmetric only. This is deliberately **never** derived from the
token's own header, from the JWKS entry PyJWKClient happens to match, or
from any other attacker-influenceable input: PyJWT's own documentation is
explicit that computing the ``algorithms`` argument from the token itself
is exactly how classic algorithm-confusion vulnerabilities happen (e.g. an
attacker presents ``alg: HS256`` and signs with the provider's *public* RSA
key re-interpreted as an HMAC secret, or presents ``alg: none`` and no
signature at all). By hardcoding this list independently of anything the
request/response carries, both of those attacks are structurally
impossible regardless of what a forged token's header claims: PyJWT's
``jwt.decode(..., algorithms=[...])`` only accepts a token whose header
``alg`` is a member of the list *we* pass, and this list contains no
symmetric algorithm and no ``none``."""


class OidcProvider:
    """Generic OIDC authorization-code adapter. See module docstring."""

    provider_id = SSO_PROVIDER_OIDC
    trust_basis = SsoTrustBasis.SIGNED_ASSERTION

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str,
        issuer: str | None,
        discovery_url: str | None = None,
        scopes: tuple[str, ...] = ("openid", "email", "profile"),
        http_timeout_seconds: float = _HTTP_TIMEOUT_SECONDS,
    ) -> None:
        if not client_id or not client_secret:
            raise ValueError("OidcProvider requires both client_id and client_secret")
        if not issuer and not discovery_url:
            raise ValueError("OidcProvider requires issuer or discovery_url")

        self._client_id = client_id
        self._client_secret = client_secret
        self._issuer = issuer.rstrip("/") if issuer else None
        self._discovery_url = discovery_url or (
            f"{self._issuer}/.well-known/openid-configuration"
        )
        self._scopes = scopes
        self._timeout = http_timeout_seconds

        self._discovery_cache: dict[str, Any] | None = None
        self._discovery_cache_at: float = 0.0
        self._discovery_lock = asyncio.Lock()

        self._jwk_client: jwt.PyJWKClient | None = None
        self._jwk_client_jwks_uri: str | None = None

    # -- discovery -----------------------------------------------------

    async def _get_discovery(self) -> dict[str, Any]:
        """Fetch-or-return-cached discovery document.

        Discovery is preferred over hand-configured endpoints per this
        phase's decision: it is the provider's own authoritative statement
        of its ``authorization_endpoint``/``token_endpoint``/``jwks_uri``,
        and it self-certifies its own ``issuer`` (checked below), which a
        set of hand-entered URLs cannot do.
        """
        async with self._discovery_lock:
            now = time.monotonic()
            if (
                self._discovery_cache is not None
                and (now - self._discovery_cache_at) < _DISCOVERY_CACHE_TTL_SECONDS
            ):
                return self._discovery_cache

            try:
                async with httpx.AsyncClient(timeout=self._timeout) as client:
                    response = await client.get(self._discovery_url)
                    response.raise_for_status()
                    doc = response.json()
            except httpx.HTTPError as exc:
                raise SsoTemporaryError(
                    f"OIDC discovery fetch failed for {self._discovery_url!r}: {exc}"
                ) from exc
            except ValueError as exc:
                raise SsoDiscoveryError(
                    "OIDC discovery document is not valid JSON"
                ) from exc

            if not isinstance(doc, dict):
                raise SsoDiscoveryError("OIDC discovery document is not a JSON object")

            for required_field in (
                "authorization_endpoint",
                "token_endpoint",
                "jwks_uri",
                "issuer",
            ):
                if not doc.get(required_field):
                    raise SsoDiscoveryError(
                        f"OIDC discovery document missing required field {required_field!r}"
                    )

            doc_issuer = str(doc["issuer"]).rstrip("/")
            if self._issuer is not None and doc_issuer != self._issuer:
                # Fail closed: a discovery document whose self-declared
                # issuer does not match the configured issuer is either a
                # misconfiguration or a redirection attack -- either way,
                # never proceed with endpoints from a document that failed
                # to self-certify.
                raise SsoDiscoveryError(
                    "OIDC discovery document's issuer "
                    f"({doc_issuer!r}) does not match configured issuer "
                    f"({self._issuer!r})"
                )
            # Adopt the (now-verified) issuer for later id_token `iss` checks
            # even if this adapter was constructed with `issuer=None` and a
            # bare `discovery_url` override.
            self._issuer = doc_issuer

            self._discovery_cache = doc
            self._discovery_cache_at = now
            return doc

    def _get_jwk_client(self, jwks_uri: str) -> jwt.PyJWKClient:
        if self._jwk_client is None or self._jwk_client_jwks_uri != jwks_uri:
            self._jwk_client = jwt.PyJWKClient(
                jwks_uri,
                cache_keys=True,
                lifespan=_JWKS_CLIENT_LIFESPAN_SECONDS,
                timeout=self._timeout,
            )
            self._jwk_client_jwks_uri = jwks_uri
        return self._jwk_client

    # -- SsoProvider protocol --------------------------------------------

    async def build_authorization_request(
        self, *, redirect_uri: str
    ) -> AuthorizationRequest:
        discovery = await self._get_discovery()

        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        # 64 raw bytes -> ~86 url-safe base64 chars, comfortably inside
        # RFC 7636's required 43-128 character range for a PKCE code_verifier.
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = _pkce_s256_challenge(code_verifier)

        params = {
            "response_type": "code",
            "client_id": self._client_id,
            "redirect_uri": redirect_uri,
            "scope": " ".join(self._scopes),
            "state": state,
            "nonce": nonce,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        authorize_url = f"{discovery['authorization_endpoint']}?{urlencode(params)}"

        return AuthorizationRequest(
            authorize_url=authorize_url,
            state=state,
            provider_state={"nonce": nonce, "code_verifier": code_verifier},
        )

    async def resolve_identity(
        self,
        *,
        code: str,
        redirect_uri: str,
        provider_state: Mapping[str, str],
    ) -> VerifiedIdentity:
        expected_nonce = provider_state.get("nonce")
        code_verifier = provider_state.get("code_verifier")
        if not expected_nonce or not code_verifier:
            raise SsoIdentityVerificationError(
                "Missing nonce/code_verifier for this OIDC flow; refusing to "
                "verify an id_token with no nonce to check it against"
            )

        discovery = await self._get_discovery()

        token_response = await self._exchange_code(
            token_endpoint=discovery["token_endpoint"],
            code=code,
            redirect_uri=redirect_uri,
            code_verifier=code_verifier,
        )

        id_token = token_response.get("id_token")
        if not id_token or not isinstance(id_token, str):
            raise SsoIdentityVerificationError(
                "OIDC token response did not include an id_token"
            )

        claims = await self._verify_id_token(id_token, jwks_uri=discovery["jwks_uri"])

        token_nonce = claims.get("nonce")
        if not token_nonce or not hmac.compare_digest(
            str(token_nonce), str(expected_nonce)
        ):
            raise SsoIdentityVerificationError(
                "id_token nonce does not match the nonce issued for this flow"
            )

        subject = claims.get("sub")
        if not subject or not isinstance(subject, str):
            raise SsoIdentityVerificationError("id_token is missing a sub claim")

        email = claims.get("email")
        email = email if isinstance(email, str) and email else None

        email_verified_claim = claims.get("email_verified")
        # Some providers encode this as a real bool, a few non-compliant
        # ones as the string "true"/"false". Absence of the claim entirely
        # -- or any other shape -- is treated as unverified, never as
        # verified-by-omission (per this phase's explicit instruction).
        email_verified = email_verified_claim is True or (
            isinstance(email_verified_claim, str)
            and email_verified_claim.strip().lower() == "true"
        )

        display_name = claims.get("name") or claims.get("preferred_username")
        display_name = display_name if isinstance(display_name, str) and display_name else None

        return VerifiedIdentity(
            provider=self.provider_id,
            subject=subject,
            trust_basis=self.trust_basis,
            email=email,
            email_verified=email_verified,
            display_name=display_name,
            raw={"claims": claims},
        )

    # -- internals ---------------------------------------------------------

    async def _exchange_code(
        self,
        *,
        token_endpoint: str,
        code: str,
        redirect_uri: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        """Authorization-code-for-token exchange over ``httpx``.

        Client authentication via ``client_secret_post`` (client id/secret
        in the POST body) -- chosen over ``client_secret_basic`` (HTTP Basic
        auth) because it is the one method essentially every OIDC provider
        in scope for a "generic" client (Google, Microsoft Entra, Keycloak,
        Authentik, Auth0) supports without additional per-deployment
        configuration; a deployment whose provider *requires*
        ``client_secret_basic`` instead is a known, documented gap (see this
        phase's handoff report) rather than a silently broken integration.
        """
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(
                    token_endpoint,
                    data={
                        "grant_type": "authorization_code",
                        "code": code,
                        "redirect_uri": redirect_uri,
                        "client_id": self._client_id,
                        "client_secret": self._client_secret,
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError as exc:
            raise SsoTemporaryError(f"OIDC token exchange request failed: {exc}") from exc

        if response.status_code != 200:
            logger.warning(
                f"[portal][sso][oidc] token exchange rejected by provider: "
                f"status={response.status_code}"
            )
            raise SsoIdentityVerificationError(
                f"OIDC token exchange rejected by provider (status {response.status_code})"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SsoIdentityVerificationError(
                "OIDC token response is not valid JSON"
            ) from exc

        if not isinstance(data, dict):
            raise SsoIdentityVerificationError("OIDC token response is not a JSON object")

        return data

    async def _verify_id_token(self, id_token: str, *, jwks_uri: str) -> dict[str, Any]:
        """Signature verification via ``jwt.PyJWKClient``, then explicit
        ``iss``/``aud``/``exp``/``iat``/``nbf`` checks with bounded clock
        skew. See ``_ALLOWED_SIGNING_ALGORITHMS`` above for the
        algorithm-confusion defense this method depends on.
        """
        jwk_client = self._get_jwk_client(jwks_uri)
        try:
            # PyJWKClient.get_signing_key_from_jwt performs blocking network
            # I/O (JWKS fetch on cache miss) -- run it off the event loop
            # rather than blocking every other in-flight request on this
            # worker while it happens.
            signing_key = await asyncio.to_thread(
                jwk_client.get_signing_key_from_jwt, id_token
            )
        except jwt.PyJWKClientError as exc:
            raise SsoIdentityVerificationError(
                f"Could not resolve a signing key for this id_token: {exc}"
            ) from exc
        except httpx.HTTPError as exc:  # pragma: no cover - PyJWKClient uses urllib, not httpx
            raise SsoTemporaryError(f"JWKS fetch failed: {exc}") from exc

        try:
            claims = jwt.decode(
                id_token,
                key=signing_key.key,
                # Hardcoded allowlist, NOT derived from the token header or
                # from signing_key.algorithm_name -- see module-level
                # constant's docstring for why that distinction is the
                # entire point.
                algorithms=list(_ALLOWED_SIGNING_ALGORITHMS),
                audience=self._client_id,
                issuer=self._issuer,
                leeway=_CLOCK_SKEW_SECONDS,
                options={"require": ["exp", "iat", "sub"]},
            )
        except jwt.InvalidTokenError as exc:
            raise SsoIdentityVerificationError(
                f"id_token failed verification: {exc}"
            ) from exc

        if not isinstance(claims, dict):  # pragma: no cover - defensive
            raise SsoIdentityVerificationError("id_token payload is not a JSON object")

        return claims


def _pkce_s256_challenge(code_verifier: str) -> str:
    """RFC 7636 S256 code_challenge: base64url(sha256(code_verifier)), no padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


__all__ = ["OidcProvider"]
