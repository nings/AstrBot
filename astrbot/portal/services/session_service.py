"""Portal-side user/session lifecycle, entirely on the portal module's own
independent SQLite database (``astrbot.portal.db``).

This file does **not** import ``ChatService``, ``conversation_mgr``,
``shared_preferences``, or ``persona_mgr`` directly -- per the capability
wall (see ``core_facade.py``), the one core touchpoint this module needs
(the portal JWT signing secret) is obtained by calling
``core_facade.get_jwt_secret()``, and the conversation/session
CRUD operations it exposes are thin delegations to ``core_facade``.

Password hashing reuses ``astrbot.core.utils.auth_password`` directly (it is
not part of the capability wall's forbidden import list -- it is a
core *utility*, not a dashboard service/core-state object) with its
PBKDF2-SHA256, per-call-random-salt scheme. The exact same primitive is
reused to hash the opaque refresh token before it is ever written to
``PortalSession.refresh_token_hash`` -- it is a generic salted-string-hash
function, not something inherently password-specific.

Refresh token shape
--------------------
The refresh cookie value is not itself an opaque blob you can hash and look
up directly (PBKDF2 salts differ per call, so there is no way to derive a
lookup key from a raw candidate without iterating every non-revoked row).
Instead the value handed to the browser is a composite
``"<session_id>.<opaque secret>"``: the ``session_id`` is the
``PortalSession`` primary key (an O(1) indexed lookup), and the secret after
the dot is the actual credential, verified against the row's
``refresh_token_hash``. Neither a uuid4 session_id nor a
``secrets.token_urlsafe`` secret ever contains a literal ``.``, so splitting
on the first one is unambiguous.

Replay detection / rotation
----------------------------
Every successful refresh call marks the presented ``PortalSession`` row
``revoked_at`` and mints a brand new row (new ``session_id``, new secret,
**same** ``family_id``). If a refresh token whose row is *already*
``revoked_at``-marked is presented again -- i.e. it was already consumed, or
otherwise already revoked -- that is a replay, and the entire token family
is revoked immediately, forcing a full re-login. The same happens if the
presented secret simply fails to verify against a *not yet revoked* row
(the session_id was guessed/leaked but the secret was not, or vice versa):
that can only happen via tampering, so it is treated the same as a replay.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol

import jwt
from fastapi import Request
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.core.utils.auth_password import (
    hash_dashboard_password,
    validate_dashboard_password,
    verify_dashboard_password,
)
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.models import PortalSession, PortalUser
from astrbot.portal.services import core_facade

ACCESS_TOKEN_ALGORITHM = "HS256"

# Fixed delay applied on every failed authentication attempt, regardless of
# *why* it failed (unknown email, wrong password, disabled account) -- both
# a brute-force throttle and a timing-attack mitigation against user
# enumeration. Mirrors astrbot.dashboard.services.auth_service.AuthService's
# `await asyncio.sleep(3)` pattern on its login() failure path.
_LOGIN_FAILURE_DELAY_SECONDS = 3.0

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_REFRESH_TOKEN_SEPARATOR = "."


class RateLimiter(Protocol):
    """Duck-typed shape expected of an injected rate limiter.

    Matches ``astrbot.dashboard.server._AuthRateLimiter.acquire`` (a
    token-bucket keyed by, e.g., IP+email) so a caller can pass one straight
    through without this module needing to import or construct a registry
    itself. ``authenticate_user`` treats a limiter as optional: pass ``None``
    (the default) to skip rate limiting entirely (e.g. from a unit test).
    """

    async def acquire(self) -> bool: ...


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class SessionServiceError(Exception):
    """Base class for all errors raised by this module."""


class InvalidEmailError(SessionServiceError):
    pass


class PasswordPolicyError(SessionServiceError):
    """Raised when a candidate password fails
    ``validate_dashboard_password``'s complexity policy."""


class EmailAlreadyRegisteredError(SessionServiceError):
    pass


class AuthenticationError(SessionServiceError):
    """Generic "email or password is wrong" failure.

    Deliberately carries the same message whether the account does not
    exist, the password is wrong, or the account is disabled -- never
    reveal which case applies (classic user-enumeration defense).
    """


class RateLimitedError(SessionServiceError):
    pass


class InvalidAccessTokenError(SessionServiceError):
    pass


class InvalidRefreshTokenError(SessionServiceError):
    """The presented refresh token is malformed, unrecognized, expired, or
    fails verification. Callers should force the user back to login."""


class RefreshReplayDetectedError(InvalidRefreshTokenError):
    """The presented refresh token had already been consumed (or otherwise
    already revoked) once before. The entire token family has already been
    revoked by the time this is raised; the caller must force a full
    re-login and may want to log this as a security event."""


# ---------------------------------------------------------------------------
# Token pair
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class TokenPair:
    access_token: str
    """Short-lived HS256 JWT, claims ``{"sub": user_id, "sid": session_id,
    "iat": ..., "exp": ...}``, signed with the portal module's own secret
    (see ``core_facade.get_jwt_secret``)."""
    refresh_token: str
    """Opaque composite ``"<session_id>.<secret>"``; store verbatim in the
    ``astrbot_portal_refresh`` cookie (Path-scoped to the refresh endpoint,
    see ``astrbot.portal.config.PortalSettings.refresh_cookie_path``)."""
    session_id: str
    family_id: str
    access_expires_at: datetime
    refresh_expires_at: datetime


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime) -> datetime:
    """Normalize a datetime read back from SQLite to be tz-aware UTC.

    SQLAlchemy's default ``DateTime`` column type does not round-trip
    ``tzinfo`` through SQLite -- every value this module ever writes is
    already UTC (see :func:`_utcnow`), so a naive value read back is safe to
    re-attach ``timezone.utc`` to before comparing against a freshly
    generated ``_utcnow()`` (which *is* tz-aware and would otherwise raise
    ``TypeError: can't compare offset-naive and offset-aware datetimes``).
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _normalize_email(email: str) -> str:
    normalized = (email or "").strip().lower()
    if not _EMAIL_RE.match(normalized):
        raise InvalidEmailError("Invalid email address")
    return normalized


def _encode_refresh_token(session_id: str, raw_secret: str) -> str:
    return f"{session_id}{_REFRESH_TOKEN_SEPARATOR}{raw_secret}"


def _decode_refresh_token(value: str) -> tuple[str, str] | None:
    if not value or _REFRESH_TOKEN_SEPARATOR not in value:
        return None
    session_id, _, raw_secret = value.partition(_REFRESH_TOKEN_SEPARATOR)
    if not session_id or not raw_secret:
        return None
    return session_id, raw_secret


def _mint_access_token(
    *,
    user_id: str,
    session_id: str,
    secret: str,
    issued_at: datetime,
    expires_at: datetime,
) -> str:
    claims = {
        "sub": user_id,
        "sid": session_id,
        "iat": int(issued_at.timestamp()),
        "exp": int(expires_at.timestamp()),
        # Required so astrbot.portal.auth.security.verify_access_token (used by
        # auth/dependencies.py's get_current_user on every protected route)
        # accepts this token -- it hard-rejects any payload where
        # payload.get("typ") != "access". See ACCESS_TOKEN_TYPE in
        # astrbot/portal/auth/security.py for the authoritative value.
        "typ": "access",
    }
    return jwt.encode(claims, secret, algorithm=ACCESS_TOKEN_ALGORITHM)


def decode_access_token(token: str, secret: str) -> dict[str, Any]:
    """Verify and decode a portal access token JWT.

    Exposed so ``astrbot.portal.auth.dependencies.require_portal_user`` (or
    equivalent) can validate the ``astrbot_portal_jwt`` cookie/header on every
    request without touching the database -- access tokens are intentionally
    short-lived (see ``PortalSettings.access_token_ttl_seconds``) and are
    verified purely by signature + expiry, matching how the core dashboard's
    own JWT is checked in ``astrbot/dashboard/api/auth.py``.

    Raises:
        InvalidAccessTokenError: token is expired, malformed, or the
            signature does not match ``secret``.
    """
    try:
        return jwt.decode(token, secret, algorithms=[ACCESS_TOKEN_ALGORITHM])
    except jwt.ExpiredSignatureError as exc:
        raise InvalidAccessTokenError("Access token expired") from exc
    except jwt.InvalidTokenError as exc:
        raise InvalidAccessTokenError("Invalid access token") from exc


# ---------------------------------------------------------------------------
# User lifecycle
# ---------------------------------------------------------------------------


async def create_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    display_name: str | None = None,
) -> PortalUser:
    """Register a new local-auth portal user.

    Raises:
        InvalidEmailError: ``email`` does not look like an email address.
        PasswordPolicyError: ``password`` fails the complexity policy
            (delegates to ``validate_dashboard_password``).
        EmailAlreadyRegisteredError: the (case-insensitive) email is taken.
    """
    normalized_email = _normalize_email(email)
    try:
        validate_dashboard_password(password)
    except ValueError as exc:
        raise PasswordPolicyError(str(exc)) from exc

    existing = await session.execute(
        select(PortalUser).where(col(PortalUser.email) == normalized_email)
    )
    if existing.scalar_one_or_none() is not None:
        raise EmailAlreadyRegisteredError("Email already registered")

    user = PortalUser(
        email=normalized_email,
        password_hash=hash_dashboard_password(password),
        display_name=(display_name or "").strip() or normalized_email.split("@")[0],
        auth_method="local",
        role="user",
        status="active",
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    return user


async def get_user_by_id(session: AsyncSession, *, user_id: str) -> PortalUser | None:
    return await session.get(PortalUser, user_id)


async def authenticate_user(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    rate_limiter: RateLimiter | None = None,
) -> PortalUser:
    """Verify credentials for a local-auth login attempt.

    Applies a fixed ``_LOGIN_FAILURE_DELAY_SECONDS`` delay on any failure
    path (unknown email, wrong/absent password, disabled account) before
    raising -- always the same generic :class:`AuthenticationError`,
    never revealing which case applied.

    Args:
        rate_limiter: optional token-bucket-shaped limiter (see
            :class:`RateLimiter`); when provided and exhausted, raises
            :class:`RateLimitedError` *before* touching the password at all
            (no timing signal, no DB round-trip on an already-throttled
            caller).
    """
    if rate_limiter is not None and not await rate_limiter.acquire():
        raise RateLimitedError("Too many login attempts, please try again later")

    normalized_email = _normalize_email(email)
    result = await session.execute(
        select(PortalUser).where(col(PortalUser.email) == normalized_email)
    )
    user = result.scalar_one_or_none()

    password_ok = bool(
        user is not None
        and user.password_hash
        and verify_dashboard_password(user.password_hash, password)
    )
    account_active = user is not None and user.status == "active"

    if not password_ok or not account_active:
        await asyncio.sleep(_LOGIN_FAILURE_DELAY_SECONDS)
        raise AuthenticationError("Invalid email or password")

    assert user is not None  # for type-checkers; guaranteed by the checks above
    return user


# ---------------------------------------------------------------------------
# Token pair issuance / refresh rotation / revocation
# ---------------------------------------------------------------------------


async def issue_token_pair(
    session: AsyncSession,
    *,
    user_id: str,
    user_agent: str | None = None,
    ip: str | None = None,
    family_id: str | None = None,
) -> TokenPair:
    """Mint a fresh access+refresh token pair and persist the refresh side.

    Args:
        family_id: pass the existing family id when rotating a refresh
            token within an existing family (see :func:`rotate_refresh`).
            Leave as ``None`` for a brand new login -- a fresh family id is
            generated explicitly here rather than relying on
            ``PortalSession.family_id``'s ``default_factory`` (per the
            foundation layer's integrator note: rotated tokens must *not*
            silently get a new family via the default).
    """
    settings = get_portal_settings()
    secret = await core_facade.get_jwt_secret()
    now = _utcnow()

    resolved_family_id = family_id or str(uuid.uuid4())
    new_session_id = str(uuid.uuid4())
    raw_secret = secrets.token_urlsafe(32)
    refresh_expires_at = now + timedelta(seconds=settings.refresh_token_ttl_seconds)

    row = PortalSession(
        session_id=new_session_id,
        user_id=user_id,
        refresh_token_hash=hash_dashboard_password(raw_secret),
        family_id=resolved_family_id,
        issued_at=now,
        expires_at=refresh_expires_at,
        user_agent=user_agent,
        ip=ip,
    )
    session.add(row)
    await session.commit()

    access_expires_at = now + timedelta(seconds=settings.access_token_ttl_seconds)
    access_token = _mint_access_token(
        user_id=user_id,
        session_id=new_session_id,
        secret=secret,
        issued_at=now,
        expires_at=access_expires_at,
    )

    return TokenPair(
        access_token=access_token,
        refresh_token=_encode_refresh_token(new_session_id, raw_secret),
        session_id=new_session_id,
        family_id=resolved_family_id,
        access_expires_at=access_expires_at,
        refresh_expires_at=refresh_expires_at,
    )


async def rotate_refresh(
    session: AsyncSession,
    *,
    refresh_token: str,
    user_agent: str | None = None,
    ip: str | None = None,
) -> TokenPair:
    """Consume one refresh token and mint the next one in its family.

    Raises:
        InvalidRefreshTokenError: malformed, unrecognized, expired, or
            fails verification (the latter two revoke the whole family
            before raising, since either case can only occur via
            tampering/leakage).
        RefreshReplayDetectedError: the presented token's row was already
            revoked -- i.e. it was already consumed once, and is now being
            replayed. The whole family is revoked before raising.
    """
    decoded = _decode_refresh_token(refresh_token)
    if decoded is None:
        raise InvalidRefreshTokenError("Malformed refresh token")
    session_id, raw_secret = decoded

    row = await session.get(PortalSession, session_id)
    if row is None:
        raise InvalidRefreshTokenError("Refresh token not recognized")

    now = _utcnow()

    if row.revoked_at is not None:
        logger.warning(
            f"[portal] refresh token replay detected for session {session_id} "
            f"(family {row.family_id}); revoking family"
        )
        await revoke_family(session, family_id=row.family_id)
        raise RefreshReplayDetectedError(
            "Refresh token already used; session revoked"
        )

    if _as_utc(row.expires_at) <= now:
        row.revoked_at = now
        session.add(row)
        await session.commit()
        raise InvalidRefreshTokenError("Refresh token expired")

    if not verify_dashboard_password(row.refresh_token_hash, raw_secret):
        logger.warning(
            f"[portal] refresh token secret mismatch for session {session_id} "
            f"(family {row.family_id}); revoking family"
        )
        await revoke_family(session, family_id=row.family_id)
        raise InvalidRefreshTokenError("Refresh token invalid")

    row.revoked_at = now
    session.add(row)
    await session.commit()

    return await issue_token_pair(
        session,
        user_id=row.user_id,
        user_agent=user_agent or row.user_agent,
        ip=ip or row.ip,
        family_id=row.family_id,
    )


async def revoke_session(session: AsyncSession, *, session_id: str) -> None:
    """Revoke a single ``PortalSession`` row (idempotent)."""
    row = await session.get(PortalSession, session_id)
    if row is None or row.revoked_at is not None:
        return
    row.revoked_at = _utcnow()
    session.add(row)
    await session.commit()


async def revoke_family(session: AsyncSession, *, family_id: str) -> None:
    """Revoke every not-yet-revoked ``PortalSession`` row in a token family."""
    now = _utcnow()
    result = await session.execute(
        select(PortalSession).where(
            col(PortalSession.family_id) == family_id,
            col(PortalSession.revoked_at).is_(None),
        )
    )
    rows = result.scalars().all()
    for row in rows:
        row.revoked_at = now
        session.add(row)
    if rows:
        await session.commit()


async def logout(session: AsyncSession, *, refresh_token: str) -> None:
    """Revoke the session behind a refresh token. Permissive/idempotent: a
    malformed or already-invalid token is treated as "already logged out"
    rather than an error, since logout should never fail loudly."""
    decoded = _decode_refresh_token(refresh_token)
    if decoded is None:
        return
    session_id, _raw_secret = decoded
    await revoke_session(session, session_id=session_id)


# ---------------------------------------------------------------------------
# Conversation (chat session) lifecycle -- thin delegation to core_facade.
# Kept here so route handlers can depend on this one module for the entire
# portal-user-facing surface (auth + chat session CRUD) without needing to
# import core_facade themselves; core_facade remains the only file that
# actually imports ChatService.
# ---------------------------------------------------------------------------


async def list_conversations(user_id: str, request: Request) -> list[dict[str, Any]]:
    return await core_facade.list_sessions(user_id, request)


async def create_conversation(user_id: str, request: Request) -> dict[str, Any]:
    return await core_facade.create_session(user_id, request)


async def get_conversation(
    user_id: str, request: Request, session_id: str
) -> dict[str, Any]:
    return await core_facade.get_owned_session(user_id, request, session_id)


async def rename_conversation(
    user_id: str, request: Request, session_id: str, display_name: str
) -> dict[str, Any]:
    return await core_facade.rename_session(user_id, request, session_id, display_name)


async def delete_conversation(
    user_id: str, request: Request, session_id: str
) -> None:
    await core_facade.delete_session(user_id, request, session_id)


async def batch_delete_conversations(
    user_id: str, request: Request, session_ids: list[str]
) -> dict[str, Any]:
    return await core_facade.batch_delete_sessions(user_id, request, session_ids)


__all__ = [
    "RateLimiter",
    "SessionServiceError",
    "InvalidEmailError",
    "PasswordPolicyError",
    "EmailAlreadyRegisteredError",
    "AuthenticationError",
    "RateLimitedError",
    "InvalidAccessTokenError",
    "InvalidRefreshTokenError",
    "RefreshReplayDetectedError",
    "TokenPair",
    "ACCESS_TOKEN_ALGORITHM",
    "decode_access_token",
    "create_user",
    "get_user_by_id",
    "authenticate_user",
    "issue_token_pair",
    "rotate_refresh",
    "revoke_session",
    "revoke_family",
    "logout",
    "list_conversations",
    "create_conversation",
    "get_conversation",
    "rename_conversation",
    "delete_conversation",
    "batch_delete_conversations",
]
