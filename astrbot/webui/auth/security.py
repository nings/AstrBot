"""Cryptographic primitives for the webui module's independent auth system.

Everything here is deliberately free of I/O and of any import from
``astrbot.dashboard.*`` or the capability-wall-forbidden core services
(rule 5 of this module's engineering rules) -- it only ever touches
``astrbot.core.utils.auth_password`` (explicitly sanctioned reuse for
password hashing, rule 4) and this module's own ``config``/``db.models``.

Covers:

* Access JWT mint/verify (HS256, short-lived, ``{sub, sid, iat, exp,
  typ:'access'}`` payload) -- signed with the webui module's own,
  independently generated secret (never ``config['dashboard']['jwt_secret']``,
  see ``astrbot.webui.config.get_webui_jwt_secret``).
* Opaque 256-bit refresh-token secrets, encoded together with their
  owning ``WebUiSession.session_id`` so the refresh endpoint can look the
  row up by primary key instead of scanning salted PBKDF2 hashes.
* Refresh-token hashing via PBKDF2-SHA256, by reusing
  ``astrbot.core.utils.auth_password.hash_dashboard_password`` /
  ``verify_dashboard_password`` verbatim -- that helper is a generic
  PBKDF2-HMAC-SHA256 KDF with a fresh random salt and a self-describing
  storage format (``pbkdf2_sha256$<iterations>$<salt>$<digest>``); nothing
  about it is password-specific, so it is exactly the "reuse
  auth_password.py helpers where they fit" case called out in this file's
  assignment, rather than a separate hand-rolled ``hashlib.pbkdf2_hmac``
  call. (Password hashing/verification itself also delegates here, per
  rule 4, via the ``hash_password``/``verify_password`` wrappers below.)
* CSRF token generation and constant-time comparison helpers.
* Refresh-token rotation with replay detection, implemented as pure
  decision functions that operate on already-loaded ``WebUiSession`` rows
  (no DB session/engine access lives in this file) -- see
  ``evaluate_refresh_token``, ``rotate_refresh_token`` and
  ``revoke_session_family``.
"""

from __future__ import annotations

import hmac
import secrets
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

import jwt

from astrbot.core.utils.auth_password import (
    hash_dashboard_password,
    validate_dashboard_password,
    verify_dashboard_password,
)
from astrbot.webui.config import get_webui_settings
from astrbot.webui.db.models import WebUiSession

# ---------------------------------------------------------------------------
# Password hashing -- delegates to astrbot.core.utils.auth_password verbatim
# (rule 4: password hashing/verification MUST delegate there, never reuse
# api_key_service.py's fixed-pepper scheme).
# ---------------------------------------------------------------------------


def hash_password(raw_password: str) -> str:
    """Hash a user-chosen password. Delegates to ``auth_password.hash_dashboard_password``."""
    return hash_dashboard_password(raw_password)


def verify_password(stored_hash: str, candidate_password: str) -> bool:
    """Verify a password against its stored hash. Delegates to ``auth_password.verify_dashboard_password``."""
    return verify_dashboard_password(stored_hash, candidate_password)


def validate_password_policy(raw_password: str) -> None:
    """Raise ``ValueError`` if ``raw_password`` fails the complexity policy.

    Delegates to ``auth_password.validate_dashboard_password`` so the webui
    module enforces the exact same length/character-class rules as the
    dashboard, rather than maintaining a second, potentially-drifting policy.
    """
    validate_dashboard_password(raw_password)


# ---------------------------------------------------------------------------
# Access JWT: HS256, short-lived, {sub, sid, iat, exp, typ:'access'}.
# ---------------------------------------------------------------------------

ACCESS_TOKEN_TYPE = "access"


class TokenError(Exception):
    """Base class for all access-token validation failures."""


class TokenExpired(TokenError):
    """The access token's ``exp`` claim is in the past."""


class TokenInvalid(TokenError):
    """The access token is malformed, has a bad signature, or wrong claims."""


@dataclass(frozen=True)
class AccessTokenPayload:
    """Decoded, validated contents of an access JWT."""

    user_id: str
    session_id: str
    issued_at: datetime
    expires_at: datetime


def mint_access_token(
    *,
    user_id: str,
    session_id: str,
    secret: str,
    ttl_seconds: int | None = None,
    now: datetime | None = None,
) -> str:
    """Mint a short-lived access JWT for ``user_id``.

    ``secret`` must be the webui module's own JWT signing secret (see
    ``astrbot.webui.config.get_webui_jwt_secret``) -- this function does not
    fetch or cache it itself, keeping this module free of any
    ``SharedPreferences`` import (capability wall, rule 5).

    ``session_id`` binds the access token to the ``WebUiSession`` it was
    issued alongside (the ``sid`` claim), so a caller can, if needed,
    correlate a live access token back to its refresh-token family without
    a second lookup table.
    """
    settings = get_webui_settings()
    ttl = ttl_seconds if ttl_seconds is not None else settings.access_token_ttl_seconds
    now = now or datetime.now(timezone.utc)
    exp = now + timedelta(seconds=ttl)
    payload = {
        "sub": user_id,
        "sid": session_id,
        "iat": int(now.timestamp()),
        "exp": int(exp.timestamp()),
        "typ": ACCESS_TOKEN_TYPE,
    }
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_access_token(token: str, secret: str) -> AccessTokenPayload:
    """Verify and decode an access JWT, raising ``TokenExpired``/``TokenInvalid`` on failure.

    Never raises any other exception type for malformed input -- callers
    (e.g. ``dependencies.get_current_user``) can catch ``TokenError`` alone
    and turn it into a 401 without needing to know about PyJWT's exception
    hierarchy.
    """
    try:
        payload = jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise TokenExpired("access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise TokenInvalid("access token invalid") from exc

    if payload.get("typ") != ACCESS_TOKEN_TYPE:
        raise TokenInvalid("unexpected token type")

    user_id = payload.get("sub")
    session_id = payload.get("sid")
    iat = payload.get("iat")
    exp = payload.get("exp")
    if not isinstance(user_id, str) or not user_id:
        raise TokenInvalid("missing sub claim")
    if not isinstance(session_id, str) or not session_id:
        raise TokenInvalid("missing sid claim")
    if not isinstance(iat, (int, float)) or not isinstance(exp, (int, float)):
        raise TokenInvalid("missing iat/exp claim")

    return AccessTokenPayload(
        user_id=user_id,
        session_id=session_id,
        issued_at=datetime.fromtimestamp(iat, tz=timezone.utc),
        expires_at=datetime.fromtimestamp(exp, tz=timezone.utc),
    )


# ---------------------------------------------------------------------------
# Opaque refresh-token secrets + PBKDF2 hashing for WebUiSession storage.
# ---------------------------------------------------------------------------

REFRESH_TOKEN_SECRET_BYTES = 32  # 256 bits of entropy.
_REFRESH_TOKEN_DELIMITER = "."


def generate_refresh_secret() -> str:
    """Generate a new opaque, cryptographically random 256-bit refresh-token secret."""
    return secrets.token_urlsafe(REFRESH_TOKEN_SECRET_BYTES)


def hash_refresh_secret(secret: str) -> str:
    """PBKDF2-SHA256 hash of a refresh-token secret, for ``WebUiSession.refresh_token_hash``.

    See module docstring for why this reuses ``hash_dashboard_password``
    rather than a hand-rolled ``hashlib.pbkdf2_hmac`` call.
    """
    return hash_dashboard_password(secret)


def verify_refresh_secret(stored_hash: str, candidate_secret: str) -> bool:
    """Constant-time verification of a presented refresh-token secret against its stored hash."""
    return verify_dashboard_password(stored_hash, candidate_secret)


def encode_refresh_token(session_id: str, secret: str) -> str:
    """Combine a ``WebUiSession.session_id`` (a uuid4, so it never contains
    the delimiter) with its opaque secret into the single string stored in
    the ``astrbot_webui_refresh`` cookie.

    Encoding the session_id alongside the secret lets the refresh endpoint
    look the row up by primary key (O(1)) instead of having to scan every
    live session's PBKDF2 hash -- which, being individually salted, cannot
    be looked up by value at all.
    """
    return f"{session_id}{_REFRESH_TOKEN_DELIMITER}{secret}"


def decode_refresh_token(token: str) -> tuple[str, str] | None:
    """Split a presented refresh-token cookie value back into ``(session_id, secret)``.

    Returns ``None`` for anything malformed (missing delimiter, empty
    halves) rather than raising, so callers can treat "malformed" and
    "not found" uniformly as an authentication failure without a
    try/except.
    """
    if not token or _REFRESH_TOKEN_DELIMITER not in token:
        return None
    session_id, _, secret = token.partition(_REFRESH_TOKEN_DELIMITER)
    if not session_id or not secret:
        return None
    return session_id, secret


# ---------------------------------------------------------------------------
# CSRF double-submit token + constant-time comparison.
# ---------------------------------------------------------------------------


def generate_csrf_token() -> str:
    """Generate a new opaque CSRF token for the non-httpOnly ``astrbot_webui_csrf`` cookie."""
    return secrets.token_urlsafe(32)


def constant_time_compare(a: str | None, b: str | None) -> bool:
    """Timing-safe string equality. Rejects ``None``/empty/non-str inputs outright."""
    if not isinstance(a, str) or not isinstance(b, str) or not a or not b:
        return False
    return hmac.compare_digest(a, b)


# ---------------------------------------------------------------------------
# Refresh-token rotation with replay detection.
#
# Design: rotation creates a *new* WebUiSession row (new session_id, new
# secret/hash) sharing the same family_id, rather than overwriting the old
# row's hash in place, and marks the old row `revoked_at` ("consumed").
# This means a captured-and-replayed old refresh token can still be
# recognized after rotation -- the session_id half still resolves to a
# real row, and that row's `revoked_at` is now set -- even though its
# specific secret/hash no longer validates against anything "current".
# ---------------------------------------------------------------------------


class RefreshOutcome(str, Enum):
    """Result classification for a single presented refresh token."""

    VALID = "valid"
    """Secret matches a live, unrevoked, unexpired row -- safe to rotate."""
    REPLAY = "replay"
    """Row was already revoked/consumed, or the secret does not match a
    live row -- both are treated as a compromise signal (see
    ``evaluate_refresh_token``) and must trigger family-wide revocation."""
    EXPIRED = "expired"
    """Row is live and the secret matches, but ``expires_at`` has passed."""
    NOT_FOUND = "not_found"
    """No row exists for the presented session_id half at all."""


@dataclass(frozen=True)
class RefreshEvaluation:
    """Outcome of evaluating one presented refresh token against its row."""

    outcome: RefreshOutcome
    session: WebUiSession | None
    family_id: str | None


def evaluate_refresh_token(
    session_row: WebUiSession | None,
    candidate_secret: str,
    *,
    now: datetime | None = None,
) -> RefreshEvaluation:
    """Pure decision function for a presented ``(session_id, secret)`` refresh pair.

    ``session_row`` must be the ``WebUiSession`` the caller already looked
    up by the session_id half of ``decode_refresh_token``'s result -- this
    function performs no DB I/O itself; the actual fetch (and, on
    ``REPLAY``, the family-wide fetch/commit via ``revoke_session_family``)
    is the caller's responsibility (``session_service.py``), keeping this
    file's logic testable with plain in-memory fakes.

    Deliberately does **not** try to distinguish "attacker guessed a wrong
    secret for a still-live session_id" from "this exact secret was
    genuinely issued but already rotated away" -- both collapse to
    ``REPLAY``. Trying to tell them apart would require retaining every
    previously-issued hash forever; treating both as a compromise signal
    and revoking the whole family is the fail-safe choice the plan's
    replay-detection checklist item (section 4, #7) calls for.
    """
    now = now or datetime.now(timezone.utc)
    if session_row is None:
        return RefreshEvaluation(RefreshOutcome.NOT_FOUND, None, None)

    already_revoked = session_row.revoked_at is not None
    secret_matches = verify_refresh_secret(
        session_row.refresh_token_hash, candidate_secret
    )

    if already_revoked or not secret_matches:
        return RefreshEvaluation(
            RefreshOutcome.REPLAY, session_row, session_row.family_id
        )

    if session_row.expires_at <= now:
        return RefreshEvaluation(
            RefreshOutcome.EXPIRED, session_row, session_row.family_id
        )

    return RefreshEvaluation(RefreshOutcome.VALID, session_row, session_row.family_id)


def revoke_session_family(
    family_sessions: Iterable[WebUiSession],
    *,
    now: datetime | None = None,
) -> list[WebUiSession]:
    """Mark every not-yet-revoked row in ``family_sessions`` as revoked.

    Mutates the passed-in ``WebUiSession`` ORM instances in place (the
    caller is expected to have loaded them from the DB session it will
    subsequently commit -- this mirrors how SQLAlchemy/SQLModel rows are
    normally mutated-then-committed elsewhere in this codebase) and returns
    only the subset that was actually changed, so the caller can log/count
    exactly how many sessions a replay event tore down.

    ``family_sessions`` should be every ``WebUiSession`` row sharing the
    ``family_id`` returned by ``evaluate_refresh_token`` -- fetching that
    set is the caller's job (a simple ``WHERE family_id = ...`` query).
    """
    now = now or datetime.now(timezone.utc)
    changed: list[WebUiSession] = []
    for row in family_sessions:
        if row.revoked_at is None:
            row.revoked_at = now
            changed.append(row)
    return changed


def rotate_refresh_token(
    old_session: WebUiSession,
    *,
    now: datetime | None = None,
) -> tuple[str, str]:
    """Consume ``old_session`` (marks it revoked) and return a fresh ``(raw_secret, secret_hash)`` pair.

    The caller must persist ``secret_hash`` onto a **new** ``WebUiSession``
    row that shares ``old_session``'s ``family_id`` and ``user_id`` (so the
    family can later be revoked as a whole), and must send the caller's
    browser a cookie built from ``encode_refresh_token(new_row.session_id,
    raw_secret)`` -- ``raw_secret`` itself is never persisted, only its hash.

    Only call this after ``evaluate_refresh_token`` has returned
    ``RefreshOutcome.VALID`` for ``old_session``.
    """
    now = now or datetime.now(timezone.utc)
    old_session.revoked_at = now
    raw_secret = generate_refresh_secret()
    secret_hash = hash_refresh_secret(raw_secret)
    return raw_secret, secret_hash
