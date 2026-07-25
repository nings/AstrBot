"""SQLModel table definitions for the AstrBot WebUI module.

This module owns a completely independent ``MetaData()`` instance
(``WebUiBaseModel.metadata``), separate from AstrBot core's
``astrbot.core.db.po.SQLModel.metadata`` and from the knowledge-base
module's ``BaseKBModel.metadata``. This is the same pattern used by
``astrbot/core/knowledge_base/models.py`` -- copied verbatim on purpose so
the webui database can be created/migrated/dropped independently of core
tables, with zero risk of colliding table names or accidental cross-module
``create_all()`` calls.

None of these tables have a real ``ForeignKey`` relationship into core's
``astrbot.core.db.po`` tables (e.g. ``Attachment``, ``ConversationV2``).
Cross-module references (e.g. binding a webui user to a core conversation)
are done the same way the rest of the codebase does it: by writing the
``user_id`` string into core's existing free-text ``creator``/``username``
columns. Only ``user_id`` foreign keys *within this module's own tables*
(``WebUiSession.user_id`` -> ``WebUiUser.user_id``,
``WebUiOAuthIdentity.user_id`` -> ``WebUiUser.user_id``,
``WebUiUsageCounter.user_id`` -> ``WebUiUser.user_id``) are declared, since
those are safe same-database, same-metadata relationships.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, MetaData, SQLModel, UniqueConstraint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WebUiBaseModel(SQLModel, table=False):
    """Base class carrying the webui module's own, independent metadata."""

    metadata = MetaData()


class WebUiUser(WebUiBaseModel, table=True):
    """Root identity table for the webui module.

    ``user_id`` is the single string that is threaded through every core
    call (``creator``/``username`` fields on ``ChatService`` etc.) to get
    per-user isolation "for free" via core's existing string-equality
    ownership checks. It must always be derived from a verified JWT, never
    accepted from a client-supplied field.
    """

    __tablename__ = "webui_users"  # type: ignore

    user_id: str = Field(
        primary_key=True,
        max_length=36,
        default_factory=lambda: str(uuid.uuid4()),
    )
    email: str = Field(max_length=255, nullable=False, unique=True, index=True)
    password_hash: str | None = Field(default=None, max_length=255)
    """PBKDF2-SHA256 hash (see ``astrbot.core.utils.auth_password``).

    Nullable: an invited user has no password until they accept the
    invitation and set one; a pure OAuth-only user (Phase 5b) may never set
    a local password at all.
    """
    display_name: str = Field(max_length=100, nullable=False)
    auth_method: str = Field(max_length=20, nullable=False, default="local")
    """One of: 'local' | 'invited' | 'oauth'."""
    role: str = Field(max_length=20, nullable=False, default="user")
    status: str = Field(max_length=20, nullable=False, default="active")
    """One of: 'active' | 'invited' | 'disabled'."""
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": _utcnow},
    )


class WebUiSession(WebUiBaseModel, table=True):
    """Revocable, replay-detectable refresh-token session record.

    A ``family_id`` groups all tokens issued from the same original login;
    replay of an already-consumed refresh token must revoke the whole
    family (see plan section 4, checklist item 7).
    """

    __tablename__ = "webui_sessions"  # type: ignore

    session_id: str = Field(
        primary_key=True,
        max_length=36,
        default_factory=lambda: str(uuid.uuid4()),
    )
    user_id: str = Field(
        foreign_key="webui_users.user_id",
        max_length=36,
        nullable=False,
        index=True,
    )
    refresh_token_hash: str = Field(max_length=255, nullable=False)
    """PBKDF2 hash of the opaque 256-bit refresh token; never store raw."""
    family_id: str = Field(
        max_length=36,
        nullable=False,
        index=True,
        default_factory=lambda: str(uuid.uuid4()),
    )
    issued_at: datetime = Field(default_factory=_utcnow, nullable=False)
    expires_at: datetime = Field(nullable=False)
    revoked_at: datetime | None = Field(default=None)
    user_agent: str | None = Field(default=None, max_length=512)
    ip: str | None = Field(default=None, max_length=64)


class WebUiOAuthIdentity(WebUiBaseModel, table=True):
    """Phase 5b OAuth/SSO identity binding.

    Table is created now (empty) so migrations never need to run again for
    it; it stays unused until Phase 5 wires up the authlib OIDC flow.
    """

    __tablename__ = "webui_oauth_identities"  # type: ignore

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    user_id: str = Field(
        foreign_key="webui_users.user_id",
        max_length=36,
        nullable=False,
        index=True,
    )
    provider: str = Field(max_length=50, nullable=False)
    provider_subject: str = Field(max_length=255, nullable=False)
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_subject",
            name="uix_webui_oauth_provider_subject",
        ),
    )


class WebUiUsageCounter(WebUiBaseModel, table=True):
    """Per-user, per-window usage accounting for rate limiting and quotas.

    ``window_start`` marks the beginning of a fixed accounting window
    (e.g. truncated to the current day for Phase 1 message-count limits).
    ``request_count`` accumulates within that window; ``concurrent_runs``
    tracks the caller's currently in-flight chat runs so per-user
    concurrency can be capped independently of the request-count window
    (see plan section 6 risk mitigations: this is the lever used to keep
    peak SQLite write pressure down without a core-level rewrite).
    """

    __tablename__ = "webui_usage_counters"  # type: ignore

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    user_id: str = Field(
        foreign_key="webui_users.user_id",
        max_length=36,
        nullable=False,
        index=True,
    )
    window_start: datetime = Field(nullable=False, index=True)
    request_count: int = Field(default=0, nullable=False)
    concurrent_runs: int = Field(default=0, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "window_start",
            name="uix_webui_usage_user_window",
        ),
    )
