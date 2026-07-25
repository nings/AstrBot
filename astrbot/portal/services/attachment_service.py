"""Portal-side attachment upload/ownership/quota enforcement (Phase 2).

This file does **not** import ``ChatService``, ``conversation_mgr``,
``shared_preferences``, or ``persona_mgr`` directly -- per the capability
wall (see ``core_facade.py``'s module docstring), the one core touchpoint
this module needs (writing the actual upload bytes via
``ChatService.save_uploaded_file``) is reached exclusively through
``core_facade.save_upload``/``core_facade.discard_orphaned_upload``. This
module owns everything upstream and downstream of that one call:

* **Upstream (before any byte reaches core):** MIME-allowlist and
  per-file-size validation against ``PortalSettings``, a best-effort
  content-signature check for the handful of formats where one is cheap and
  unambiguous, per-user daily byte-quota enforcement against
  ``PortalUsageCounter.uploaded_bytes``, and generating the uniquified
  ``stored_filename`` core will actually write to disk (see
  ``core_facade.save_upload``'s docstring for why this uniquification MUST
  happen here, not inside core).
* **Downstream (after core has written the file):** recording ownership in
  ``PortalAttachmentOwner`` -- the only remedy for core's ``Attachment``
  table having no owner column at all (verified ground truth) -- and
  exposing the ownership checks every other portal route needs:
  ``assert_owns``/``assert_owns_all`` (the latter is the exact
  ``core_facade.AttachmentOwnershipVerifier`` shape ``send_message``/
  ``update_message`` require before forwarding any media part to
  ``ChatService``) and ``download`` (the portal's own ownership-checked
  file-serving path, replacing core's own unauthenticated/unowned
  ``/api/v1/files/*`` endpoints, which must never be exposed to the portal
  SPA -- see design/portal-multiuser-plan.md section 4, checklist item 4).

Partial-failure handling (upload correctness contract)
-------------------------------------------------------
A single upload touches three different pieces of durable state that
cannot be committed as one atomic transaction: core's own SQLite database
(a different file), the filesystem (the actual bytes), and this module's
own portal database (the quota counter row + the ``PortalAttachmentOwner``
row). ``upload_file`` below is structured to make the **portal-side**
pieces (quota reservation + ownership row) atomic with each other -- they
share one ``AsyncSession``/transaction, so a failure writing the ownership
row also undoes the quota reservation via the same rollback -- while
treating the **core-side** write (file + ``Attachment`` row) as the one
non-transactional side effect, whose only possible failure mode
(succeeded on core's side, failed on the portal's side) is repaired by an
explicit compensating action: ``core_facade.discard_orphaned_upload``
deletes both the file and the core DB row. This is a best-effort
compensating action, not a true distributed transaction (there is no way
to make it one across two separate SQLite files plus a filesystem), but it
is the concrete answer to "what happens if a step after the file is
written fails": nothing is left orphaned on purpose, and any residual
failure (e.g. the disk delete itself failing) is logged loudly rather than
silently swallowed (see ``core_facade.discard_orphaned_upload``'s own
docstring).

MIME/content verification -- what is and is not actually checked
------------------------------------------------------------------
Core (``ChatService.save_uploaded_file``) sniffs real bytes for images
only, via ``detect_image_mime_type_async``; every other ``attach_type`` is
recorded with the client's bare declared ``content_type``, completely
unsniffed. This module therefore:

* Rejects any declared ``content_type`` outside
  ``PortalSettings.allowed_upload_mime_types``/``allowed_upload_attach_types``
  before a single byte is written (``_normalize_content_type`` /
  ``_attach_type_for_mime``).
* Re-validates core's own post-sniff ``mime_type`` against the same
  allowlist after the write (the one point where core's own sniffing can
  disagree with the client's declared header) and discards the upload if
  the sniffed type is not allowed.
* Adds a best-effort, honest-scope signature check (``_verify_content_
  signature``) for exactly two formats where one is cheap and unambiguous:
  ``application/pdf`` (magic bytes ``%PDF-``) and ``application/json``
  (must actually parse as JSON).
* Does **NOT** verify audio/video container bytes at all (no cheap,
  unambiguous signature check exists without a real media-parsing
  dependency, which is out of this phase's scope) and does **NOT** verify
  ``text/plain``/``text/csv`` bytes beyond the declared header (there is no
  signature for plain text at all, and enforcing a specific encoding would
  reject legitimate non-UTF-8 files). **This is a real, stated gap**: a
  client can declare an allowed audio/video/text MIME type and upload
  arbitrary bytes of that declared type, and this module will not detect
  the mismatch. Closing it fully would require real per-format parsers
  (e.g. a container-format sniffer for audio/video), which belongs in
  core's ``media_utils`` if it is ever added, not duplicated here.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, Protocol

from fastapi import Request
from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import col, select

from astrbot.core import logger
from astrbot.portal.config import get_portal_settings
from astrbot.portal.db.engine import portal_async_sessionmaker
from astrbot.portal.db.models import PortalAttachmentOwner, PortalUsageCounter
from astrbot.portal.services import core_facade
from astrbot.portal.services.core_facade import ResourceNotFoundError

# ---------------------------------------------------------------------------
# Exceptions. `ResourceNotFoundError` is deliberately NOT redefined here --
# ownership failures raise the exact same class `core_facade` already uses,
# so route code can reuse the exact same 404-mapping helper for both chat
# and attachment errors (see design/portal-multiuser-plan.md section 4,
# checklist item 4: unowned/missing must both map to 404, never 403).
# ---------------------------------------------------------------------------


class AttachmentServiceError(Exception):
    """Base class for all errors raised by this module (other than
    ownership failures, which raise ``core_facade.ResourceNotFoundError``)."""


class FileTooLargeError(AttachmentServiceError):
    """The upload exceeds ``PortalSettings.max_upload_file_bytes``."""


class UnsupportedMimeTypeError(AttachmentServiceError):
    """The declared (or, for images, the sniffed) MIME type -- or its
    derived ``attach_type`` bucket -- is not allowed."""


class QuotaExceededError(AttachmentServiceError):
    """The upload would exceed ``PortalSettings.max_daily_upload_bytes`` for
    this user's current accounting window.

    Callers should map this to HTTP 429 with ``remaining_bytes`` in the
    response body (plan section 5, Phase 2 acceptance criterion 3), not a
    silent drop/truncate.
    """

    def __init__(self, message: str, *, remaining_bytes: int) -> None:
        super().__init__(message)
        self.remaining_bytes = remaining_bytes


# ---------------------------------------------------------------------------
# Upload-like duck typing. Deliberately minimal so `file_routes.py` (not
# owned by this file) can pass a plain FastAPI/Starlette `UploadFile`
# straight through without needing any portal-specific wrapper -- this
# module does its own buffered read (see `upload_file`) rather than
# delegating streaming/`.save()` to the caller's object.
# ---------------------------------------------------------------------------


class UploadLike(Protocol):
    filename: str | None
    content_type: str | None

    async def read(self, size: int = -1) -> bytes: ...


class _BufferedUpload:
    """Upload-like object wrapping already-read, already-size-validated
    bytes, matching the ``.filename``/``.content_type``/async ``.save()``
    shape ``core_facade.save_upload`` (via ``ChatService.save_uploaded_
    file``) expects.

    Reading fully into memory first (see ``upload_file``) is what lets the
    size cap be enforced *before* a single byte is written to the shared
    attachments directory, at the cost of briefly holding one upload's
    bytes (bounded by ``max_upload_file_bytes``, 20 MiB by default) in
    memory.
    """

    __slots__ = ("filename", "content_type", "_data")

    def __init__(self, filename: str, content_type: str, data: bytes) -> None:
        self.filename = filename
        self.content_type = content_type
        self._data = data

    async def save(self, destination: str) -> None:
        await asyncio.to_thread(Path(destination).write_bytes, self._data)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def _normalize_content_type(content_type: str | None) -> str:
    return (content_type or "application/octet-stream").split(";")[0].strip().lower()


def _attach_type_for_mime(mime_type: str) -> str:
    """Same image/audio/video/else-file bucketing ``ChatService.
    save_uploaded_file`` itself uses (``chat_service.py:592-605``) --
    duplicated here (not imported) purely so this module can check
    ``allowed_upload_attach_types`` BEFORE ever calling into core, since
    core does not expose that derivation as a standalone helper."""
    if mime_type.startswith("image/"):
        return "image"
    if mime_type.startswith("audio/"):
        return "record"
    if mime_type.startswith("video/"):
        return "video"
    return "file"


def _make_stored_filename(original_filename: str | None) -> str:
    """Uniquify the caller-declared filename with a uuid4-hex stem prefix.

    MUST happen here, before ``core_facade.save_upload`` ever calls into
    ``ChatService.save_uploaded_file`` -- see ``PortalAttachmentOwner``'s
    class docstring (db/models.py) and ``core_facade.save_upload``'s own
    docstring for the cross-tenant overwrite bug this prevents. Core's
    ``sanitize_upload_filename`` only strips path components; it never
    uniquifies, and every upload lands in one shared flat directory.
    """
    normalized = (original_filename or "").replace("\\", "/")
    basename = PurePosixPath(normalized).name.replace("\x00", "").strip()
    if basename in ("", ".", ".."):
        basename = "upload"
    return f"{uuid.uuid4().hex}-{basename}"


def _safe_display_name(original_filename: str | None) -> str:
    """The client's display name, kept purely for showing the user their
    own filename back -- never used to resolve a path (see
    ``PortalAttachmentOwner.original_filename``'s docstring)."""
    name = (original_filename or "").strip() or "upload"
    return name[:512]


def _verify_content_signature(content_type: str, data: bytes) -> None:
    """Best-effort, honest-scope magic-byte/parseability check for the
    small number of formats where one is cheap and unambiguous.

    NOT a general content-sniffing solution -- see this module's own
    docstring for exactly which MIME types remain unverified beyond the
    declared ``Content-Type`` header (audio/video containers, plain
    text/CSV) and why.
    """
    if content_type == "application/pdf":
        if not data.startswith(b"%PDF-"):
            raise UnsupportedMimeTypeError(
                "File content does not match declared MIME type application/pdf"
            )
    elif content_type == "application/json":
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise UnsupportedMimeTypeError(
                "File content is not valid JSON despite declared MIME type "
                "application/json"
            ) from exc
    # image/*: core re-sniffs after write (ChatService.save_uploaded_file's
    # detect_image_mime_type_async); upload_file below re-validates that
    # post-sniff value against the allowlist once core has returned.
    # audio/*, video/*, text/plain, text/csv: no cheap, unambiguous
    # signature exists (or, for text/csv, no signature at all) -- these
    # remain verified only at the declared-Content-Type-header level. See
    # this module's docstring.


# ---------------------------------------------------------------------------
# Quota accounting. Mirrors astrbot.portal.api.chat_routes._window_start /
# ._load_or_create_counter's exact truncate/load-or-create/retry-on-
# IntegrityError shape (same UniqueConstraint(user_id, window_start) race
# those guard against) rather than inventing a second window-accounting
# helper -- see PortalUsageCounter.uploaded_bytes's own docstring
# (db/models.py) for why uploaded_bytes shares that exact row/window.
# Duplicated here (not imported) because chat_routes is a route module this
# service should not depend on, and because the functions there are
# module-private (a leading underscore, never meant to be a cross-module
# contract).
# ---------------------------------------------------------------------------


def _window_start(now: datetime | None = None) -> datetime:
    settings = get_portal_settings()
    window_seconds = max(int(settings.usage_window_seconds), 1)
    now = now or datetime.now(timezone.utc)
    epoch = int(now.timestamp())
    return datetime.fromtimestamp(
        (epoch // window_seconds) * window_seconds, tz=timezone.utc
    )


async def _load_or_create_counter(
    session: AsyncSession, user_id: str, window_start: datetime
) -> PortalUsageCounter | None:
    """Fetch this user's usage-counter row for ``window_start``, creating it
    on first use. Returns ``None`` if a concurrent insert raced us twice in
    a row (the caller must treat that as "try again later", not a bug)."""
    for _ in range(2):
        result = await session.execute(
            select(PortalUsageCounter).where(
                col(PortalUsageCounter.user_id) == user_id,
                col(PortalUsageCounter.window_start) == window_start,
            )
        )
        counter = result.scalar_one_or_none()
        if counter is not None:
            return counter

        counter = PortalUsageCounter(
            user_id=user_id,
            window_start=window_start,
            request_count=0,
            concurrent_runs=0,
            uploaded_bytes=0,
        )
        session.add(counter)
        try:
            await session.flush()
            return counter
        except IntegrityError:
            await session.rollback()
    return None


async def get_remaining_quota(user_id: str) -> int:
    """Bytes remaining in ``user_id``'s current daily upload window.

    Exposed so the route layer can tell the client how much quota is left
    on both a successful upload and a 429 rejection (``PortalUsageCounter.
    uploaded_bytes``'s own docstring in db/models.py).
    """
    settings = get_portal_settings()
    window_start = _window_start()
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalUsageCounter.uploaded_bytes).where(
                col(PortalUsageCounter.user_id) == user_id,
                col(PortalUsageCounter.window_start) == window_start,
            )
        )
        uploaded = result.scalar_one_or_none() or 0
    return max(settings.max_daily_upload_bytes - uploaded, 0)


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


async def upload_file(
    user_id: str, request: Request, upload: UploadLike
) -> dict[str, Any]:
    """Validate, quota-check, persist, and record ownership of one upload.

    Order of operations (each gate runs strictly before the next, and
    strictly before any byte is written to core's shared attachments
    directory):

    1. Declared ``content_type`` against ``allowed_upload_mime_types``.
    2. Derived ``attach_type`` against ``allowed_upload_attach_types``.
    3. Declared size (if the upload object reports one) and then the real
       read size against ``max_upload_file_bytes``.
    4. The best-effort content-signature check (``_verify_content_
       signature``) -- see module docstring for exactly what this does and
       does not catch.
    5. The per-user daily byte quota (``PortalUsageCounter.uploaded_bytes``
       vs ``max_daily_upload_bytes``), reserved (incremented) in the same
       database transaction that will later add the ``PortalAttachmentOwner``
       row, so a failure anywhere after this point rolls the reservation
       back for free via that same transaction's rollback.
    6. ``core_facade.save_upload`` (the actual disk write + core DB insert)
       with an already-uniquified ``stored_filename``.
    7. A post-write re-check of core's own (possibly sniff-corrected, for
       images) ``mime_type`` against the allowlist -- discarding the upload
       (``core_facade.discard_orphaned_upload``) if it disagrees.
    8. The ``PortalAttachmentOwner`` row, committed together with step 5's
       quota reservation.

    Raises ``UnsupportedMimeTypeError``, ``FileTooLargeError``, or
    ``QuotaExceededError`` (all ``AttachmentServiceError`` subclasses) for
    validation/quota failures, or propagates ``core_facade.CoreFacadeError``
    for anything that goes wrong on core's side.
    """
    settings = get_portal_settings()

    content_type = _normalize_content_type(getattr(upload, "content_type", None))
    if content_type not in settings.allowed_upload_mime_types:
        raise UnsupportedMimeTypeError(f"MIME type '{content_type}' is not allowed")

    attach_type = _attach_type_for_mime(content_type)
    if attach_type not in settings.allowed_upload_attach_types:
        raise UnsupportedMimeTypeError(f"Uploads of type '{attach_type}' are disabled")

    declared_size = getattr(upload, "size", None)
    if (
        isinstance(declared_size, int)
        and declared_size > settings.max_upload_file_bytes
    ):
        raise FileTooLargeError(
            f"Declared size exceeds the {settings.max_upload_file_bytes}-byte limit"
        )

    data = await upload.read()
    byte_size = len(data)
    if byte_size == 0:
        raise AttachmentServiceError("Uploaded file is empty")
    if byte_size > settings.max_upload_file_bytes:
        raise FileTooLargeError(
            f"File exceeds the {settings.max_upload_file_bytes}-byte limit"
        )

    _verify_content_signature(content_type, data)

    window_start = _window_start()
    stored_filename = _make_stored_filename(getattr(upload, "filename", None))
    original_filename = _safe_display_name(getattr(upload, "filename", None))
    buffered = _BufferedUpload(stored_filename, content_type, data)

    async with portal_async_sessionmaker() as session:
        counter = await _load_or_create_counter(session, user_id, window_start)
        if counter is None:
            await session.rollback()
            raise QuotaExceededError("请稍后重试", remaining_bytes=0)

        # Check and reserve in ONE conditional UPDATE. Reading uploaded_bytes
        # and then writing back `value + byte_size` is a lost-update race: two
        # concurrent uploads from the same user each read the same
        # pre-increment baseline, both pass the check, and the second write
        # clobbers the first, letting a burst exceed the daily cap by roughly
        # (concurrent requests x file size). Folding the comparison into the
        # UPDATE's WHERE clause makes the reservation atomic -- the statement
        # either reserves the bytes or matches no row, and SQLite serializes
        # writers so no interleaving can observe a stale baseline.
        #
        # Still inside this session's transaction, so a failure further down
        # rolls the reservation back for free, exactly as before.
        reserved = await session.execute(
            update(PortalUsageCounter)
            .where(
                col(PortalUsageCounter.user_id) == user_id,
                col(PortalUsageCounter.window_start) == window_start,
                col(PortalUsageCounter.uploaded_bytes) + byte_size
                <= settings.max_daily_upload_bytes,
            )
            .values(uploaded_bytes=col(PortalUsageCounter.uploaded_bytes) + byte_size)
            .execution_options(synchronize_session=False)
        )
        if reserved.rowcount != 1:
            # No row matched: either the quota would be exceeded, or the
            # counter row disappeared under us. Re-read to report an accurate
            # remaining figure to the client rather than guessing.
            current = await session.execute(
                select(PortalUsageCounter.uploaded_bytes).where(
                    col(PortalUsageCounter.user_id) == user_id,
                    col(PortalUsageCounter.window_start) == window_start,
                )
            )
            uploaded_now = current.scalar_one_or_none() or 0
            await session.rollback()
            raise QuotaExceededError(
                "Daily upload quota exceeded",
                remaining_bytes=max(
                    settings.max_daily_upload_bytes - uploaded_now, 0
                ),
            )

        # Read the reserved total out as a plain int while still inside the
        # transaction. The Core-level UPDATE bypassed the identity map, so the
        # loaded `counter` object holds a stale value -- and reading it back
        # after `commit()` would depend on the session's expire-on-commit
        # behaviour, which under asyncio means an implicit lazy reload outside
        # a greenlet context.
        reserved_total = await session.execute(
            select(PortalUsageCounter.uploaded_bytes).where(
                col(PortalUsageCounter.user_id) == user_id,
                col(PortalUsageCounter.window_start) == window_start,
            )
        )
        uploaded_after_reservation = reserved_total.scalar_one_or_none() or 0

        # The actual disk write + core DB insert happens here, inside this
        # transaction's *lifetime* but on an entirely different SQLite
        # database file -- it is NOT covered by `session.rollback()` below.
        # If it fails, there is nothing on core's side to clean up (core's
        # own save path never returned successfully); if it succeeds but a
        # later step here fails, `discard_orphaned_upload` is the
        # compensating action for that side specifically (see module
        # docstring's "Partial-failure handling" section).
        try:
            result = await core_facade.save_upload(
                user_id, request, buffered, stored_filename=stored_filename
            )
        except Exception:
            await session.rollback()
            raise

        if result["mime_type"] not in settings.allowed_upload_mime_types:
            # Core's post-write image sniff (detect_image_mime_type_async)
            # revealed a real MIME type outside our allowlist even though
            # the client's declared Content-Type passed the pre-write
            # check -- this can only diverge for images, the one
            # attach_type core sniffs at all. Discard immediately.
            await session.rollback()
            await core_facade.discard_orphaned_upload(request, result["attachment_id"])
            raise UnsupportedMimeTypeError(
                f"Detected MIME type '{result['mime_type']}' is not allowed"
            )

        owner = PortalAttachmentOwner(
            attachment_id=result["attachment_id"],
            user_id=user_id,
            byte_size=result["byte_size"],
            mime_type=result["mime_type"],
            attach_type=result["attach_type"],
            original_filename=original_filename,
            stored_filename=result["stored_filename"],
        )
        session.add(owner)

        try:
            await session.commit()
        except Exception:
            await session.rollback()
            await core_facade.discard_orphaned_upload(request, result["attachment_id"])
            raise

        remaining_after = max(
            settings.max_daily_upload_bytes - uploaded_after_reservation, 0
        )

    logger.info(
        f"[portal] user {user_id} recorded ownership of attachment "
        f"{owner.attachment_id} ({owner.attach_type}, {owner.byte_size} bytes)"
    )
    return {
        "attachment_id": owner.attachment_id,
        "stored_filename": owner.stored_filename,
        "original_filename": owner.original_filename,
        "mime_type": owner.mime_type,
        "attach_type": owner.attach_type,
        "byte_size": owner.byte_size,
        "remaining_quota_bytes": remaining_after,
    }


# ---------------------------------------------------------------------------
# Ownership checks / download
# ---------------------------------------------------------------------------


async def _owned_row(user_id: str, attachment_id: str) -> PortalAttachmentOwner:
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(PortalAttachmentOwner).where(
                col(PortalAttachmentOwner.attachment_id) == attachment_id
            )
        )
        owner = result.scalar_one_or_none()
    if owner is None or owner.user_id != user_id:
        raise ResourceNotFoundError("Attachment not found")
    return owner


async def assert_owns(user_id: str, attachment_id: str) -> None:
    """Raise ``core_facade.ResourceNotFoundError`` unless ``attachment_id``
    is owned by ``user_id``.

    Row absent and row-present-but-owned-by-someone-else both map to the
    identical exception/message -- never distinguishable, per the module's
    "never confirm existence" rule (design/portal-multiuser-plan.md section
    4, checklist item 4).
    """
    await _owned_row(user_id, attachment_id)


async def assert_owns_all(user_id: str, attachment_ids: Sequence[str]) -> None:
    """Batch ownership check for the send-path hook: ONE query for the
    whole list, not N.

    This is exactly the ``core_facade.AttachmentOwnershipVerifier`` shape
    ``send_message``/``update_message`` require before forwarding any media
    part to ``ChatService`` -- pass this function itself as the
    ``verify_attachment_ownership`` argument.
    """
    unique_ids = {str(attachment_id) for attachment_id in attachment_ids}
    if not unique_ids:
        return
    async with portal_async_sessionmaker() as session:
        result = await session.execute(
            select(
                col(PortalAttachmentOwner.attachment_id),
                col(PortalAttachmentOwner.user_id),
            ).where(col(PortalAttachmentOwner.attachment_id).in_(unique_ids))
        )
        owners_by_id = {row[0]: row[1] for row in result.all()}

    if any(owners_by_id.get(attachment_id) != user_id for attachment_id in unique_ids):
        raise ResourceNotFoundError("Attachment not found")


async def download(
    user_id: str, request: Request, attachment_id: str
) -> tuple[str, str, str]:
    """Resolve an owned attachment to ``(path, mime_type, display_filename)``.

    Ownership is checked here (``_owned_row``) AND independently re-checked
    inside ``core_facade.resolve_attachment`` -- see that function's own
    docstring for why it refuses to be a bare passthrough. ``display_
    filename`` is ``PortalAttachmentOwner.original_filename``, the client's
    own display name -- never used to resolve the path, only returned so
    the route can set it as the download's suggested filename.
    """
    owner = await _owned_row(user_id, attachment_id)
    path, mime_type = await core_facade.resolve_attachment(
        user_id, request, attachment_id
    )
    return path, mime_type, owner.original_filename


__all__ = [
    "AttachmentServiceError",
    "FileTooLargeError",
    "UnsupportedMimeTypeError",
    "QuotaExceededError",
    "UploadLike",
    "upload_file",
    "assert_owns",
    "assert_owns_all",
    "download",
    "get_remaining_quota",
]
