"""Attachment upload/download HTTP routes for the AstrBot Portal module
(Phase 2).

Every handler here goes through
:mod:`astrbot.portal.services.attachment_service` -- the module that owns
MIME/size/quota validation, the uniquified ``stored_filename`` generation,
the ``PortalAttachmentOwner`` shadow-ownership bookkeeping, and the sole
authorized wrapper around ``core_facade.save_upload``/``resolve_attachment``.
This file must never import ``ChatService`` or any other capability-wall
member directly (rule 5 of the module's engineering rules) -- it does not,
and it does not need to: ``attachment_service`` already reaches
``core_facade`` for the one core touchpoint (the actual disk write).

These routes are the **sole sanctioned replacement** for core's own
``GET /api/v1/files/*`` endpoints (``astrbot/dashboard/api/files.py``),
which must never be exposed to the portal SPA -- those endpoints either
have no ownership check at all (``/files/{attachment_id}``), resolve any
file in the shared attachments directory by bare filename
(``/files/content?filename=``), or have no auth dependency whatsoever
(the token-based routes). See design/portal-multiuser-plan.md section 4,
checklist item 4, and this phase's verified ground truth.

Every ``user_id`` used below comes from
:func:`astrbot.portal.auth.dependencies.get_current_user` -- never from a
request body, query string, or header (rule 4).

Ownership failures (``core_facade.ResourceNotFoundError``, raised by every
``attachment_service`` function that takes a client-supplied
``attachment_id``) map to HTTP 404, never 403 -- a 403 would let a caller
distinguish "not yours" from "does not exist", i.e. an existence oracle
(see ``PortalAttachmentOwner``'s class docstring in db/models.py and this
phase's non-negotiable engineering rules).

DELETE is deliberately NOT implemented here -- see the "Why no DELETE"
section below for the concrete reason, rather than shipping a route that
lies about what it does.

Why no DELETE
--------------
A correct delete would need to do two things atomically: (1) remove the
core ``Attachment`` row + on-disk file, and (2) give the byte quota back
to the *exact* daily accounting window (``PortalUsageCounter.window_start``)
it was originally charged against. ``PortalAttachmentOwner`` records
``created_at`` but not which ``window_start`` the upload was charged
under -- and ``_window_start()``'s truncation is a pure function of
*when it runs*, not of any stored value, so re-deriving "the window this
upload was charged to" from ``created_at`` after the fact is only reliable
if the file is deleted within the same window it was uploaded in (true
same-day deletes are fine; a delete of a file uploaded in an earlier
window would silently credit *today's* counter instead of the row it was
actually debited from -- a real accounting bug, not a cosmetic one).
Additionally, ``core_facade.discard_orphaned_upload`` -- the only existing
facade function that deletes a core attachment -- is explicitly documented
as NOT owner-checked and NOT meant to be reachable from any client-supplied
attachment_id ("Never call this with an attachment_id sourced from a
client request"); wiring a client-facing DELETE route through it would
mean either violating that documented contract or duplicating an
independent, unaudited delete-from-core path here. Building this correctly
needs a schema change (recording ``window_start`` on
``PortalAttachmentOwner``, or a dedicated core-facade delete function that
is explicitly designed to be owner-checked and client-reachable), which is
outside this file's ownership (db/models.py, core_facade.py). Shipping a
DELETE that silently mis-credits quota, or that reuses a function
documented as unsafe for this exact purpose, would be worse than omitting
it -- so it is omitted, and flagged here and in this phase's ``gaps``.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse

from astrbot.portal.auth.dependencies import get_current_user, require_csrf
from astrbot.portal.db.models import PortalUser
from astrbot.portal.services import attachment_service
from astrbot.portal.services.core_facade import ResourceNotFoundError

router = APIRouter(prefix="/files", tags=["Portal Files"])
"""Mounted by ``astrbot.portal.api.router.portal_router`` (prefix ``/portal``)
exactly like ``chat_routes.router`` -- every path below ends up public at
``/api/v1/portal/files/...``."""

# MIME-type prefixes safe to render inline in a browser tab/`<img>`/`<audio>`/
# `<video>` element without meaningfully increasing this endpoint's attack
# surface, given `PortalSettings.allowed_upload_mime_types` already excludes
# anything that executes as active content (no SVG, no HTML/JS, no
# archives -- see that field's own docstring). Everything else (PDF, plain
# text/CSV, JSON) is served as a forced download instead: none of those are
# meant to be inline-previewed by the chat composer, and forcing a download
# keeps one more browser code path (the built-in PDF/text viewer rendering
# attacker-supplied bytes as a top-level "page" from this product's own
# origin) out of the picture entirely.
_INLINE_MIME_PREFIXES = ("image/", "audio/", "video/")


def _content_disposition(display_filename: str, *, inline: bool) -> str:
    """Build a ``Content-Disposition`` header value from a client-supplied
    display filename, safely.

    ``display_filename`` is ``PortalAttachmentOwner.original_filename`` --
    attacker-controlled (it is whatever the uploading client sent) but
    never used to resolve a path (see that field's own docstring); it is
    used here purely as a header value, so it must be neutralized against
    header-injection (stray CR/LF) and non-ASCII bytes that a bare
    ``filename=`` parameter cannot carry safely. Both an ASCII-only
    fallback (``filename=``) and an RFC 5987 percent-encoded UTF-8 form
    (``filename*=``) are emitted, matching the standard dual-parameter
    pattern browsers expect.
    """
    disposition = "inline" if inline else "attachment"
    sanitized = display_filename.replace("\r", "").replace("\n", "").replace('"', "'")
    ascii_fallback = sanitized.encode("ascii", "ignore").decode("ascii").strip()
    if not ascii_fallback:
        ascii_fallback = "download"
    encoded = quote(sanitized, safe="")
    return f'{disposition}; filename="{ascii_fallback}"; filename*=UTF-8\'\'{encoded}'


# ---------------------------------------------------------------------------
# Upload
# ---------------------------------------------------------------------------


@router.post("", status_code=201)
async def upload_attachment(
    request: Request,
    file: UploadFile = File(...),
    current_user: PortalUser = Depends(get_current_user),
    _csrf: None = Depends(require_csrf),
) -> dict[str, Any]:
    """Validate, quota-check, and persist one uploaded file.

    Returns the ``attachment_id`` plus the display metadata the composer
    needs to render an upload preview/pending-attachment chip -- deliberately
    NOT ``attachment_service.upload_file``'s full result dict verbatim:
    ``stored_filename`` (the on-disk uniquified name) is withheld, since it
    is an internal implementation detail with no legitimate client use and
    this route must never expose any filesystem-path-adjacent detail.

    429 (with the caller's remaining daily quota in the body) on quota
    exhaustion -- plan section 5, Phase 2 acceptance criterion 3 -- not a
    silent drop/truncate. 415 for a disallowed MIME type, 400 for an
    oversized or empty file.
    """
    try:
        result = await attachment_service.upload_file(
            current_user.user_id, request, file
        )
    except attachment_service.QuotaExceededError as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "message": str(exc),
                "remaining_quota_bytes": exc.remaining_bytes,
            },
        ) from exc
    except attachment_service.UnsupportedMimeTypeError as exc:
        raise HTTPException(status_code=415, detail=str(exc)) from exc
    except attachment_service.FileTooLargeError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except attachment_service.AttachmentServiceError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    return {
        "attachment_id": result["attachment_id"],
        "filename": result["original_filename"],
        "mime_type": result["mime_type"],
        "attach_type": result["attach_type"],
        "byte_size": result["byte_size"],
        "remaining_quota_bytes": result["remaining_quota_bytes"],
    }


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------


@router.api_route("/{attachment_id}", methods=["GET", "HEAD"])
async def download_attachment(
    attachment_id: str,
    request: Request,
    current_user: PortalUser = Depends(get_current_user),
) -> FileResponse:
    """Serve an attachment the caller owns.

    ``HEAD`` has to be registered explicitly. FastAPI's ``@router.get``
    registers ``methods=["GET"]`` and nothing else, so a ``HEAD`` request
    against a GET-only route gets a flat ``405`` -- it does *not* fall back
    to running the handler and dropping the body. The portal's file chip
    probes with ``HEAD`` before offering a download
    (``PortalAttachment.vue::probeFileExists``) and renders
    "unavailable or deleted" on any non-OK response, so a GET-only
    registration marks *every* file attachment broken in the UI. Starlette
    still discards the body for us, so ``HEAD`` costs a stat, not a read.

    Ownership is checked by ``attachment_service.download`` against
    ``PortalAttachmentOwner`` BEFORE anything else happens; a missing row
    or one owned by a different user both raise
    ``core_facade.ResourceNotFoundError`` here, mapped to a single,
    indistinguishable 404 -- never a 403 (that would confirm the id exists
    but belongs to someone else, an existence oracle).

    Security headers on the response, since this endpoint's entire job is
    to serve attacker-supplied bytes from the product's own origin:

    * ``X-Content-Type-Options: nosniff`` -- always set. Without it, a
      browser could sniff a response's real bytes and render it as a
      different type than the declared ``Content-Type`` (e.g. a
      mis-declared upload sniffed as ``text/html``), reintroducing exactly
      the risk the MIME allowlist (which excludes ``text/html``/SVG/JS) is
      meant to close off.
    * ``Content-Security-Policy: default-src 'none'; sandbox`` -- always
      set. This only takes effect on the rare path where a browser
      navigates to this URL directly as a top-level document/iframe (e.g.
      a user opening the download link in a new tab) -- it is not enforced
      for ordinary ``<img>``/``<audio>``/``<video>`` element loads, which
      the portal composer's inline previews rely on and which are governed
      by the *embedding* page's own CSP instead. ``sandbox`` with no
      allow-* tokens disables script execution, forms, and top-level
      navigation for that direct-navigation case -- the last line of
      defense against a MIME-allowlist gap (e.g. the acknowledged one for
      PDF: ``application/pdf`` is allowed but its internal-JavaScript risk
      is not eliminated by this header, since a PDF viewer's own scripting
      engine is not governed by the web page's CSP; closing that fully
      would need a PDF-sanitizing parser, out of this phase's scope -- see
      ``attachment_service.py``'s module docstring for the equivalent,
      already-stated MIME-verification gap).
    * ``Content-Disposition``: ``inline`` for image/audio/video (so the
      composer's `<img>`/`<audio>`/`<video>` previews work), forced
      ``attachment`` for everything else (PDF/text/CSV/JSON) -- these are
      not meant to be inline-previewed by the composer, and forcing a
      download keeps the browser's own PDF/text viewer from ever rendering
      attacker-supplied bytes as a navigated-to page from this origin.
    """
    try:
        path, mime_type, display_filename = await attachment_service.download(
            current_user.user_id, request, attachment_id
        )
    except ResourceNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Not found") from exc

    inline = mime_type.startswith(_INLINE_MIME_PREFIXES)
    headers = {
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
        "Content-Disposition": _content_disposition(display_filename, inline=inline),
    }
    return FileResponse(path, media_type=mime_type, headers=headers)


__all__ = ["router"]
