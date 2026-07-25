"""Portal-side operator-provisioned account creation: admin-issued invites
and their redemption (Phase 5a).

This file does **not** import ``ChatService``/``conv_mgr``/``sp``/
``persona_mgr``/``ProviderManager``/``kb_mgr``, nor anything from
``astrbot.dashboard.*`` -- unlike ``kb_visibility_service.py``/
``agent_bundle_service.py``, this module has no core touchpoint at all (an
invite only ever creates/activates rows in this module's own
``portal_users``/``portal_invites`` tables and, on successful redemption,
hands off to ``session_service.issue_token_pair`` for the actual token
mint), so there is nothing here that needs ``core_facade`` or a FastAPI
``Request`` in the first place. It reuses, rather than re-implements:

* ``astrbot.portal.auth.security`` for the token secret/hash primitives
  (``generate_refresh_secret``/``hash_refresh_secret``/
  ``verify_refresh_secret`` -- the exact same three functions
  ``PortalSession``'s refresh tokens use) and for password handling
  (``hash_password``/``validate_password_policy``, themselves thin wrappers
  over ``astrbot.core.utils.auth_password``).
* ``astrbot.portal.auth.rate_limit.get_login_throttle`` for redemption
  throttling (see "Rate limiting" below).
* ``astrbot.portal.services.session_service.issue_token_pair`` for the
  token pair a successful redemption returns -- this module never mints a
  JWT or a refresh-token hash by hand.

There is no mail infrastructure anywhere in this repository (no
``smtplib``/``aiosmtplib`` usage exists). :func:`admin_create_invite`
therefore returns the plaintext one-time link **exactly once**, at the
moment of creation, and never again -- it is the operator's job to copy and
deliver that value out-of-band (chat, a ticket, however they reach the
invitee). Every other function in this module that reads back a
``PortalInvite`` row (:func:`admin_list_invites`, the ``_admin_entry``
shaping helper) exposes its ``invite_id``, timestamps, and computed
``status`` -- never the secret, and never anything that could reconstruct
it (only ``token_hash`` is stored, and that is never serialized out of this
module at all).

Verification order -- why an unknown ``invite_id`` and a wrong secret for a
real one must look identical
-----------------------------------------------------------------------
:func:`redeem_invite` looks up the presented ``(invite_id, secret)`` pair by
primary key first, then verifies the secret against that row's
``token_hash``. If the row does not exist *or* the secret fails to verify,
it raises the exact same :class:`InviteInvalidError` either way. This is
not an accident of implementation convenience: if a missing ``invite_id``
produced a different response (say, 404) than an existing ``invite_id``
with a wrong secret (say, 401), the ``invite_id`` namespace itself -- a
36-character uuid4, but still a namespace an attacker could enumerate --
would become probeable. A stranger with no valid link must learn nothing
from the response beyond "that did not work"; only once
``verify_refresh_secret`` has actually succeeded against a real row's hash
(i.e., only for someone who already possesses the correct, unguessable
secret, delivered to them out-of-band by the operator) does this module
consider it safe to say anything more specific.

Once the secret verifies, distinguishing consumed vs. revoked vs. expired
*is* safe
-----------------------------------------------------------------------
After secret verification succeeds, :func:`redeem_invite` raises three
distinct exceptions for the three ways a genuine invite can no longer be
redeemable: :class:`InviteAlreadyConsumedError`, :class:`InviteRevokedError`,
:class:`InviteExpiredError`. This is deliberately more informative than the
pre-verification branch, and that asymmetry is the whole point: reaching
this branch already proves the caller holds the correct secret for this
``invite_id``, which nobody could have without either being the intended
invitee or having compromised the out-of-band delivery channel itself (at
which point the invite's state is the least of the problem). Telling a
holder of the correct link "this was already used" vs. "this was revoked"
vs. "this expired" leaks nothing to a stranger still guessing secrets --
they never reach this branch at all -- while giving a legitimate invitee
(or the operator debugging a support ticket) a genuinely useful answer
instead of one deliberately-vague "invalid" for every non-oracle-relevant
case. Per the assignment's own acceptance criterion, a replayed
(already-consumed) token is mapped to HTTP 410 Gone by the recommended
route mapping below; this module recommends 410 for revoked and expired
too (see each exception class's docstring) -- all three describe a
resource that used to be valid at this exact URL and no longer is, which is
precisely what 410 means, and using the same status for all three keeps a
legitimate invitee's client-side handling simple ("show the message body,
offer to contact the operator") without needing three different code
branches for what is, from the invitee's point of view, one experience:
"this link doesn't work anymore".

Recommended HTTP status mapping (decided here since this module owns the
exception vocabulary; the route layer, not owned by this file, should
follow it verbatim so it stays a single source of truth):

* :class:`InviteRateLimitedError` -> 429
* :class:`InviteInvalidError` -> 401 (generic; identical for "no such
  invite" and "wrong secret for a real one", see above)
* :class:`InviteAlreadyConsumedError` / :class:`InviteRevokedError` /
  :class:`InviteExpiredError` -> 410
* :class:`InviteEmailMismatchError` -> 409 (an operational integrity
  failure -- see "Email binding" below -- not an attacker-guessable path,
  since it is only reachable after secret verification)
* :class:`InvitePasswordPolicyError` -> 400
* :class:`InviteRedemptionConflictError` -> 409 (lost a concurrency race in
  a way that did not cleanly resolve to one of the three states above;
  should be rare -- see "Concurrency" below -- and is safe to retry)
* :class:`InviteNotFoundError` / :class:`InviteEmailAlreadyRegisteredError`
  (admin-only functions, reached only behind ``require_system_scope``) ->
  404 / 409 respectively. These two are NOT held to the same
  no-existence-oracle standard as the redemption path: the caller is
  already an authenticated operator (gated at the route layer, this module
  trusts that gate completely and performs no authorization check of its
  own), so there is no stranger to hide an "exists"/"does not exist"
  distinction from.

Concurrency: single-use is a database guarantee, not an application-level
check-then-act
-----------------------------------------------------------------------
"Read ``consumed_at``, see it is ``None``, then write it" is a classic
lost-update race: two concurrent redemptions of the same token can both
read ``None`` before either writes, and both would then believe they own
the redemption. :func:`redeem_invite` closes this exactly the way
``attachment_service.py``'s upload-quota reservation closes the analogous
race for ``PortalUsageCounter.uploaded_bytes`` -- by folding the
precondition into a conditional ``UPDATE``'s ``WHERE`` clause and checking
``rowcount`` rather than trusting a prior ``SELECT``:

.. code-block:: sql

    UPDATE portal_invites
    SET consumed_at = :now
    WHERE invite_id = :invite_id
      AND consumed_at IS NULL
      AND revoked_at IS NULL

Only the request whose ``UPDATE`` actually flips a row (``rowcount == 1``)
may proceed to activate the account; SQLite serializes writers, so the
loser's statement either matches zero rows (the winner's write already
landed) or the loser's own read of a still-live row was already up to date
-- there is no interleaving in which two concurrent callers both observe
``rowcount == 1``. The very same shape is applied a **second** time,
independently, against ``PortalUser.status`` (``WHERE user_id = :id AND
status = 'invited'``), for a related but distinct race: a pre-created
``PortalUser`` could in principle have more than one outstanding
``PortalInvite`` row (this table intentionally has no one-row-per-user
uniqueness constraint -- see ``PortalInvite``'s own docstring on the
reissue path), so "this exact token was not replayed" alone does not imply
"this account has not already been activated by a *different*,
concurrently-redeemed invite for the same user". Both conditional
``UPDATE``\\ s run inside the same, still-open database transaction and are
only ever committed together (see "One atomic transaction" below); if
either reports ``rowcount != 1`` the whole transaction is rolled back
before any exception is raised, so a losing request never leaves a partial
write behind.

``expires_at`` is deliberately **not** folded into either conditional
``UPDATE``'s ``WHERE`` clause, unlike ``consumed_at``/``revoked_at``/
``status``. Those three are exact-match/``IS NULL`` conditions with no
representation ambiguity in SQLite; a ``> :now`` inequality against a
``DATETIME`` column, however, has no established precedent anywhere else
in this codebase for round-tripping a timezone-aware Python
``datetime`` through SQLite's string-based date storage inside a raw SQL
comparison, and getting that silently wrong (a naive/aware mismatch, or a
storage-format quirk) would either always allow or always block
redemption -- a far worse failure mode than the one it would guard
against. The property this module actually needs to guarantee atomically
is single-use (the assignment's literal acceptance criterion: "a replayed
token returns 410"), which the ``consumed_at``/``revoked_at``/``status``
conditions above already deliver in full; expiry is instead checked
Python-side, immediately before these conditional updates, against the
freshly-read row. The residual window this leaves -- a request that began
a handful of milliseconds before ``expires_at`` and reaches the atomic
update a handful of milliseconds after -- is an inherent property of any
TTL check that does not hold a lock for the entire request lifetime, is
not a replay/double-activation, and is explicitly accepted here as a
non-issue rather than an oversight.

One atomic transaction: ``consumed_at`` + ``PortalUser`` activation, never
two commits
-----------------------------------------------------------------------
Per ``PortalInvite``'s own docstring, the entire redemption -- both
conditional ``UPDATE``\\ s above, plus setting the activated
``PortalUser.password_hash``/``status`` -- happens against ONE
``AsyncSession`` and is committed exactly once. If any precondition check
fails partway through, the session is rolled back and nothing is
persisted; there is no code path that could leave the invite consumed but
the user still ``status='invited'`` (or the reverse).

Email binding: the invitee never gets to change the email they were
invited to
-----------------------------------------------------------------------
``PortalInvite.target_email`` is a snapshot of ``PortalUser.email`` at
issuance time. Before honoring any redemption, this module re-reads the
referenced ``PortalUser``'s *current* email and refuses (fail closed,
:class:`InviteEmailMismatchError`) unless it still equals ``target_email``
exactly. Nothing in :func:`redeem_invite`'s parameters accepts a
client-supplied email at all -- the redemption request can only ever set a
password (and, optionally, a display name) for the account the operator
already named when they created the invite. Allowing an email change
during redemption would turn a leaked or guessed invite link into a way to
redirect an account to an attacker-controlled address; this is the same
family of concern rule 5 raises for SSO email-based account linking, and
this module resolves it the same way: never let identity binding change
hands on an unverified/ambient signal.

Rate limiting lives in the service layer itself, not just the route
-----------------------------------------------------------------------
:func:`redeem_invite` calls ``astrbot.portal.auth.rate_limit.
get_login_throttle()`` -- the exact same process-wide throttle instance
``auth_routes.py`` already uses for ``/register``/``/login``/``/refresh``
-- directly, keyed by ``(ip, invite_id)``, exactly like login's
``(ip, email)`` keying. This is deliberately NOT left to the route layer to
remember to call: this module's own assignment states plainly that
redemption is "an unauthenticated, credential-accepting endpoint's service
layer" and must be throttled "like login is", and since this file cannot
control what the (not-owned-by-this-file) route does, baking the check
into the service function itself is the only way to guarantee it holds
regardless of how the route is wired. A route MAY still perform its own
early ``throttle.check`` for a cheaper pre-DB rejection (mirroring
``auth_routes.py``'s existing login/register shape) but must not do so
against the *same* ``(ip, invite_id)`` key a second time immediately before
calling this function, or it would burn two tokens per attempt instead of
one; if the route wants an early check, it should use ``account=None`` (IP
only) and let this function's own check cover the account dimension.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.auth.rate_limit import apply_failure_delay, get_login_throttle
from astrbot.portal.auth.security import (
    generate_refresh_secret,
    hash_password,
    hash_refresh_secret,
    validate_password_policy,
    verify_refresh_secret,
)
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalInvite, PortalUser
from astrbot.portal.services import session_service

# ---------------------------------------------------------------------------
# Exceptions. See module docstring's "Recommended HTTP status mapping"
# section for how each of these should be translated by the (not-owned-by-
# this-file) route layer.
# ---------------------------------------------------------------------------


class InviteServiceError(Exception):
    """Base class for all errors raised by this module."""


class InviteRateLimitedError(InviteServiceError):
    """The ``(ip, invite_id)`` throttle rejected this attempt before any
    token verification took place. Recommended: 429."""


class InviteInvalidError(InviteServiceError):
    """The presented token is malformed, or does not verify against any
    real, existing ``PortalInvite`` row.

    Deliberately the SAME exception for "no row exists for this
    ``invite_id``" and "a row exists but the secret does not match its
    ``token_hash``" -- see module docstring's "Verification order" section
    for why collapsing these two is the entire point. Recommended: 401.
    """


class InviteAlreadyConsumedError(InviteServiceError):
    """The secret verified, but this invite (or the account it targets) was
    already activated -- either via this exact token, or via a *different*
    still-outstanding invite issued against the same pre-created
    ``PortalUser`` (see module docstring's "Concurrency" section for why
    both cases collapse to this one exception). Recommended: 410 (the
    assignment's literal acceptance criterion for a replayed token)."""


class InviteRevokedError(InviteServiceError):
    """The secret verified, but an operator revoked this invite before it
    was redeemed. Recommended: 410 (see module docstring for why this
    shares a status with the other two "used to be valid, no longer is"
    cases)."""


class InviteExpiredError(InviteServiceError):
    """The secret verified, but ``expires_at`` has passed. Recommended:
    410."""


class InviteEmailMismatchError(InviteServiceError):
    """The secret verified and the invite is otherwise live, but the
    referenced ``PortalUser``'s current email no longer equals
    ``PortalInvite.target_email`` -- see module docstring's "Email
    binding" section. Fail-closed; redemption is refused and the invite is
    NOT consumed (an operator can fix the drift and the same link becomes
    redeemable again). Recommended: 409."""


class InvitePasswordPolicyError(InviteServiceError):
    """The candidate password fails ``validate_password_policy``.
    Recommended: 400."""


class InviteRedemptionConflictError(InviteServiceError):
    """A conditional ``UPDATE`` lost a concurrency race in a way that, on
    re-read, did not cleanly resolve to consumed/revoked/expired (should be
    rare in practice -- see module docstring's "Concurrency" section).
    Safe to retry. Recommended: 409."""


class InviteNotFoundError(InviteServiceError):
    """(Admin-only, e.g. :func:`admin_revoke_invite`.) No ``PortalInvite``
    row exists for the given ``invite_id``. Unlike every exception above,
    this IS a plain existence signal -- the caller is already an
    authenticated operator (``require_system_scope``, enforced at the
    route layer, not by this module), so there is no stranger this could
    leak anything to. Recommended: 404."""


class InviteEmailAlreadyRegisteredError(InviteServiceError):
    """(Admin-only, :func:`admin_create_invite`.) The target email already
    belongs to a ``PortalUser`` whose ``status`` is not ``'invited'``
    (i.e. already ``'active'`` or ``'disabled'``), or a concurrent insert
    won an email-uniqueness race. Recommended: 409."""


# ---------------------------------------------------------------------------
# Shared helpers.
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_INVITE_TOKEN_SEPARATOR = "."


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime read back from SQLite to tz-aware UTC.

    Duplicated from ``session_service._as_utc`` (module-private there, and
    an eight-line function) rather than imported -- every value this
    module writes is already UTC (:func:`_utcnow`), so a naive value read
    back is safe to re-attach ``timezone.utc`` to before comparing against
    a freshly generated ``_utcnow()``.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_email(email: str) -> str:
    normalized = (email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise InviteServiceError("Invalid email address")
    return normalized


def _split_invite_token(token: str) -> tuple[str, str] | None:
    """Split a presented invite token into ``(invite_id, secret)``.

    Mirrors ``astrbot.portal.auth.security.decode_refresh_token``'s exact
    shape (same ``.`` delimiter convention) -- duplicated rather than
    imported since invites are a conceptually distinct token from a
    refresh token even though the encoding is identical; see
    ``PortalInvite``'s own docstring for why this reuse-the-shape-not-the-
    name choice was made. Returns ``None`` for anything malformed (missing
    delimiter, empty halves) rather than raising, so the caller can fold
    "malformed" and "not found" into the same generic outcome.
    """
    if not token or _INVITE_TOKEN_SEPARATOR not in token:
        return None
    invite_id, _, secret = token.partition(_INVITE_TOKEN_SEPARATOR)
    if not invite_id or not secret:
        return None
    return invite_id, secret


def _invite_status(row: PortalInvite, now: datetime) -> str:
    """Compute the operator-facing status of one invite row.

    One of ``"consumed"`` | ``"revoked"`` | ``"expired"`` | ``"pending"``.
    Checked in this exact order so a consumed-then-somehow-also-revoked row
    (should never happen, but costs nothing to order defensively) reports
    the more final state.
    """
    if row.consumed_at is not None:
        return "consumed"
    if row.revoked_at is not None:
        return "revoked"
    if _as_utc(row.expires_at) <= now:
        return "expired"
    return "pending"


def _admin_entry(row: PortalInvite) -> dict[str, Any]:
    """Shape one ``PortalInvite`` row for the operator's invite-management
    page. NEVER includes the plaintext token or ``token_hash`` -- the
    plaintext is returned exactly once, by :func:`admin_create_invite`
    alone, at creation time; every other read of an invite row (including
    this one) exposes only bookkeeping."""
    now = _utcnow()
    return {
        "invite_id": row.invite_id,
        "user_id": row.user_id,
        "target_email": row.target_email,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "expires_at": row.expires_at.isoformat(),
        "consumed_at": row.consumed_at.isoformat() if row.consumed_at else None,
        "revoked_at": row.revoked_at.isoformat() if row.revoked_at else None,
        "revoked_by": row.revoked_by,
        "status": _invite_status(row, now),
    }


# ---------------------------------------------------------------------------
# Admin-side CRUD. Operator-only: every function below trusts its caller
# (the admin routes, gated by ``require_system_scope`` imported verbatim
# from ``astrbot.dashboard.api.auth`` -- see this phase's engineering
# rules) to have already authenticated a real operator. None of these
# functions perform any authorization check of their own -- mirrors
# ``kb_visibility_service.py``/``agent_bundle_service.py``'s own
# admin-side sections exactly.
# ---------------------------------------------------------------------------


async def admin_create_invite(
    *,
    email: str,
    created_by: str,
    display_name: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Issue a one-time invite for ``email``, returning ``(admin_entry,
    plaintext_token)``.

    **The second element of the returned tuple is the ONLY time the
    plaintext token is ever available.** From this call onward, only its
    PBKDF2 hash exists anywhere (``PortalInvite.token_hash``); there is no
    way to recover it, by this module or anyone else, ever again. There is
    no mail infrastructure in this repository -- the caller (the admin
    route, not owned by this file) MUST surface this value to the operator
    and make explicit in the UI that they are responsible for delivering
    it out-of-band (copy/paste into chat, a ticket, however they reach the
    invitee); this function does not, and cannot, send anything itself.

    If ``email`` already belongs to a ``PortalUser`` whose ``status`` is
    ``'invited'`` (a previous invite for this address is still outstanding,
    unredeemed), this is a **reissue**: no second ``PortalUser`` row is
    created (that would fail on the unique ``email`` constraint anyway);
    instead a fresh ``PortalInvite`` row is inserted against the *same*
    ``user_id``, with its own fresh secret/expiry. The old invite (if any)
    is left exactly as it was -- both remain independently redeemable
    (whichever is presented first wins; see module docstring's
    "Concurrency" section for why a second, still-outstanding invite for an
    already-activated user is refused at redemption time rather than
    needing to be proactively revoked here).

    If ``email`` belongs to a ``PortalUser`` whose ``status`` is
    ``'active'`` or ``'disabled'``, raises
    :class:`InviteEmailAlreadyRegisteredError` -- this function never
    re-provisions an account that is not currently in the ``'invited'``
    resting state (see ``PortalUser.status``'s documented vocabulary).

    Otherwise, a brand new ``PortalUser`` row is pre-created
    (``status='invited'``, ``auth_method='invited'``, ``password_hash=
    None``) per ``PortalInvite``'s own docstring's design rationale
    (claims email uniqueness immediately; gives this invite a real
    ``user_id`` to reference). ``display_name`` is required to be
    non-``None`` on ``PortalUser`` (no column default) -- if the caller
    does not supply one, it is derived from the email's local-part,
    exactly matching ``session_service.create_user``'s own fallback.
    """
    normalized_email = _normalize_email(email)
    settings = get_portal_settings()
    now = _utcnow()
    expires_at = now + timedelta(seconds=settings.invite_token_ttl_seconds)

    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalUser).where(col(PortalUser.email) == normalized_email)
        )
        user = result.scalar_one_or_none()

        if user is not None and user.status != "invited":
            raise InviteEmailAlreadyRegisteredError(
                f"{normalized_email!r} is already registered to an account "
                f"that is not awaiting invite acceptance"
            )

        if user is None:
            user = PortalUser(
                email=normalized_email,
                password_hash=None,
                display_name=(display_name or "").strip()
                or normalized_email.split("@")[0],
                auth_method="invited",
                role="user",
                status="invited",
            )
            session.add(user)
            try:
                await session.flush()
            except IntegrityError as exc:
                await session.rollback()
                raise InviteEmailAlreadyRegisteredError(
                    f"{normalized_email!r} was just registered concurrently; retry"
                ) from exc

        secret = generate_refresh_secret()
        invite = PortalInvite(
            token_hash=hash_refresh_secret(secret),
            user_id=user.user_id,
            target_email=normalized_email,
            created_by=created_by,
            expires_at=expires_at,
        )
        session.add(invite)
        await session.commit()
        await session.refresh(invite)

    plaintext_token = f"{invite.invite_id}{_INVITE_TOKEN_SEPARATOR}{secret}"
    logger.info(
        f"[portal] admin {created_by!r} issued invite {invite.invite_id} "
        f"for {normalized_email!r} (user {user.user_id})"
    )
    return _admin_entry(invite), plaintext_token


async def admin_list_invites() -> list[dict[str, Any]]:
    """Every ``PortalInvite`` row, most recently created first, each
    joined with its computed ``status`` (:func:`_invite_status`). Never
    includes a plaintext token -- see :func:`_admin_entry`."""
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalInvite).order_by(col(PortalInvite.created_at).desc())
        )
        rows = list(result.scalars().all())
    return [_admin_entry(row) for row in rows]


async def admin_revoke_invite(invite_id: str, *, revoked_by: str) -> dict[str, Any]:
    """Cancel an outstanding (not yet redeemed) invite.

    Raises:
        InviteNotFoundError: no such ``invite_id``.
        InviteAlreadyConsumedError: the invite was already redeemed --
            revocation only ever applies to an invite nobody has used yet;
            an already-activated account is not something "revoking the
            invite" could meaningfully undo (that is a separate account-
            disable operation, out of this module's scope).

    Idempotent for an already-revoked invite: calling this again returns
    the existing row unchanged (``revoked_at``/``revoked_by`` are NOT
    overwritten a second time), preserving the original revocation's audit
    trail rather than attributing it to whoever happened to call this
    twice -- mirrors ``PortalKbVisibility.enabled``'s "flag rather than
    delete, preserve history" convention used throughout this module's
    sibling services.
    """
    async with portal_async_sessionmaker() as session:
        row = await session.get(PortalInvite, invite_id)
        if row is None:
            raise InviteNotFoundError("Invite not found")
        if row.consumed_at is not None:
            raise InviteAlreadyConsumedError(
                "Cannot revoke an invite that has already been redeemed"
            )
        if row.revoked_at is None:
            row.revoked_at = _utcnow()
            row.revoked_by = revoked_by
            session.add(row)
            await session.commit()
            await session.refresh(row)

    logger.info(f"[portal] admin {revoked_by!r} revoked invite {invite_id}")
    return _admin_entry(row)


# ---------------------------------------------------------------------------
# End-user-facing: unauthenticated redemption.
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class InviteRedemptionResult:
    """Everything a route needs to finish an invite-acceptance response --
    deliberately the same shape a login/register route already has on hand
    (compare ``auth_routes.py``'s ``user``/``token_pair`` pair after a
    successful ``session_service.authenticate_user``/``issue_token_pair``
    call), so redemption slots into the exact same
    "build user payload + set auth cookies" call site with no new shape to
    special-case."""

    user_id: str
    email: str
    display_name: str
    token_pair: session_service.TokenPair


async def redeem_invite(
    *,
    token: str,
    password: str,
    display_name: str | None = None,
    ip: str | None = None,
    user_agent: str | None = None,
) -> InviteRedemptionResult:
    """Redeem a one-time invite link: verify it, activate its pre-created
    ``PortalUser``, and issue a normal token pair via
    ``session_service.issue_token_pair``.

    See the module docstring in full for the security reasoning behind
    every step below; in order:

    1. **Rate limiting** (``get_login_throttle().check(ip=ip,
       account=invite_id)``) -- before any DB lookup. ``InviteRateLimitedError``
       if exhausted.
    2. **Parse** the token into ``(invite_id, secret)``.
       ``InviteInvalidError`` if malformed.
    3. **Look up + verify secret.** ``InviteInvalidError`` -- identical
       response -- whether ``invite_id`` does not exist or the secret does
       not match. A failure here records a throttle failure and applies
       the fixed post-failure delay (mirrors
       ``session_service.authenticate_user``'s login-failure handling).
    4. **State checks**, now that the secret has proven genuine:
       ``consumed_at`` -> :class:`InviteAlreadyConsumedError`;
       ``revoked_at`` -> :class:`InviteRevokedError`; ``expires_at`` in the
       past -> :class:`InviteExpiredError`.
    5. **Password policy** (``validate_password_policy``) --
       :class:`InvitePasswordPolicyError` if it fails. Checked before
       touching the referenced ``PortalUser`` so a merely-weak password
       never burns the invite.
    6. **Email binding** -- the referenced ``PortalUser``'s *current*
       email must still equal ``PortalInvite.target_email``.
       :class:`InviteEmailMismatchError` (fail closed) otherwise; the
       invite is NOT consumed on this path, so a fixed drift can be
       corrected and the same link retried.
    7. **Atomic single-use activation** -- two conditional ``UPDATE``\\ s
       (invite ``consumed_at``, then ``PortalUser.status``/
       ``password_hash``) in the same transaction; either both succeed and
       the transaction commits, or neither does. See module docstring's
       "Concurrency" section for the exact guarantee and why each
       ``UPDATE`` is only "conditional" (its own ``WHERE`` clause, not a
       prior ``SELECT``) that decides success. A lost race here reports
       :class:`InviteAlreadyConsumedError` (or, in the rare case the
       re-read state does not cleanly resolve, the generic
       :class:`InviteRedemptionConflictError`).
    8. **Token issuance** -- only after step 7's transaction has committed,
       ``session_service.issue_token_pair`` mints the actual access/refresh
       pair, in its own separate transaction. This is intentionally NOT
       folded into step 7's transaction: token issuance and account
       activation are different concerns (mirrors how ``session_service.
       authenticate_user`` and ``issue_token_pair`` are already two
       separate calls in ``auth_routes.py``'s login route), and if it were
       to fail after step 7 already committed, the invitee's account is
       simply active with a password already set -- recoverable by a plain
       login, not a corrupted state.

    ``display_name``, if provided (and non-blank after stripping), replaces
    the pre-created ``PortalUser.display_name`` at redemption time -- the
    admin-supplied/email-derived value used at invite-creation time was
    only ever a placeholder the invitee is free to personalize. This is
    NOT true of ``email`` -- no parameter here accepts one; see module
    docstring's "Email binding" section for why that omission is
    deliberate, not incomplete.
    """
    throttle = get_login_throttle()
    parsed = _split_invite_token(token)
    account_key = parsed[0] if parsed is not None else None

    decision = await throttle.check(ip=ip, account=account_key)
    if not decision.allowed:
        raise InviteRateLimitedError("Too many attempts, please try again later")

    if parsed is None:
        throttle.record_failure(ip=ip, account=None)
        await apply_failure_delay()
        raise InviteInvalidError("Invalid or unrecognized invite token")

    invite_id, secret = parsed

    async with portal_async_sessionmaker() as session:
        row = await session.get(PortalInvite, invite_id)
        if row is None or not verify_refresh_secret(row.token_hash, secret):
            throttle.record_failure(ip=ip, account=invite_id)
            await apply_failure_delay()
            raise InviteInvalidError("Invalid or unrecognized invite token")

        # From here on the secret has verified against a real row -- safe
        # to distinguish further states (module docstring).
        now = _utcnow()
        if row.consumed_at is not None:
            raise InviteAlreadyConsumedError("This invitation has already been used")
        if row.revoked_at is not None:
            raise InviteRevokedError("This invitation has been revoked")
        if _as_utc(row.expires_at) <= now:
            raise InviteExpiredError("This invitation has expired")

        try:
            validate_password_policy(password)
        except ValueError as exc:
            raise InvitePasswordPolicyError(str(exc)) from exc

        user = await session.get(PortalUser, row.user_id)
        if user is None:
            # Should be impossible (PortalInvite.user_id is a real FK into
            # this same database), but fail closed and generic rather than
            # ever surfacing an internal-consistency error to an
            # unauthenticated caller.
            logger.error(
                f"[portal] invite {invite_id} references missing user "
                f"{row.user_id}; refusing redemption"
            )
            raise InviteInvalidError("Invalid or unrecognized invite token")

        if user.email != row.target_email:
            logger.warning(
                f"[portal] invite {invite_id} email binding mismatch: "
                f"user email {user.email!r} != invite target "
                f"{row.target_email!r}; refusing redemption"
            )
            raise InviteEmailMismatchError(
                "This invitation is no longer valid for this account"
            )

        # Single-use guarantee, part 1: this exact invite row. See module
        # docstring's "Concurrency" section for why `expires_at` is
        # deliberately NOT part of this WHERE clause (already checked
        # Python-side above).
        invite_update = await session.execute(
            update(PortalInvite)
            .where(
                col(PortalInvite.invite_id) == invite_id,
                col(PortalInvite.consumed_at).is_(None),
                col(PortalInvite.revoked_at).is_(None),
            )
            .values(consumed_at=now)
            .execution_options(synchronize_session=False)
        )
        if invite_update.rowcount != 1:
            await session.rollback()
            refreshed = await session.get(PortalInvite, invite_id)
            if refreshed is not None and refreshed.consumed_at is not None:
                raise InviteAlreadyConsumedError(
                    "This invitation has already been used"
                )
            if refreshed is not None and refreshed.revoked_at is not None:
                raise InviteRevokedError("This invitation has been revoked")
            raise InviteRedemptionConflictError(
                "This invitation could not be redeemed right now; please retry"
            )

        # Single-use guarantee, part 2: the target account itself, in case
        # a *different* still-outstanding invite for the same user_id won
        # a concurrent redemption first (module docstring's "Concurrency"
        # section). Both this and the UPDATE above are in the same
        # not-yet-committed transaction -- a failure here rolls back the
        # invite consumption too.
        user_values: dict[str, Any] = {
            "password_hash": hash_password(password),
            "status": "active",
        }
        if display_name is not None:
            stripped_name = display_name.strip()
            if stripped_name:
                user_values["display_name"] = stripped_name

        user_update = await session.execute(
            update(PortalUser)
            .where(
                col(PortalUser.user_id) == user.user_id,
                col(PortalUser.status) == "invited",
            )
            .values(**user_values)
            .execution_options(synchronize_session=False)
        )
        if user_update.rowcount != 1:
            await session.rollback()
            raise InviteAlreadyConsumedError("This invitation has already been used")

        await session.commit()

        activated_user_id = user.user_id
        activated_email = user.email
        activated_display_name = user_values.get("display_name", user.display_name)

    throttle.record_success(ip=ip, account=invite_id)
    logger.info(
        f"[portal] invite {invite_id} redeemed; activated user {activated_user_id}"
    )

    async with portal_async_sessionmaker() as token_session:
        token_pair = await session_service.issue_token_pair(
            token_session,
            user_id=activated_user_id,
            user_agent=user_agent,
            ip=ip,
        )

    return InviteRedemptionResult(
        user_id=activated_user_id,
        email=activated_email,
        display_name=activated_display_name,
        token_pair=token_pair,
    )


__all__ = [
    "InviteServiceError",
    "InviteRateLimitedError",
    "InviteInvalidError",
    "InviteAlreadyConsumedError",
    "InviteRevokedError",
    "InviteExpiredError",
    "InviteEmailMismatchError",
    "InvitePasswordPolicyError",
    "InviteRedemptionConflictError",
    "InviteNotFoundError",
    "InviteEmailAlreadyRegisteredError",
    "InviteRedemptionResult",
    "admin_create_invite",
    "admin_list_invites",
    "admin_revoke_invite",
    "redeem_invite",
]
