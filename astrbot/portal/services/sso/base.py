"""The SSO provider abstraction: one interface, two genuinely different trust bases.

This module exists to answer exactly one design question honestly: generic
OIDC and WeCom/Feishu do **not** produce identity the same way, and the
abstraction must never let that difference get papered over.

* **OIDC** (:mod:`astrbot.portal.services.sso.oidc`) hands back an
  ``id_token`` -- a cryptographically **signed assertion** -- whose
  signature, issuer, audience, expiry and nonce are verified against the
  provider's own published JWKS. The trust anchor is a public key the portal
  never has to keep secret.
* **WeCom/Feishu** (adapters not owned by this file set -- see
  ``notes_for_integrator``) have no signed assertion at all: a callback code
  is exchanged, using this app's own confidential ``app_secret``, for an
  **API response** that *claims* to describe a user. The trust anchor there
  is the confidentiality of a shared secret plus the response genuinely
  being for this app's registration -- nothing cryptographically verifiable
  about the payload itself.

Those are not two flavors of the same guarantee; they are different
guarantees. So :class:`VerifiedIdentity` -- the one shape every adapter hands
back to :mod:`astrbot.portal.services.sso_service` -- carries its
``trust_basis`` as a mandatory field on every single instance, not as
incidental metadata the caller has to already know out-of-band. Any code
downstream that wants to treat "this identity's email is provably owned by
the presenter" as true MUST check ``trust_basis == SsoTrustBasis.SIGNED_ASSERTION``
first -- an :class:`AUTHENTICATED_API_RESPONSE` identity can never earn that
trust level no matter what its ``email_verified`` field says, because there
is no signature backing that claim at all, only "the app secret has not
leaked (that we know of)".

``sso_service.py`` additionally cross-checks a returned identity's
``trust_basis`` against the *adapter instance's own* statically-declared
``trust_basis`` class attribute before accepting it -- see that module's
``security_decisions`` -- specifically so a buggy or malicious adapter
cannot unilaterally claim ``SIGNED_ASSERTION`` for an identity it never
cryptographically verified. This file only defines the shape; that
enforcement lives at the call site since this module intentionally has no
knowledge of which concrete adapters exist.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

# ---------------------------------------------------------------------------
# Trust basis: the load-bearing distinction this whole module exists for.
# ---------------------------------------------------------------------------


class SsoTrustBasis(str, Enum):
    """What was actually verified about a :class:`VerifiedIdentity`, and how."""

    SIGNED_ASSERTION = "signed_assertion"
    """The identity came from a cryptographically signed token (an OIDC
    ``id_token``) whose signature was verified against the issuer's published
    JWKS, together with issuer/audience/expiry/nonce checks. An email on an
    identity at this trust level MAY be trusted as provider-attested,
    provided the token's own ``email_verified`` claim was also checked
    (a signature only proves the IdP said this; it does not by itself prove
    the IdP verified the email address)."""

    AUTHENTICATED_API_RESPONSE = "authenticated_api_response"
    """The identity came from an authenticated API call (this app's
    ``app_secret`` was used to obtain/present a token, then a userinfo-style
    endpoint was called), with no cryptographically signed assertion
    involved anywhere in the chain. Trust rests entirely on the
    confidentiality of the app secret and on the HTTP response genuinely
    coming from the real provider over TLS -- there is no signature to
    verify and no JWKS to check it against. An identity at this trust level
    must never be treated as equivalent to :attr:`SIGNED_ASSERTION` for any
    security decision (e.g. email-based account linking) even if it reports
    an ``email_verified``-shaped field, because nothing here is a *signed*
    claim a relying party could verify independently of trusting the
    provider's live API right now."""


# ---------------------------------------------------------------------------
# Provider-adapter error hierarchy. Kept intentionally separate from
# sso_service.py's own SsoServiceError hierarchy (policy-layer errors) --
# these are protocol/adapter-layer failures (discovery, token exchange,
# signature/claim verification, or a non-OIDC provider's API call failing),
# raised by whatever concrete class implements SsoProvider below.
# ---------------------------------------------------------------------------


class SsoProviderError(Exception):
    """Base class for every failure an :class:`SsoProvider` adapter raises.

    ``sso_service.py`` catches this at the call site rather than letting an
    adapter's underlying SDK/library exception (``httpx.HTTPError``,
    ``jwt.PyJWTError``, a raw ``wechatpy``/``lark_oapi`` exception, ...)
    escape past the service boundary -- callers of ``sso_service`` should
    never need to know or care which HTTP/JWT/enterprise-SDK library a given
    provider adapter happens to use internally.
    """


class SsoDiscoveryError(SsoProviderError):
    """An OIDC discovery document could not be fetched, was not valid JSON,
    was missing a required field, or its ``issuer`` did not match the
    configured issuer. Not applicable to non-OIDC providers, which have no
    discovery step."""


class SsoIdentityVerificationError(SsoProviderError):
    """The provider's response could not be trusted as presented: for OIDC,
    a bad signature, disallowed algorithm, wrong ``iss``/``aud``, expired/
    not-yet-valid token, or a ``nonce`` that does not match the one this
    flow issued; for an API-response provider, a token-exchange or userinfo
    call that failed, returned an unexpected shape, or could not be
    attributed to this app's own registration."""


class SsoTemporaryError(SsoProviderError):
    """A network/timeout failure talking to the provider (discovery, token
    exchange, JWKS fetch, userinfo call). Distinguished from
    :class:`SsoIdentityVerificationError` because this class of failure is
    plausibly transient/retryable, whereas a verification failure never
    becomes valid no matter how many times it is retried."""


# ---------------------------------------------------------------------------
# Data shapes exchanged between sso_service.py and every SsoProvider adapter.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuthorizationRequest:
    """What an adapter hands back from starting a new flow.

    ``state`` is the opaque CSRF-protection value the provider is expected
    to echo back verbatim on its callback -- ``sso_service.py`` uses it as
    the lookup key for the pending-flow record it stores server-side, and
    additionally binds it to the initiating browser via a cookie (see that
    module's docstring). It must be unique per flow and unguessable
    (adapters should generate it with ``secrets.token_urlsafe`` or
    equivalent, never a predictable value).

    ``provider_state`` carries whatever *additional* per-flow secret
    material this specific adapter needs re-presented to
    :meth:`SsoProvider.resolve_identity` at callback time, keyed by name --
    for OIDC this is ``{"nonce": ..., "code_verifier": ...}``; a provider
    with no such additional state (there is currently no such provider in
    scope, but the shape allows for one) may return an empty mapping.
    ``sso_service.py`` treats the contents as opaque strings it merely
    stores and replays back to the same adapter instance -- it never
    inspects or interprets them itself, which is what keeps this seam
    generic across trust bases.
    """

    authorize_url: str
    state: str
    provider_state: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class VerifiedIdentity:
    """The one shape every :class:`SsoProvider` adapter resolves a callback into.

    ``subject`` is always the provider's own stable, non-reassignable
    identifier for this account (OIDC's ``sub`` claim; WeCom/Feishu's own
    internal user id) -- **never** an email address. Emails can change and
    be reassigned by an IdP administrator; matching or storing identity by
    email would silently break (or worse, redirect) the binding the moment
    that happens. ``PortalOAuthIdentity.provider_subject`` stores exactly
    this value.

    ``trust_basis`` is mandatory on every instance (not just documented on
    the producing class) precisely so a consumer of this dataclass can never
    accidentally treat an unverified API-response identity as cryptographically
    attested just because it happened to flow through the same code path as
    an OIDC one -- see this module's docstring.

    ``email_verified`` must be ``False`` whenever the upstream provider
    omitted the concept entirely (no ``email_verified``-shaped field in its
    response at all) -- absence is never treated as "verified by omission".
    Adapters must set this explicitly rather than leaving it at the
    dataclass default and hoping the default is safe; the default below
    (``False``) exists only as a fail-closed guard against an adapter bug
    that forgets to set the field, not as a license to skip setting it.
    """

    provider: str
    subject: str
    trust_basis: SsoTrustBasis
    email: str | None = None
    email_verified: bool = False
    display_name: str | None = None
    raw: Mapping[str, Any] = field(default_factory=dict)
    """Adapter-specific decoded claims/profile payload, kept only for audit/
    debugging -- never re-derive an authorization decision from this field
    downstream; every value that matters for a decision must already be one
    of the named fields above."""

    def __post_init__(self) -> None:
        if not self.subject:
            raise ValueError("VerifiedIdentity.subject must not be empty")
        if not self.provider:
            raise ValueError("VerifiedIdentity.provider must not be empty")


# ---------------------------------------------------------------------------
# The provider protocol itself.
# ---------------------------------------------------------------------------


@runtime_checkable
class SsoProvider(Protocol):
    """What ``sso_service.py`` needs from any identity provider adapter.

    Deliberately two async methods and nothing else -- no shared "verify"
    step that could be tricked into skipping OIDC's cryptographic checks by
    a provider that simply declares itself non-OIDC. Each concrete adapter
    performs its *entire* trust chain (whatever that looks like for its own
    protocol) inside :meth:`resolve_identity` and returns only the fully
    resolved, already-verified-per-its-own-rules :class:`VerifiedIdentity` --
    there is no intermediate "partially verified" shape for ``sso_service.py``
    to misuse.

    ``provider_id`` must be one of ``astrbot.portal.config.ALL_SSO_PROVIDERS``.
    ``trust_basis`` is a fixed class-level property of the adapter (an OIDC
    adapter is always :attr:`SsoTrustBasis.SIGNED_ASSERTION`; a WeCom/Feishu
    adapter is always :attr:`SsoTrustBasis.AUTHENTICATED_API_RESPONSE`) --
    it does not vary per call, which is exactly what lets ``sso_service.py``
    cross-check it against whatever a given ``resolve_identity`` call
    returns (see module docstring).
    """

    provider_id: str
    trust_basis: SsoTrustBasis

    async def build_authorization_request(
        self, *, redirect_uri: str
    ) -> AuthorizationRequest:
        """Start a new flow: return the URL to redirect the browser to plus
        whatever per-flow secret state (``state``, and any
        ``provider_state`` such as OIDC's ``nonce``/PKCE ``code_verifier``)
        must be persisted server-side until the callback arrives.

        Must not perform the redirect itself, must not raise for anything
        other than a genuine failure to construct the request (e.g. OIDC
        discovery failing) -- raises :class:`SsoProviderError` subclasses on
        failure.
        """
        ...

    async def resolve_identity(
        self,
        *,
        code: str,
        redirect_uri: str,
        provider_state: Mapping[str, str],
    ) -> VerifiedIdentity:
        """Complete a flow: exchange ``code`` for identity and return a
        fully verified :class:`VerifiedIdentity`, or raise.

        ``redirect_uri`` must be byte-for-byte the same value passed to
        :meth:`build_authorization_request` for this same flow (some
        providers re-check it during token exchange; all of them are
        entitled to). ``provider_state`` is exactly what this same adapter
        returned from :meth:`build_authorization_request` at flow start,
        replayed back verbatim by ``sso_service.py`` from its pending-flow
        store -- the caller (``sso_service.py``) has already validated
        ``state``/CSRF-binding by the time this is called; this method only
        ever receives a request for a flow whose ``state`` was already
        confirmed to belong to the presenting browser.

        Raises:
            SsoDiscoveryError: (OIDC only) discovery document invalid.
            SsoIdentityVerificationError: signature/claims/nonce failed, or
                (non-OIDC) the provider's API call failed/returned garbage.
            SsoTemporaryError: a network/timeout failure -- plausibly worth
                a retry at the caller's discretion.
        """
        ...


__all__ = [
    "SsoTrustBasis",
    "SsoProviderError",
    "SsoDiscoveryError",
    "SsoIdentityVerificationError",
    "SsoTemporaryError",
    "AuthorizationRequest",
    "VerifiedIdentity",
    "SsoProvider",
]
