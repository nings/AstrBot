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

``PortalAttachmentOwner``, ``PortalKbVisibility`` and ``PortalAgentBundle``
are the exceptions to "no references into core tables": they associate with
core's ``astrbot.core.db.po.Attachment``,
``astrbot.core.knowledge_base.models.KnowledgeBase``, and (respectively,
for ``PortalAgentBundle``) core's persona names
(``astrbot.core.persona_mgr.PersonaManager.personas_v3``) and provider ids
(``astrbot.core.provider.manager.ProviderManager.inst_map``) by *value*
(string equality), never by a real SQLAlchemy ``ForeignKey`` -- the first
two core resources live in database files separate from this module's own
(``data/data_v4.db`` and the knowledge-base module's own database file, vs
this module's ``data/portal/portal.db``), and SQLite has no cross-database-
file FK mechanism; persona names and provider ids are not database rows at
all but in-memory config-derived identifiers with no table to reference in
the first place. See each class's docstring below for the specific gap
each table exists to fill: core's ``Attachment`` has no owner column at
all, core's ``KnowledgeBase`` is a global operator resource with no owner
column either -- but for KBs the portal doesn't need per-user *ownership*,
it needs an admin-curated *visibility whitelist*, since every end user
shares the same pool of operator-provisioned corpora -- and
``PortalAgentBundle`` is the indirection that lets an end user "choose an
agent" without ever naming a persona or provider directly, since the
client must never be trusted to supply either (see this module's rule 6).
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


class PortalAgentBundle(PortalBaseModel, table=True):
    """Admin-curated preset an end user may select for a session: "pick an
    agent" without the client ever naming a persona, provider, or knowledge
    base directly.

    This is the load-bearing indirection behind rule 6 (never accept a
    provider/model/persona id from the client except as a bundle id): the
    portal's end-user surface only ever exposes ``bundle_id`` values drawn
    from this table's ``enabled=True`` rows. The server resolves what a
    bundle *means* -- which persona, which chat provider, which knowledge
    bases -- and writes those resolved values into core's own per-session
    binding surfaces. The client never sees, and never supplies, a persona
    name or a provider id.

    Three fields here (``persona_id``, ``chat_provider_id``, the KB ids
    encoded in ``kb_ids_json``) associate with core or another portal table
    **by value only, never by a real SQLAlchemy ``ForeignKey``** -- see the
    module docstring above for why (cross-database-file for the KB case;
    no table at all, just in-memory config-derived identifiers, for persona
    and provider). Concretely:

    * ``persona_id`` matches a name in
      ``PersonaManager.personas_v3`` (``astrbot/core/persona_mgr.py``) --
      the same string space ``get_persona_v3_by_id``/
      ``resolve_selected_persona`` search by ``item["name"]``.
    * ``chat_provider_id`` matches a key in
      ``ProviderManager.inst_map`` (``astrbot/core/provider/manager.py``) --
      the same string space ``get_using_provider`` and
      ``get_provider_by_id`` look up by.
    * Each id encoded in ``kb_ids_json`` matches a
      ``PortalKbVisibility.kb_id`` **that is also currently
      ``enabled=True``** -- see "KB whitelist enforcement" below for why
      this one is validated against a portal table rather than core
      directly.

    **Dangling references and why none of them can corrupt a session:**
    the operator can delete a persona, remove/reconfigure a provider, or
    revoke a KB's portal visibility out from under a bundle that already
    references it, at any time, with no cascading delete and no referential
    integrity to stop them (by design -- see above). Rather than a stored
    reverse-index or an on-delete hook, dangling is handled entirely at
    *resolution time*, matching each surface's own existing graceful
    behaviour rather than inventing a new one:

    * Persona: ``resolve_selected_persona`` already treats an id with no
      matching ``personas_v3`` entry as "no persona found" and (on the
      ``webchat`` platform every portal session runs on) falls back to the
      built-in ``_chatui_default_`` persona -- never a crash, never the
      previous persona silently staying in effect forever.
    * Provider: ``get_using_provider`` already treats
      ``inst_map.get(provider_id)`` returning ``None`` as "no per-umo
      preference" and falls through to the operator's own
      ``default_provider_id`` (and, failing that, the first configured
      provider instance) -- exactly core's own pre-existing default-closed
      behaviour, not a portal invention.
    * Knowledge bases: core's own ``retrieve_knowledge_base``
      (``astrbot/core/tools/knowledge_base_tools.py``) already logs and
      skips any ``kb_id`` that no longer resolves via ``kb_mgr.get_kb``,
      so a KB deleted from core entirely degrades gracefully on its own.
      That is a *different* failure mode from the one this table's
      whitelist check guards against, though: a KB can still exist in core,
      unmodified, while an admin revokes its ``PortalKbVisibility`` row
      (or flips it to ``enabled=False``) after this bundle was created with
      it. Core has no idea that ever happened -- it would keep serving that
      corpus to any session bound to it. Whoever applies a bundle to a
      session (``agent_bundle_service.py``, not this file) MUST re-check
      every id in ``kb_ids_json`` against **current**
      ``PortalKbVisibility`` state at apply time, silently dropping any id
      that is no longer ``enabled=True`` there (logging a warning, the same
      shape core's own skip-and-log above already uses) rather than
      trusting that whatever passed validation when the bundle was created
      or last edited is still valid now.

    **KB whitelist enforcement -- yes, always, at both ends:** a bundle
    must never be able to smuggle in a corpus the operator has not made
    visible. ``PortalKbVisibility`` (see its own docstring above) is
    documented as "the *entire* access-control surface for portal KB
    access": nothing about a KB is exposed to any portal end-user endpoint
    unless a row exists there with ``enabled=True``. If bundle creation
    were allowed to reference an arbitrary core ``kb_id`` unchecked, an
    operator (or a bug in the bundle admin route) could hand every end user
    who selects that bundle silent access to a corpus never whitelisted,
    defeating Phase 3's whole model in one step. So: ``kb_ids_json`` MUST
    be validated against ``PortalKbVisibility`` (``enabled=True`` rows
    only) in two places -- (1) at bundle create/update time in
    ``agent_bundle_service.py``, rejecting any id not currently visible,
    and (2) again at bundle-apply time per the dangling-reference note
    above, since visibility can be revoked after the bundle was saved.
    Belt-and-suspenders on purpose: (1) gives the admin an immediate error
    instead of a silently-truncated bundle; (2) is what actually keeps the
    invariant true for bundles that were valid when saved but have since
    drifted.

    **Which of the two persona/provider binding surfaces this bundle uses,
    and why:** core exposes two places a session's effective persona/
    provider can be pinned (see ``PersonaManager.resolve_selected_persona``
    and ``ProviderManager.get_using_provider``). This bundle's
    ``persona_id`` is applied via ``conv_mgr.update_conversation(
    persona_id=...)`` -- the conversation's own persona, which
    ``resolve_selected_persona`` only falls back to when no
    ``session_service_config["persona_id"]`` (a *different*, force-applied
    slot) is set for that umo. This bundle's ``chat_provider_id`` is
    applied via the per-umo ``sp`` preference key
    ``provider_perf_chat_completion`` (``scope="umo"``), which
    ``get_using_provider`` checks *first*, before the operator's own
    ``default_provider_id``. These are deliberately not symmetric, and that
    asymmetry is a known, currently-open gap rather than an oversight: a
    data-scope caller writing ``session_service_config`` through core's own
    ``POST /api/v1/sessions/rules`` route can still force a persona that
    shadows this bundle's ``persona_id`` choice, the same class of bypass
    Phase 3 closed for ``kb_config`` via ``session_rule_guard.py``. Closing
    it for ``session_service_config`` (claiming it in that guard, with a
    ``reset`` that removes only the ``persona_id`` sub-key -- never the
    whole dict, since ``llm_enabled``/``tts_enabled`` live there too -- so
    the bundle's ``conv_mgr`` fallback becomes visible again) is
    ``session_rule_guard.py``'s job, not this file's; this docstring
    records the decision so whoever extends that guard does not have to
    re-derive it.

    Nullable/defaulted throughout (see class-level field docs) so
    ``engine.py``'s ``_add_missing_columns`` can reconcile this table onto
    an already-created database as fields are added in later iterations.
    """

    __tablename__ = "portal_agent_bundles"  # type: ignore

    bundle_id: str = Field(
        primary_key=True,
        max_length=36,
        default_factory=lambda: str(uuid.uuid4()),
    )
    name: str = Field(max_length=100, nullable=False)
    """Consumer-facing display name shown in the end-user bundle picker
    (e.g. "Friendly Assistant", "Technical Support"). Never a persona name
    or provider id -- those are never shown to the client at all."""
    description: str | None = Field(default=None, max_length=1000)
    """Optional consumer-facing description of what this bundle is for,
    shown alongside ``name`` in the picker so a user can decide which
    bundle to select without any operator-internal context (persona
    prompt, provider identity) ever reaching the client."""

    persona_id: str | None = Field(default=None, max_length=100)
    """The persona this bundle applies, matched by value against
    ``PersonaManager.personas_v3[*]["name"]`` -- see class docstring for
    the exact binding surface (``conv_mgr.update_conversation``) and the
    dangling-reference behaviour if the operator deletes it later.
    Nullable: ``None`` means this bundle does not touch persona at all,
    leaving whatever persona the conversation already had in effect (e.g.
    a bundle that exists only to switch the provider for cost/latency
    reasons). This is a per-field, finer-grained instance of the module's
    overall default-closed principle: a bundle silent on one dimension
    changes nothing on that dimension, rather than forcing an implicit
    value."""
    chat_provider_id: str | None = Field(default=None, max_length=100)
    """The chat-completion provider this bundle applies, matched by value
    against ``ProviderManager.inst_map`` keys. Deliberately only the chat
    provider -- the plan scopes this phase to persona+provider+KB, not a
    per-bundle STT/TTS/embedding/rerank override; those keep using the
    operator's global defaults regardless of which bundle is selected.
    Applied via the per-umo ``provider_perf_chat_completion`` ``sp`` key
    (see class docstring) -- this is the mechanism the ground truth
    confirms keeps Phase 1's "client never supplies a provider" invariant
    intact, since it is the server, not the client, writing this
    preference, and only after resolving it from this column. Nullable:
    ``None`` means this bundle does not set a provider preference, leaving
    ``get_using_provider`` to fall through to the operator's own
    ``default_provider_id`` -- i.e. the safe, default-closed behaviour for
    this dimension is simply not writing the per-umo key at all, not
    writing some sentinel value."""
    kb_ids_json: str = Field(default="[]", nullable=False)
    """JSON-encoded list of ``PortalKbVisibility.kb_id`` strings this
    bundle binds (e.g. ``'["kb-abc", "kb-def"]'``), stored as plain text
    rather than a native column type since SQLite has no first-class JSON
    column and this module has no other precedent for one (see class
    docstring for the two-point whitelist validation this list must pass
    both at write time and at every apply time). Defaults to ``"[]"``
    (an explicit empty list, JSON-decoded) rather than ``NULL`` so callers
    never need a null-check before ``json.loads`` -- consistent with this
    module's existing convention (``PortalKbVisibility``'s whole reason
    for existing is that an explicit empty ``kb_ids`` is a meaningful,
    different state from "unset"; the same convention is reused here
    rather than invented twice). Bound to the same
    ``PortalSettings.max_kb_ids_per_session`` ceiling (``config.py``) at
    apply time as any other KB-binding path -- deliberately not a second,
    independent cap, since the retrieval-latency/context-cost rationale
    behind that ceiling applies identically regardless of whether the
    KBs arrived at a session via the direct bind route or via a bundle."""

    enabled: bool = Field(default=True, nullable=False)
    """Whether this bundle is currently selectable by end users. A flag
    rather than a delete, mirroring ``PortalKbVisibility.enabled``, so
    disabling a bundle preserves it (and any audit trail) for sessions that
    already selected it in the past to keep resolving consistently, while
    removing it from the picker for new selections."""
    sort_order: int = Field(default=0, nullable=False)
    """Ascending sort key for the end-user bundle picker's display order.
    Purely a presentation concern with no authorization meaning -- an
    admin reordering bundles cannot grant or revoke access to anything."""
    is_default: bool = Field(default=False, nullable=False)
    """UI-only "recommended/pre-highlighted" hint for the picker (e.g. the
    bundle shown first or pre-selected in the widget before the user has
    chosen anything). This is deliberately NOT a server-side auto-bind
    marker: a session with no bundle explicitly selected by the user
    still has no ``bundle_id`` bound anywhere, and every resolution path
    (persona, provider, KB) falls through to the operator's own configured
    defaults exactly as if this column did not exist -- per the module's
    default-closed principle, "an admin marked bundle X as recommended"
    must never be conflated with "the server picked bundle X for you".
    Not enforced unique at the database level (SQLAlchemy/SQLite would
    need a partial/filtered unique index for that); at most one row having
    ``is_default=True`` is a convention the admin bundle-management route
    is expected to maintain, not a constraint this table enforces."""
    updated_by: str | None = Field(default=None, max_length=255)
    """The operator identity that (most recently) created/updated this
    bundle, sourced from the admin route's ``AuthContext.username``
    (``astrbot/dashboard/api/auth.py``'s ``require_system_scope``
    dependency) at write time -- a plain string, not a foreign key, for
    the same reason ``PortalKbVisibility.granted_by`` is: dashboard admin
    identities are not rows in this module's own tables. Nullable and
    informational/audit-only, never used in an authorization decision."""

    created_at: datetime = Field(default_factory=_utcnow, nullable=False)
    updated_at: datetime = Field(
        default_factory=_utcnow,
        nullable=False,
        sa_column_kwargs={"onupdate": _utcnow},
    )
