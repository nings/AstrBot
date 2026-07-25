"""SQLModel table definitions for the AstrBot Portal module.

This module owns a completely independent ``MetaData()`` instance
(``PortalBaseModel.metadata``), separate from AstrBot core's
``astrbot.core.db.po.SQLModel.metadata`` and from the knowledge-base
module's ``BaseKBModel.metadata``. This is the same pattern used by
``astrbot/core/knowledge_base/models.py`` -- copied verbatim on purpose so
the portal database can be created/migrated/dropped independently of core
tables, with zero risk of colliding table names or accidental cross-module
``create_all()`` calls.

None of these tables have a real ``ForeignKey`` relationship into core's
``astrbot.core.db.po`` tables (e.g. ``Attachment``, ``ConversationV2``).
Cross-module references (e.g. binding a portal user to a core conversation)
are done the same way the rest of the codebase does it: by writing the
``user_id`` string into core's existing free-text ``creator``/``username``
columns. Only ``user_id`` foreign keys *within this module's own tables*
(``PortalSession.user_id`` -> ``PortalUser.user_id``,
``PortalOAuthIdentity.user_id`` -> ``PortalUser.user_id``,
``PortalUsageCounter.user_id`` -> ``PortalUser.user_id``,
``PortalAttachmentOwner.user_id`` -> ``PortalUser.user_id``) are declared,
since those are safe same-database, same-metadata relationships.

``PortalAttachmentOwner`` and ``PortalKbVisibility`` are the exceptions to
"no references into core tables": they associate with core's
``astrbot.core.db.po.Attachment`` and
``astrbot.core.knowledge_base.models.KnowledgeBase`` respectively by
*value* (``attachment_id`` / ``kb_id`` string equality), never by a real
SQLAlchemy ``ForeignKey`` -- both core tables live in database files
separate from this module's own (``data/data_v4.db`` and the knowledge-base
module's own database file, vs this module's ``data/portal/portal.db``),
and SQLite has no cross-database-file FK mechanism. See each class's
docstring below for the specific gap each table exists to fill: core's
``Attachment`` has no owner column at all, and core's ``KnowledgeBase`` is a
global operator resource with no owner column either -- but for KBs the
portal doesn't need per-user *ownership*, it needs an admin-curated
*visibility whitelist*, since every end user shares the same pool of
operator-provisioned corpora.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlmodel import Field, MetaData, SQLModel, UniqueConstraint


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PortalBaseModel(SQLModel, table=False):
    """Base class carrying the portal module's own, independent metadata."""

    metadata = MetaData()


class PortalUser(PortalBaseModel, table=True):
    """Root identity table for the portal module.

    ``user_id`` is the single string that is threaded through every core
    call (``creator``/``username`` fields on ``ChatService`` etc.) to get
    per-user isolation "for free" via core's existing string-equality
    ownership checks. It must always be derived from a verified JWT, never
    accepted from a client-supplied field.
    """

    __tablename__ = "portal_users"  # type: ignore

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


class PortalSession(PortalBaseModel, table=True):
    """Revocable, replay-detectable refresh-token session record.

    A ``family_id`` groups all tokens issued from the same original login;
    replay of an already-consumed refresh token must revoke the whole
    family (see plan section 4, checklist item 7).
    """

    __tablename__ = "portal_sessions"  # type: ignore

    session_id: str = Field(
        primary_key=True,
        max_length=36,
        default_factory=lambda: str(uuid.uuid4()),
    )
    user_id: str = Field(
        foreign_key="portal_users.user_id",
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


class PortalOAuthIdentity(PortalBaseModel, table=True):
    """Phase 5b OAuth/SSO identity binding.

    Table is created now (empty) so migrations never need to run again for
    it; it stays unused until Phase 5 wires up the authlib OIDC flow.
    """

    __tablename__ = "portal_oauth_identities"  # type: ignore

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    user_id: str = Field(
        foreign_key="portal_users.user_id",
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
            name="uix_portal_oauth_provider_subject",
        ),
    )


class PortalUsageCounter(PortalBaseModel, table=True):
    """Per-user, per-window usage accounting for rate limiting and quotas.

    ``window_start`` marks the beginning of a fixed accounting window
    (e.g. truncated to the current day for Phase 1 message-count limits).
    ``request_count`` accumulates within that window; ``concurrent_runs``
    tracks the caller's currently in-flight chat runs so per-user
    concurrency can be capped independently of the request-count window
    (see plan section 6 risk mitigations: this is the lever used to keep
    peak SQLite write pressure down without a core-level rewrite).

    ``uploaded_bytes`` (Phase 2) extends the same per-user/per-window row
    with a rolling daily uploaded-byte total, rather than introducing a
    second counter table. This was a deliberate choice, not the only
    possible one -- see ``contracts_for_downstream`` in this phase's
    handoff report for the justification -- but concretely: uploads and
    chat requests already share the exact same accounting shape (one row
    per user per day, truncate-then-load-or-create,
    check-then-increment-in-one-transaction under the
    ``UniqueConstraint(user_id, window_start)`` below), and
    ``astrbot/portal/api/chat_routes.py`` already implements that
    truncate/load-or-create/increment dance for ``request_count`` and
    ``concurrent_runs`` against this exact table. Adding a column here
    means the upload route reuses that proven, race-safe pattern (same
    row, same transaction boundary, same unique constraint) instead of
    duplicating the whole window-accounting machinery -- including a
    second ``_window_start()``-equivalent and a second
    load-or-create-under-lock helper -- for a second table that would
    track the calendar day in lockstep with this one anyway. The
    trade-off accepted: a request-only day (no uploads) still gets a row
    with ``uploaded_bytes=0`` once any counter activity happens for that
    user that day, which is negligible storage overhead for the
    simplicity gained. Whoever implements the upload route should follow
    the same "read row -> check ``uploaded_bytes + incoming_size <=
    settings.max_daily_upload_bytes`` -> increment in the same
    transaction" pattern used for ``request_count`` in ``chat_routes.py``,
    and return the remaining quota (``max_daily_upload_bytes -
    uploaded_bytes``) to the client on both success and 429 rejection so
    the UI can show it.
    """

    __tablename__ = "portal_usage_counters"  # type: ignore

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    user_id: str = Field(
        foreign_key="portal_users.user_id",
        max_length=36,
        nullable=False,
        index=True,
    )
    window_start: datetime = Field(nullable=False, index=True)
    request_count: int = Field(default=0, nullable=False)
    concurrent_runs: int = Field(default=0, nullable=False)
    uploaded_bytes: int = Field(default=0, nullable=False)
    """Cumulative bytes uploaded by this user within ``window_start``'s
    accounting window (Phase 2). Checked-and-incremented in the same
    transaction as the load-or-create read, mirroring ``request_count``'s
    existing pattern in ``chat_routes.py``. Compared against
    ``PortalSettings.max_daily_upload_bytes`` (see
    ``astrbot/portal/config.py``) before the upload route accepts a file.
    """

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "window_start",
            name="uix_portal_usage_user_window",
        ),
    )


class PortalAttachmentOwner(PortalBaseModel, table=True):
    """Shadow ownership record for a core ``astrbot.core.db.po.Attachment`` row.

    This table is the **only** remedy for a real architectural gap: core's
    ``Attachment`` (``astrbot/core/db/po.py:340-368``) has no owner column
    at all, and its lookup helper
    (``BaseDatabase.get_attachment_by_id``, ``astrbot/core/db/sqlite.py``)
    does a bare ``WHERE attachment_id == :id`` with no owner filter --
    it cannot have one, since core has no concept of a portal end-user.
    Without this table, any authenticated portal caller could reference
    *any* attachment_id (including another user's) in a multimodal message
    part and the core send path would happily read that file and feed it
    to the model. Every portal route that accepts an ``attachment_id``
    from a client (upload confirmation, message parts, the download route)
    MUST look it up here and confirm ``user_id`` matches the authenticated
    caller before doing anything else with it; a miss (row absent, or
    present but owned by someone else) must be treated identically and
    mapped to a 404 (never 403 -- a 403 would let a caller distinguish
    "not yours" from "doesn't exist", i.e. an existence oracle).

    ``attachment_id`` associates with core's ``Attachment.attachment_id``
    **by value equality only, never by a real ``ForeignKey``**: core's
    ``Attachment`` table lives in a different SQLite database file
    (``data/data_v4.db``) than this module's own
    (``data/portal/portal.db``), and SQLite (like every mainstream RDBMS)
    cannot enforce a foreign-key constraint across separate database
    files/connections. Referential integrity between this row and the
    corresponding core row is therefore an application-level invariant
    (the row here is written immediately after the core insert succeeds,
    inside the upload route/service, never speculatively before it) rather
    than a database-enforced one.

    ``byte_size``, ``mime_type``, ``attach_type``, and the two filename
    columns are deliberately duplicated out of the upload flow onto this
    row (rather than re-derived later by re-opening/``stat``-ing the file
    on disk or re-querying core's ``Attachment``) so that:

    * quota accounting (``PortalUsageCounter.uploaded_bytes``) and any
      future per-user storage reporting never need a filesystem ``stat``
      call or a second cross-database query just to learn how big a file
      the user already has on disk;
    * ``original_filename`` (what the client called it, e.g. ``photo.png``)
      and ``stored_filename`` (the uniquified name actually written to
      core's shared flat ``attachments_dir``, e.g.
      ``3f9c2b1a-photo.png``) are recorded as the two *different* values
      they actually are. Core's own
      ``sanitize_upload_filename`` (``chat_service.py:41-48``) does
      **not** uniquify -- it only replaces empty/``.``/``..`` names with a
      fresh uuid, and otherwise returns the client's basename unchanged.
      Combined with core writing every upload into one shared flat
      directory, two different users uploading a same-named file (e.g.
      both send ``photo.png``) collide on disk: the second write
      overwrites the first, and the first user's already-issued
      ``Attachment.path`` now silently serves the second user's bytes --
      a real cross-tenant content-confusion bug. The portal MUST generate
      a unique ``stored_filename`` (e.g. a uuid prefix/stem) itself and
      pass that -- not the client's raw filename -- into whatever core
      call ultimately writes the file, recording both names here so the
      client-facing display name (``original_filename``) is preserved
      without the collision risk. This fix belongs entirely in the portal
      layer (the upload service/route, not owned by this file); core's
      ``sanitize_upload_filename`` must not be patched.
    """

    __tablename__ = "portal_attachment_owners"  # type: ignore

    attachment_id: str = Field(
        primary_key=True,
        max_length=36,
    )
    """Same string as core's ``Attachment.attachment_id``. Primary key here
    (not autoincrement) since this row's whole purpose is a 1:1 shadow of
    exactly one core attachment row, looked up by that id -- never by this
    table's own surrogate key."""
    user_id: str = Field(
        foreign_key="portal_users.user_id",
        max_length=36,
        nullable=False,
        index=True,
    )
    """Indexed (not just present) because every real query against this
    table is either "does this attachment_id belong to this user_id"
    (PK lookup + equality check, index not required for that alone) or,
    for future per-user listing/quota-repair tooling, "give me every
    attachment row owned by this user_id" -- a reverse lookup that does
    need this index."""
    byte_size: int = Field(nullable=False)
    """Size of the stored file in bytes, captured once at upload time.
    Used for quota accounting (``PortalUsageCounter.uploaded_bytes``)
    without re-``stat``-ing the file on every check."""
    mime_type: str = Field(max_length=255, nullable=False)
    """The MIME type actually recorded against the core ``Attachment`` row
    (post-sniff for images per core's ``detect_image_mime_type_async``;
    the client-supplied ``content_type`` as-is for record/video/file,
    since core does not sniff those). Duplicated here so the portal's own
    MIME-allowlist enforcement and any future audit/reporting never need
    to re-open core's database."""
    attach_type: str = Field(max_length=20, nullable=False)
    """One of core's ``ChatService.save_uploaded_file`` buckets: ``'image'``
    | ``'record'`` | ``'video'`` | ``'file'``."""
    original_filename: str = Field(max_length=512, nullable=False)
    """The client's display name for this upload (e.g. ``photo.png``), used
    only for showing the user their own filename back -- never used to
    resolve a path. See class docstring for why this must differ from
    ``stored_filename``."""
    stored_filename: str = Field(max_length=512, nullable=False)
    """The uniquified name actually written to disk in core's shared flat
    attachments directory (e.g. ``3f9c2b1a-photo.png``). See class
    docstring for the cross-tenant overwrite bug this field exists to
    prevent."""
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)


class PortalKbVisibility(PortalBaseModel, table=True):
    """Admin-curated whitelist of knowledge bases end users may select.

    Core's knowledge bases (``astrbot.core.knowledge_base.models.KnowledgeBase``,
    ``kb_id``/``kb_name`` at ``astrbot/core/knowledge_base/models.py:24,31``) are
    **global operator resources with no owner column** -- unlike
    ``PortalAttachmentOwner`` shadowing a per-request core resource that a
    single user just created, this table is not "who owns this row" but
    "which of the operator's global corpora has an admin decided end users
    are even allowed to see at all". It is therefore the *entire*
    access-control surface for portal KB access: nothing about a KB is
    exposed to any portal end-user endpoint unless a row exists here with
    ``enabled=True``. See ``astrbot/core/tools/knowledge_base_tools.py:24-87``
    for why this matters more than it might look like it should -- a portal
    session with no explicit ``kb_config`` written falls through to the
    operator's *global* ``kb_names`` config (a default-open leak), so the
    portal's session-creation path must always write an explicit
    ``{"kb_ids": []}`` and every bind attempt must be checked against this
    table before that list is ever allowed to grow.

    ``kb_id`` associates with core's ``KnowledgeBase.kb_id`` **by value
    equality only, never by a real SQLAlchemy ``ForeignKey``**: exactly the
    same cross-database-file constraint documented on
    ``PortalAttachmentOwner`` above applies here -- core's knowledge-base
    metadata lives in its own database file (see
    ``astrbot.core.knowledge_base.models.BaseKBModel.metadata``, a separate
    ``MetaData()`` instance again, just like this module's own), not in
    this module's ``data/portal/portal.db``, and SQLite cannot enforce an
    FK across separate database connections/files.
    """

    __tablename__ = "portal_kb_visibilities"  # type: ignore

    id: int | None = Field(
        default=None,
        primary_key=True,
        sa_column_kwargs={"autoincrement": True},
    )
    kb_id: str = Field(max_length=36, nullable=False, unique=True, index=True)
    """Same string as core's ``KnowledgeBase.kb_id`` (a 36-char uuid). Unique
    and indexed: this table holds at most one visibility row per core KB,
    and the primary lookup shape everywhere (route authorization checks,
    the "list visible KBs" query) is by this value, never by the surrogate
    ``id`` above."""
    kb_name: str = Field(max_length=100, nullable=False)
    """A cached copy of core's ``KnowledgeBase.kb_name`` (also max 100 there,
    see ``astrbot/core/knowledge_base/models.py:31``), captured at the time
    an admin whitelists this ``kb_id``, so the admin's own visibility-list
    UI can render a recognizable name without an extra cross-database join
    on every page load. This value **can go stale**: if the operator later
    renames the KB in core, this column does not update automatically --
    there is no event/webhook wiring core's KB rename path back into this
    table. The expected remedy is operational, not automatic: the admin
    visibility-list UI (Phase 3's admin page) is expected to re-fetch core's
    live KB list on load and diff it against this cached value, prompting
    the admin to refresh/re-save the row if it drifts, rather than this
    table silently self-healing. Consumer-facing surfaces should prefer
    ``display_name`` when set (see below) over this field precisely because
    this one is an operator-corpus name that can drift, not a
    consumer-facing label."""
    enabled: bool = Field(default=True, nullable=False)
    """Whether this KB is currently selectable by end users. Kept as a flag
    rather than deleting the row on revocation so that revoking visibility
    preserves the row's audit trail (``created_at``/``updated_at``/
    ``granted_by``) instead of losing history of who granted it and when;
    every portal-facing "list visible KBs" and "bind this kb_id" check must
    filter on ``enabled == True`` in addition to the row merely existing."""
    display_name: str | None = Field(default=None, max_length=100)
    """Optional consumer-facing name to show end users instead of the raw
    operator-facing ``kb_name`` cache above -- an internal corpus is often
    named something like ``internal-eng-runbooks-v2`` that means nothing
    (or means the wrong thing) to an end user, so an admin can set a
    friendlier label here. Falls back to ``kb_name`` when unset."""
    description: str | None = Field(default=None, max_length=1000)
    """Optional consumer-facing description of what this KB covers, shown
    alongside ``display_name``/``kb_name`` in the end-user KB picker so a
    user can decide which KB(s) to bind without operator-internal context."""
    granted_by: str | None = Field(default=None, max_length=255)
    """The operator identity that (most recently) granted/updated this
    row's visibility, sourced cheaply from the admin route's
    ``AuthContext.username`` (``astrbot/dashboard/api/auth.py``'s
    ``require_system_scope`` dependency) at write time -- a plain string,
    not a foreign key, since dashboard admin identities are not rows in
    this module's own tables (they are not ``PortalUser``s at all; the two
    identity systems are independent by design, see the module's Phase 1
    capability-wall notes). Nullable because the column must reconcile
    additively onto an already-created table via ``engine.py``'s
    ``_add_missing_columns`` if this field is ever added after initial
    rollout, and because it is informational/audit-only, never used in an
    authorization decision."""
    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": _utcnow},
    )
