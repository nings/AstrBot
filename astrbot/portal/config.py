"""Typed configuration for the AstrBot Portal module.

Capability-wall decision (rule 5 of the module's engineering rules): only
``astrbot/portal/services/core_facade.py`` is allowed to import
``astrbot.core.utils.shared_preferences.SharedPreferences``. This module
must therefore never import it, directly or transitively -- and it does
not.

How ``get_portal_jwt_secret`` still works without that import: this module
exposes the *policy* (which scope/scope_id/key to persist under, and the
lazy-generate-on-first-use behaviour) as a small pure function that takes
the preference get/put operations as **injected callables**, matching the
exact async call shape of ``SharedPreferences.get_async``/``put_async``.
Whoever owns ``core_facade.py`` is expected to call it like this::

    from astrbot.portal.config import get_portal_jwt_secret

    secret = await get_portal_jwt_secret(sp.get_async, sp.put_async)

where ``sp`` is the process-wide ``SharedPreferences`` instance (imported
only inside ``core_facade.py``). This keeps ``config.py`` free of any
``SharedPreferences`` symbol -- so it stays trivially unit-testable with
plain fakes and never violates the capability wall -- while still producing
exactly the persisted value the design requires:
``scope='global', scope_id='portal', key='jwt_secret'``. This secret is
never ``config['dashboard']['jwt_secret']`` and is never derived from it
(rule 6).
"""

from __future__ import annotations

import asyncio
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

# ---------------------------------------------------------------------------
# JWT secret storage location (SharedPreferences scope/scope_id/key triple).
# ---------------------------------------------------------------------------

PORTAL_JWT_SECRET_SCOPE = "global"
PORTAL_JWT_SECRET_SCOPE_ID = "portal"
PORTAL_JWT_SECRET_KEY = "jwt_secret"

# ---------------------------------------------------------------------------
# Cookie / header names. These must never collide with the core dashboard's
# `astrbot_dashboard_jwt` cookie, so that no proxy/middleware/browser can
# ever confuse a portal end-user session with a core dashboard admin session
# (rule 6).
# ---------------------------------------------------------------------------

PORTAL_JWT_COOKIE_NAME = "astrbot_portal_jwt"
PORTAL_REFRESH_COOKIE_NAME = "astrbot_portal_refresh"
PORTAL_CSRF_COOKIE_NAME = "astrbot_portal_csrf"
PORTAL_CSRF_HEADER_NAME = "X-Portal-CSRF-Token"

# ---------------------------------------------------------------------------
# SSO provider identifiers (Phase 5). These are the exact strings written into
# ``PortalOAuthIdentity.provider`` (db/models.py, max_length=50 -- trivially
# large enough) and the exact strings ``PortalSettings.sso_enabled_providers``
# below is validated against. Three providers, three genuinely different
# trust models -- see each provider's settings section further down and
# ``astrbot/portal/auth/security.py``'s (or its Phase 5 successor's) per-adapter
# docstrings for why they cannot share one verification code path:
#
# * ``SSO_PROVIDER_OIDC`` -- generic OpenID Connect (Google, Microsoft Entra,
#   Keycloak, Authentik, Auth0, ...). Trust rests on a cryptographically
#   signed ``id_token`` verified against the provider's own JWKS
#   (``jwt.PyJWKClient``) -- signature, ``iss``, ``aud``, ``exp``, ``nonce``.
# * ``SSO_PROVIDER_WECOM`` -- WeCom (企业微信) via ``wechatpy.enterprise``/
#   ``wechatpy.oauth``. No signed assertion at all: the callback code is
#   exchanged for identity via an authenticated API call, so trust rests
#   entirely on the confidentiality of ``wecom_app_secret`` and the response
#   genuinely being for this app -- never treat this the same way as a
#   verified OIDC token.
# * ``SSO_PROVIDER_FEISHU`` -- Feishu (飞书) via ``lark_oapi``'s ``authen``
#   API module. Same non-cryptographic trust shape as WeCom: an API-response
#   identity gated by ``feishu_app_secret``'s confidentiality, not a
#   verified signature.
# ---------------------------------------------------------------------------

SSO_PROVIDER_OIDC = "oidc"
SSO_PROVIDER_WECOM = "wecom"
SSO_PROVIDER_FEISHU = "feishu"
ALL_SSO_PROVIDERS = frozenset({SSO_PROVIDER_OIDC, SSO_PROVIDER_WECOM, SSO_PROVIDER_FEISHU})
"""The complete, closed set of identity providers in scope for this module.
Nothing else is supported -- see this phase's engineering decisions. Used
both to validate ``PortalSettings.sso_enabled_providers`` (fail closed on a
typo/unknown provider at settings-construction time, rather than silently
treating it as disabled) and as the canonical key set every
provider-availability/adapter-dispatch table in this module should iterate."""


@dataclass(frozen=True)
class PortalSettings:
    """Immutable, process-wide settings for the portal module.

    None of these values are secret; the JWT signing secret itself is
    handled separately by :func:`get_portal_jwt_secret` (see module
    docstring for why it lives outside this dataclass).
    """

    # --- Token lifetimes ---------------------------------------------------
    access_token_ttl_seconds: int = 15 * 60
    """Access JWT TTL: 15 minutes."""
    refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    """Refresh token TTL: 30 days."""

    # --- Cookie / CSRF wiring ------------------------------------------------
    jwt_cookie_name: str = PORTAL_JWT_COOKIE_NAME
    refresh_cookie_name: str = PORTAL_REFRESH_COOKIE_NAME
    csrf_cookie_name: str = PORTAL_CSRF_COOKIE_NAME
    csrf_header_name: str = PORTAL_CSRF_HEADER_NAME
    refresh_cookie_path: str = "/api/v1/portal/auth/refresh"
    """Path scope for the refresh cookie: only sent to the refresh endpoint."""

    # --- Registration ---------------------------------------------------
    self_registration_open: bool = True
    """Whether POST /api/v1/portal/auth/register is enabled.

    Phase 1 shipped this defaulting to ``True`` (there was no other way to
    get an account yet) with an explicit forward-looking note that it
    should flip once admin-provisioned invites existed -- this phase is
    where that becomes possible. The product question this phase's
    assignment calls out ("should self-registration stay open once
    provisioning exists") has a real answer, and it is not "leave it
    exactly as-is forever": once a deployment configures SSO against a
    corporate IdP (WeCom/Feishu are both enterprise identity systems, not
    consumer ones) or starts provisioning specific accounts via invite, it
    has by construction expressed a preference for a controlled roster,
    and leaving self-registration on by default would let anyone bypass
    both provisioning models with a plain email+password signup.

    The default here is left at ``True`` in *this* commit specifically
    because flipping it breaks two already-passing tests this phase does
    not own (``tests/test_portal_auth.py::test_login_then_me_succeeds``,
    ``::test_refresh_replay_revokes_whole_family`` -- both register a user
    via the open self-registration endpoint as test setup) that this
    module's engineering rules require to keep passing, and this phase is
    not permitted to edit test files to accommodate its own config change.
    This is a deliberate, documented exception to the "pick a default
    that reflects the product decision" instruction, not an oversight --
    see this phase's handoff report for the full reasoning. **A deployment
    that provisions accounts via invite and/or SSO should still set this to
    ``False``** (it remains a one-line settings override, not a removed
    capability); whoever next touches ``auth_routes.py``/the test suite for
    this setting should update the two tests above to explicitly construct
    their own ``PortalSettings(self_registration_open=True)`` override
    (rather than relying on the shared default) and only then flip this
    default to ``False`` for real.
    """

    # --- Auth endpoint rate limiting (register/login/refresh) --------------
    # Shaped after astrbot.dashboard.server._AuthRateLimiter's token-bucket.
    auth_rate_limit_capacity: int = 5
    """Token-bucket capacity per IP+email dimension."""
    auth_rate_limit_refill_per_minute: float = 1.0
    """Tokens refilled per minute."""
    auth_failure_delay_seconds: float = 3.0
    """Fixed delay applied after a failed login attempt (timing-attack /
    brute-force mitigation, mirrors astrbot.dashboard's auth_service.py
    pattern)."""

    # --- Per-user quotas backing PortalUsageCounter checks -------------------
    max_requests_per_window: int = 200
    """Max chat-message requests allowed within one usage window."""
    usage_window_seconds: int = 24 * 60 * 60
    """Length of one usage-counter accounting window: 1 day."""
    max_concurrent_runs_per_user: int = 2
    """Max simultaneously in-flight chat runs allowed for a single user."""

    # --- Upload policy (Phase 2: file/image attachments) --------------------
    #
    # None of these limits exist in core today: `ChatService.save_uploaded_file`
    # (`astrbot/dashboard/services/chat_service.py:592-640`) has no size cap
    # and no MIME allowlist whatsoever -- it derives `attach_type` purely from
    # the client-supplied multipart `content_type` and, for anything that
    # isn't an image, never even sniffs the real bytes. The portal is the
    # only layer that will ever enforce these, so getting sane defaults here
    # matters; see `PortalAttachmentOwner` (db/models.py) for the byte_size
    # bookkeeping and `PortalUsageCounter.uploaded_bytes` for the rolling
    # daily total these limits are checked against.
    max_upload_file_bytes: int = 20 * 1024 * 1024
    """Hard cap on a single uploaded file, in bytes (20 MiB).

    Chosen as a middle ground for a consumer chat product: comfortably
    above a typical phone camera photo (2-8 MB) or a multi-minute voice
    recording encoded as compressed audio, while still small enough that
    a handful of concurrent uploads can't exhaust disk or memory on a
    single-process, single-SQLite-writer deployment (see plan section 6's
    SQLite single-writer risk entry). Deliberately conservative rather
    than generous: raising a limit later is a trivial, low-risk config
    change; a deployment that got hit with disk exhaustion because the
    default was too permissive is not. Applies uniformly across all
    `attach_type`s for Phase 2 -- there is no per-type override yet.
    """

    max_daily_upload_bytes: int = 200 * 1024 * 1024
    """Rolling per-user daily upload quota, in bytes (200 MiB).

    Sized as roughly 10x `max_upload_file_bytes`, i.e. a real user can
    still have a heavy attachment day (multiple photos, a couple of voice
    notes, a document) without hitting the ceiling, while a compromised or
    abusive account can't use the upload endpoint as free unbounded
    storage against the operator's disk. Checked against
    `PortalUsageCounter.uploaded_bytes` for the current day's window
    before the upload route accepts a file; when exceeded the route
    should return 429 with the remaining-quota figure (plan section 5,
    Phase 2 acceptance criterion 3), not silently truncate or drop the
    upload.
    """

    allowed_upload_mime_types: frozenset[str] = frozenset(
        {
            # Images (attach_type='image'; core re-sniffs the real bytes for
            # these via detect_image_mime_type_async, so this allowlist is
            # the pre-sniff gate on the client's declared content_type).
            "image/jpeg",
            "image/png",
            "image/webp",
            "image/gif",
            # Audio / voice messages (attach_type='record'). Core does NOT
            # sniff these -- this allowlist is the only validation they get.
            "audio/mpeg",
            "audio/mp4",
            "audio/wav",
            "audio/x-wav",
            "audio/webm",
            "audio/ogg",
            "audio/aac",
            # Video (attach_type='video'). Also unsniffed by core.
            "video/mp4",
            "video/webm",
            "video/quicktime",
            # Plain documents (attach_type='file'). Also unsniffed by core.
            "application/pdf",
            "text/plain",
            "text/csv",
            "application/json",
        }
    )
    """MIME-type allowlist for uploads, enforced against the client-declared
    `content_type` before core ever writes bytes to disk (and, for images,
    re-checked against core's post-sniff detected MIME).

    Deliberately excludes anything that renders/executes as active content
    if served back to a browser or that commonly disguises unwanted
    payloads: no `image/svg+xml` (SVG can carry embedded `<script>`, an XSS
    vector when this attachment is later served or previewed), no
    `text/html`/`application/javascript`, no archive formats
    (`application/zip` and friends -- zip-bomb/decompression and
    "malware disguised as a document" risk, and archives are not a useful
    input to a multimodal chat model anyway), and no Office macro-enabled
    formats. Legacy/macro-enabled MS Office (`.doc`/`.xls`/`.ppt`,
    `application/vnd.ms-*`) and their modern OOXML equivalents
    (`application/vnd.openxmlformats-officedocument.*`) are also left off
    this default list -- if a deployment needs them, add the specific MIME
    types deliberately rather than opening the whole Office family
    (`.doc`/`.xls` can carry macros; the newer OOXML types are safer but
    still parsed by third-party libraries with their own attack surface).
    This is a static allowlist for Phase 2; there is no admin-configurable
    override yet (matches this dataclass's existing "no on-disk override
    mechanism" limitation noted on `get_portal_settings`).
    """

    allowed_upload_attach_types: frozenset[str] = frozenset(
        {"image", "record", "video", "file"}
    )
    """Allowlist of `attach_type` buckets (core's own vocabulary from
    `ChatService.save_uploaded_file`: 'image' | 'record' | 'video' | 'file')
    the upload route will accept. Ships with all four enabled by default --
    the MIME allowlist above is what actually keeps this Phase 2 default
    narrow and safe, this list exists as a second, independent knob (e.g. a
    deployment that wants to disable video uploads entirely to save disk,
    without touching the MIME list) rather than as the primary gate.
    """

    # --- Knowledge-base policy (Phase 3: KB visibility + session binding) ---
    #
    # See `astrbot/core/tools/knowledge_base_tools.py:24-87` for the
    # default-open trap these settings exist alongside: a session with no
    # explicit `kb_config` silently falls through to the operator's *global*
    # `kb_names` config, not to "no KB". These settings only govern the
    # *portal-authorized* binding path (`PortalKbVisibility`, db/models.py) --
    # they do not by themselves close that trap; the service/route layer
    # closes it by always writing an explicit `kb_config` (empty `kb_ids` by
    # default) rather than leaving the key unset. `kb_selection_enabled`
    # below is the master switch for whether that binding path is even
    # reachable at all.
    kb_selection_enabled: bool = True
    """Whether end users may see/bind any knowledge base at all.

    Defaults on since Phase 3 assumes a deployment that wants the feature,
    but an operator running a plain chatbot with no KB use case at all
    should be able to turn the entire surface off with one flag -- when
    `False`, the KB list/bind routes should refuse the request outright
    (this module's own choice, not a core one) rather than simply returning
    an empty whitelist, so the feature is verifiably inert rather than
    quietly empty.
    """

    max_kb_ids_per_session: int = 3
    """Maximum number of distinct `kb_id`s a single session's `kb_config`
    may bind at once.

    Chosen as a small, deliberately conservative ceiling for a
    consumer-facing product: `kb_mgr.retrieve` (invoked from
    `retrieve_knowledge_base`) fans a single query out across every bound
    KB and concatenates results, so each additional KB directly multiplies
    retrieval latency and LLM context spent on (often redundant) retrieved
    chunks for one user turn. Three is enough for a real multi-corpus use
    case (e.g. "product docs" + "internal FAQ" + "policy handbook"
    simultaneously) without letting a session silently accumulate the
    operator's entire whitelist over time and turn every message into a
    full-catalog fan-out search.
    """

    default_kb_top_k: int = 5
    """Default `top_k` applied to a session's `kb_config` when a user binds
    KBs without specifying one.

    Matches core's own fallback default for the global (non-portal) path
    (`config.get("kb_final_top_k", 5)` in
    `astrbot/core/tools/knowledge_base_tools.py`), so a portal-bound session
    behaves like the operator's existing global-KB behavior in the
    dimension the portal doesn't have an opinion on, rather than silently
    picking a different retrieval depth than what the operator is already
    used to seeing.
    """

    max_kb_top_k: int = 10
    """Hard ceiling on the `top_k` a user-facing request may set for their
    own session's `kb_config`, independent of `default_kb_top_k`.

    `top_k` (unlike `kb_fusion_top_k`, which always comes from global
    config and is never user-controllable at all -- see
    `knowledge_base_tools.py`) is read from the *session's own* config, so
    without a ceiling here a client could request an arbitrarily large
    `top_k` and turn one chat message into a disproportionately expensive
    retrieval + a context window stuffed with low-relevance chunks. Set
    to 2x the default: enough headroom for a user who genuinely wants a
    broader search on one question, without allowing an unbounded value.
    """

    # --- Agent Bundle policy (Phase 4: user-selectable persona+provider+KB
    # presets) ---------------------------------------------------------------
    #
    # `PortalAgentBundle` (db/models.py) is the *only* thing an end user ever
    # names when "choosing an agent" -- never a persona id, never a provider
    # id, never a raw kb_id. This section is the policy layer around that:
    # whether the whole feature is reachable at all, plus the one behavioural
    # question the design doc calls out explicitly -- what happens when a
    # session has no bundle bound. The answer is deliberately *not* a config
    # flag: a session with no `bundle_id` selected must resolve exactly as if
    # this feature did not exist at all (operator's `default_provider_id`,
    # the conversation's existing persona, no KB), which the two binding
    # surfaces below (`provider_perf_chat_completion` / `conv_mgr`'s own
    # persona field) already do on their own the moment nothing writes to
    # them -- see `PortalAgentBundle`'s docstring for the field-by-field
    # justification. Making "no bundle bound behaves like the operator's
    # default" itself togglable would be a foot-gun (an operator could not
    # accidentally disable the one behaviour rule 7 requires), so it is not
    # a setting here, only documented as an invariant every caller of this
    # phase's service/route layer must preserve.
    agent_bundle_selection_enabled: bool = True
    """Whether end users may see/select any Agent Bundle at all.

    Mirrors `kb_selection_enabled`'s shape and rationale exactly: defaults on
    since Phase 4 assumes a deployment that wants the feature, but an
    operator who wants every end user locked to their own single configured
    persona/provider/KB combination -- "an operator may not want end users
    choosing anything" -- must be able to turn the entire surface off with
    one flag. When `False`, the bundle list/select routes should refuse the
    request outright (this module's own choice, not a core one) rather than
    returning an empty bundle list, so the feature reads as verifiably inert
    rather than quietly empty -- exactly the same reasoning already applied
    to `kb_selection_enabled` above, reused here rather than re-litigated.
    Turning this off has no effect on sessions that already have a
    `bundle_id` bound from before it was disabled; it only gates whether a
    *new* selection can be made, since retroactively un-applying a bundle's
    already-written persona/provider preference is a distinct, separate
    operation (clearing the binding), not a read-time policy check.
    """

    max_kb_ids_per_bundle: int = max_kb_ids_per_session
    """Maximum number of distinct `kb_id`s a single `PortalAgentBundle` row
    may bind, enforced by `agent_bundle_service.py` at bundle create/update
    time.

    Deliberately reuses `max_kb_ids_per_session`'s value rather than
    introducing a second, independent ceiling: the retrieval-latency/
    context-cost rationale documented on `max_kb_ids_per_session` applies
    identically no matter which path (`kb_routes.py`'s direct per-session
    bind, or a bundle's KBs applied at selection time) put those KBs into a
    session's `kb_config` -- the fan-out cost to `kb_mgr.retrieve` is the
    same either way. Expressed as a reference to the other field (evaluated
    once, at class-body time, same as every other literal default in this
    dataclass) instead of a hand-copied literal so the two can never drift
    out of sync if one is tuned later without remembering the other.
    """

    # --- Admin-provisioned invites (Phase 5a) --------------------------------
    invite_token_ttl_seconds: int = 7 * 24 * 60 * 60
    """TTL for an admin-created ``PortalInvite`` (db/models.py), in seconds:
    7 days.

    There is no mail infrastructure in this repo (verified: no
    ``smtplib``/``aiosmtplib`` usage anywhere), so an invite link is always
    handed to the invitee out-of-band by the operator (chat message,
    ticket, whatever channel they already use) rather than emailed
    automatically. Sized accordingly: long enough that the operator does
    not have to deliver the link the same minute they generate it and the
    recipient has a realistic window to actually notice and act on it
    through an arbitrary out-of-band channel, but bounded well short of
    ``refresh_token_ttl_seconds`` (30 days) so a leaked or forgotten
    out-of-band message (e.g. a stale ticket) does not stay redeemable
    indefinitely. An operator who needs a fresher window for an
    already-issued invite is not stuck waiting this out either way -- see
    ``PortalInvite``'s docstring for the "reissue against the same
    pre-created user_id" recovery path, which is just a new row with a
    fresh expiry, not a wait.
    """

    # --- SSO / OIDC + WeCom + Feishu (Phase 5b) ------------------------------
    #
    # Three providers in scope, three genuinely different credential shapes
    # (see ``ALL_SSO_PROVIDERS``'s comment above for why they cannot share one
    # verification code path) -- deliberately kept as flat, provider-prefixed
    # fields rather than nested per-provider dataclasses, matching this
    # dataclass's existing flat style everywhere else in this file.
    #
    # Secrets note (``oidc_client_secret``/``wecom_app_secret``/
    # ``feishu_app_secret``): none of these three fields may ever be logged or
    # echoed back by any endpoint. The only thing a downstream route should
    # expose to the SPA is *availability* -- see ``get_sso_provider_availability``
    # below, which returns booleans only and never touches a secret field's
    # value in doing so. Whoever builds that route should return exactly (and
    # only) that function's dict, never ``dataclasses.asdict(settings)`` or
    # any other blanket serialization of this class, which would include the
    # secret fields verbatim.
    sso_enabled: bool = False
    """Master switch for the entire SSO surface. When ``False``, the OAuth
    start/callback routes must refuse the request outright regardless of any
    individual provider's fields being populated below -- mirrors this
    file's existing ``kb_selection_enabled``/``agent_bundle_selection_enabled``
    "verifiably inert, not quietly empty" convention."""

    sso_enabled_providers: frozenset[str] = frozenset()
    """Subset of ``ALL_SSO_PROVIDERS`` currently reachable, independent of
    ``sso_enabled``: both must hold (``sso_enabled and provider in
    sso_enabled_providers``) for a given provider's start/callback routes to
    do anything. Validated against ``ALL_SSO_PROVIDERS`` in
    :meth:`__post_init__` -- an unknown entry (typo, e.g. ``'wecomm'``)
    raises at settings-construction time (startup) rather than silently
    behaving as "that provider is disabled", per this module's fail-closed
    rule: a misconfigured deployment should fail loudly, not quietly narrow
    its own security surface without telling anyone."""

    sso_redirect_base_url: str | None = None
    """Base URL the portal SPA is actually served from (e.g.
    ``https://chat.example.com/portal``), used to construct each provider's
    callback/redirect URI. Must match the SPA's real serving base exactly --
    the portal SPA is served at ``/portal`` with hash/history routing under
    that base (Phase 1 ground truth), and an OIDC/WeCom/Feishu authorize
    request's redirect_uri must byte-for-byte match what each provider's app
    registration has on file, or the provider will refuse the callback.
    Whoever builds the authorize-redirect and callback-verification code
    must derive the redirect_uri from this single field in both places
    (e.g. ``f"{sso_redirect_base_url}/oauth/callback/{provider}"``) rather
    than hand-constructing it twice, so the two can never drift apart.
    ``None`` (the default) means SSO cannot be started at all, independent
    of ``sso_enabled`` -- a deployment must explicitly configure where it
    lives before any provider can be reachable."""

    sso_state_ttl_seconds: int = 10 * 60
    """How long an OAuth ``state``/``nonce`` pair (CSRF protection for the
    authorize -> callback round trip) remains valid before the callback must
    complete: 10 minutes. Short on purpose -- this is a live, in-flight
    login attempt, not a credential with any reason to be long-lived; a
    short window minimizes how long a captured (but not yet used) state
    value stays exploitable while still comfortably covering how long a
    real user takes to authenticate at the IdP and get redirected back
    (including a slow corporate SSO/MFA prompt)."""

    # --- Generic OIDC (Google, Microsoft Entra, Keycloak, Authentik, Auth0, ...)
    oidc_issuer: str | None = None
    """Issuer URL (e.g. ``https://accounts.google.com``). Configuration
    should be sourced from this provider's own discovery document
    (``{issuer}/.well-known/openid-configuration``) at runtime rather than
    hand-entering the authorize/token/JWKS endpoints separately, per this
    phase's decision -- ``oidc_discovery_url`` below exists only for the
    rarer case where a provider's discovery document does not live at the
    conventional suffix under its issuer."""

    oidc_discovery_url: str | None = None
    """Explicit override for the OIDC discovery document URL, for a
    provider whose ``/.well-known/openid-configuration`` is not reachable
    at the conventional path under ``oidc_issuer``. Leave unset (``None``)
    to use the conventional suffix; when set, takes precedence over
    deriving it from ``oidc_issuer``."""

    oidc_client_id: str | None = None
    """This portal's registered client id with the OIDC provider. Not a
    secret; safe to expose as part of the (non-secret) authorize-request
    construction, but still not part of what ``get_sso_provider_availability``
    returns to the SPA -- the SPA never constructs the authorize URL itself,
    it only asks "is OIDC available" and is handed a server-built redirect."""

    oidc_client_secret: str | None = None
    """This portal's client secret for the OIDC token-exchange step. SECRET:
    never logged, never echoed back by any endpoint, never included in any
    settings serialization exposed to a route. Used only server-side during
    the authorization-code-for-token exchange."""

    oidc_scopes: tuple[str, ...] = ("openid", "email", "profile")
    """Scopes requested in the authorize request, in the exact order they
    should be joined (space-separated) into the request -- a tuple, not a
    ``frozenset``, specifically so this ordering is preserved rather than
    made arbitrary. ``openid`` is load-bearing (it is what makes this an
    OIDC request rather than a bare OAuth2 one and is what causes the
    provider to return a signed ``id_token`` at all); ``email``/``profile``
    are what typically populate the ``email``/``email_verified``/``name``
    claims this module's identity-linking logic needs."""

    # There is deliberately NO `oidc_require_email_verified` setting here.
    #
    # An earlier draft of this module carried one, documented as the
    # fail-closed gate against binding an SSO identity to a pre-existing local
    # account by email address. It was dead configuration: nothing read it,
    # because the takeover it claimed to prevent is closed structurally instead,
    # and more strongly than a flag could.
    #
    # `sso_service` resolves an identity only by `(provider, subject)`, never by
    # email, and no code path creates a `PortalUser` from an SSO callback at all
    # -- SSO is a login method for accounts that already exist, not a signup
    # path. So an identity provider cannot obtain, squat or take over an account
    # by asserting any email address, verified or not, and there is nothing for
    # such a setting to gate. See `sso_service`'s module docstring, and
    # `tests/test_portal_oauth.py::
    # test_unknown_sso_identity_creates_no_account_and_grants_no_session`, which
    # pins that property so a later "auto-provision on first SSO login"
    # convenience cannot remove it silently.
    #
    # A security setting that does nothing is worse than no setting: an operator
    # reading `oidc_require_email_verified = True` reasonably concludes that
    # email verification is enforced somewhere. It was not. `email_verified` is
    # still recorded faithfully on `VerifiedIdentity` (and is always False for
    # providers that have no such concept), so it is available the moment a
    # feature genuinely needs to branch on it -- at which point the setting can
    # come back together with the code that reads it.

    # --- WeCom (企业微信) -----------------------------------------------------
    #
    # No signed identity assertion exists in this flow at all (unlike OIDC) --
    # trust rests entirely on ``wecom_app_secret``'s confidentiality and on
    # the token-exchange response genuinely being for this app. See
    # ``ALL_SSO_PROVIDERS``'s comment above; whoever implements the adapter
    # must make this non-cryptographic trust basis explicit in its own
    # docstring rather than presenting it as equivalent to OIDC verification.
    #
    # These credentials are for a *separate* WeCom app registration used only
    # for portal web login -- deliberately independent of whatever corp app
    # credentials ``astrbot/core/platform/sources/wecom/`` already holds for
    # *receiving IM messages*. That existing adapter's credentials must never
    # be read, reused, or reconfigured from here: conflating a bot's IM
    # credentials with a web-login app's credentials would mean a compromise
    # of one channel compromises the other, and the two are not necessarily
    # even the same corp app in a real deployment.
    wecom_corp_id: str | None = None
    """The WeCom enterprise's corp id (``CorpID``) for the app used for
    portal web login. Not a secret by itself, but only meaningful paired
    with ``wecom_agent_id``/``wecom_app_secret`` below."""

    wecom_agent_id: str | None = None
    """The WeCom self-built app's numeric AgentId, represented as ``str``
    for consistency with every other id field in this config module (plain
    string settings, no per-field int parsing at the config layer); the
    adapter that actually calls ``wechatpy.enterprise``/``wechatpy.oauth``
    is expected to ``int()``-cast this where the SDK requires a real int."""

    wecom_app_secret: str | None = None
    """The web-login app's secret (``Secret``), used to exchange an OAuth
    code for this app's own access token before calling WeCom's user-info
    API. SECRET: never logged, never echoed back by any endpoint. Must be
    the *login app's* secret, not the IM-receiving bot's secret -- see the
    section comment above."""

    # --- Feishu (飞书) --------------------------------------------------------
    #
    # Same non-cryptographic trust shape as WeCom (see that section's comment
    # above) -- via ``lark_oapi``'s ``authen`` API module. Same separation
    # requirement from ``astrbot/core/platform/sources/lark/``'s existing
    # IM-receiving bot credentials applies identically here.
    feishu_app_id: str | None = None
    """The Feishu app id for the app used for portal web login. Not a
    secret by itself."""

    feishu_app_secret: str | None = None
    """The web-login app's secret, used to exchange an OAuth code for
    identity via ``lark_oapi``'s ``authen`` API. SECRET: never logged, never
    echoed back by any endpoint. Must be a *separate* app registration from
    whatever ``astrbot/core/platform/sources/lark/`` uses for receiving IM
    messages -- see the WeCom section comment above for why conflating the
    two credential sets would be a real defect, not just an untidy
    shortcut."""

    def __post_init__(self) -> None:
        """Fail closed on an unknown ``sso_enabled_providers`` entry at
        settings-construction time (startup), rather than letting a typo
        silently behave as "that provider is disabled" with no signal to
        the operator that anything is wrong."""
        unknown = self.sso_enabled_providers - ALL_SSO_PROVIDERS
        if unknown:
            raise ValueError(
                "PortalSettings.sso_enabled_providers contains unknown "
                f"provider(s) {sorted(unknown)!r}; must be a subset of "
                f"{sorted(ALL_SSO_PROVIDERS)!r}"
            )


DEFAULT_PORTAL_SETTINGS = PortalSettings()


def get_portal_settings() -> PortalSettings:
    """Return the module's settings.

    Phase 1 ships no on-disk override mechanism; this indirection exists so
    a later phase can source overrides (env vars, an admin-configurable
    table) without changing every call site.
    """
    return DEFAULT_PORTAL_SETTINGS


# ---------------------------------------------------------------------------
# SSO provider *availability* -- the one thing safe to expose to the SPA.
# ---------------------------------------------------------------------------


def get_sso_provider_availability(
    settings: PortalSettings | None = None,
) -> dict[str, bool]:
    """Return ``{provider: bool}`` for every entry in ``ALL_SSO_PROVIDERS``,
    and nothing else.

    This is the exact function a downstream route (e.g. ``GET
    /api/v1/portal/auth/sso/providers``) should call and serialize verbatim
    when the SPA needs to know which login buttons to render. It answers
    only "is this provider enabled *and* does it look minimally configured"
    -- it never returns an issuer URL, a client id, a corp id, an agent id,
    or (obviously) any of the three secret fields. A downstream route must
    not substitute a blanket ``dataclasses.asdict(settings)``/``vars(settings)``
    dump for this function "for convenience" -- that would serialize
    ``oidc_client_secret``/``wecom_app_secret``/``feishu_app_secret``
    directly into an HTTP response, which is precisely the "must not be
    logged or echoed back by any endpoint" requirement this phase's
    assignment calls out.

    "Minimally configured" is a presence check only (``bool(value)``), not
    a validity check (e.g. a malformed issuer URL still counts as
    "present" here) -- this function's job is to tell the SPA which login
    buttons make sense to show, not to validate the deployment's SSO
    configuration; a genuinely broken value still fails later, at the
    actual authorize/token-exchange step, the same place it would fail
    without this function existing at all.
    """
    settings = settings or get_portal_settings()

    if not settings.sso_enabled:
        return dict.fromkeys(ALL_SSO_PROVIDERS, False)

    return {
        SSO_PROVIDER_OIDC: (
            SSO_PROVIDER_OIDC in settings.sso_enabled_providers
            and bool(settings.oidc_client_id)
            and bool(settings.oidc_client_secret)
            and bool(settings.oidc_issuer or settings.oidc_discovery_url)
        ),
        SSO_PROVIDER_WECOM: (
            SSO_PROVIDER_WECOM in settings.sso_enabled_providers
            and bool(settings.wecom_corp_id)
            and bool(settings.wecom_agent_id)
            and bool(settings.wecom_app_secret)
        ),
        SSO_PROVIDER_FEISHU: (
            SSO_PROVIDER_FEISHU in settings.sso_enabled_providers
            and bool(settings.feishu_app_id)
            and bool(settings.feishu_app_secret)
        ),
    }


# ---------------------------------------------------------------------------
# JWT secret: lazily generated, persisted via injected SharedPreferences
# accessors (see module docstring for the capability-wall rationale).
# ---------------------------------------------------------------------------

PreferenceGetter = Callable[[str, str, str], Awaitable[str | None]]
"""Matches the call shape of ``SharedPreferences.get_async(scope, scope_id, key)``."""

PreferenceSetter = Callable[[str, str, str, str], Awaitable[None]]
"""Matches the call shape of ``SharedPreferences.put_async(scope, scope_id, key, value)``."""

_secret_generation_lock = asyncio.Lock()


async def get_portal_jwt_secret(
    get_preference: PreferenceGetter,
    put_preference: PreferenceSetter,
) -> str:
    """Lazily fetch-or-generate the portal module's own JWT signing secret.

    ``get_preference``/``put_preference`` must be callables with the exact
    call shape of ``SharedPreferences.get_async``/``put_async`` -- pass the
    bound methods of the process's ``SharedPreferences`` instance directly::

        secret = await get_portal_jwt_secret(sp.get_async, sp.put_async)

    On first call (no existing value under
    ``scope='global', scope_id='portal', key='jwt_secret'``), generates a
    fresh 32-byte cryptographically random urlsafe-base64 string via
    ``secrets.token_urlsafe(32)`` and persists it before returning it.
    Subsequent calls return the persisted value unchanged. An in-process
    lock prevents two concurrent first-callers in the same process from
    generating (and persisting) two different values.
    """
    async with _secret_generation_lock:
        existing = await get_preference(
            PORTAL_JWT_SECRET_SCOPE,
            PORTAL_JWT_SECRET_SCOPE_ID,
            PORTAL_JWT_SECRET_KEY,
        )
        if existing:
            return existing

        new_secret = secrets.token_urlsafe(32)
        await put_preference(
            PORTAL_JWT_SECRET_SCOPE,
            PORTAL_JWT_SECRET_SCOPE_ID,
            PORTAL_JWT_SECRET_KEY,
            new_secret,
        )
        return new_secret
