"""Stage webchat attachments into the session workspace.

Uploaded files land in the shared, flat ``data/attachments/`` directory (see
``webchat_event.attachments_dir``). That directory is not on the read allowlist
the agent's filesystem tools enforce for non-admin users -- see
``_read_allowed_roots`` in ``astrbot/core/tools/computer_tools/fs.py``, which
permits only ``data/skills``, plugin skills, the session workspace, and the two
temp roots. Every Portal end user is a plain ``member`` (``role`` is only
promoted to ``admin`` for ids listed in ``admins_id``), so the model gets a
``PermissionError`` the moment it tries to read the file the user just
uploaded.

Widening that allowlist to cover ``data/attachments/`` is not an option: the
directory is shared flat across every session of every user, so one Portal
user's agent could enumerate and read another's uploads.

Instead each attachment is copied into the session's own workspace, which *is*
on the allowlist and is already scoped per umo. The message component is
rewritten in place to point at the staged copy, so everything downstream -- the
``[File Attachment: ...]`` prompt reference built in ``astr_main_agent``, the
read tool's allowlist check, and its converted-text cache -- operates inside
the workspace.

It has to be a real copy. A hard link is rejected by
``fs._reject_multi_link_file`` (``st_nlink > 1`` is treated as a possible alias
for content outside the allowlist) and a symlink is resolved back to
``data/attachments/`` before the allowlist check runs. Both would reintroduce
the very failure this module exists to fix.

Staging is best-effort: any failure leaves the component pointing at the
original path, which is exactly the pre-staging behavior. A broken copy must
never take down message handling.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path, PurePosixPath

from astrbot.api import logger
from astrbot.core.message.components import File, Image, Record, Reply, Video
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.workspace import resolve_workspace_root_for_umo

STAGING_DIR_NAME = "uploads"
"""Subdirectory of the session workspace that staged attachments land in."""

_MAX_REPLY_DEPTH = 3
"""Quoted messages nest; bound the walk rather than trusting the payload."""


def attachments_root() -> Path:
    """Return the shared attachment directory staging reads out of."""
    return Path(get_astrbot_data_path(), "attachments").resolve(strict=False)


def _is_under(path: Path, root: Path) -> bool:
    return path == root or path.is_relative_to(root)


def _copy_if_needed(src: Path, dst: Path) -> None:
    """Copy ``src`` to ``dst`` unless an identical copy is already there.

    Runs on a worker thread -- attachments are capped at 20 MiB by the portal's
    upload policy, which is enough to matter on the event loop.
    """
    try:
        dst_stat = dst.stat()
        src_stat = src.stat()
        if dst_stat.st_size == src_stat.st_size and dst_stat.st_mtime >= (
            src_stat.st_mtime
        ):
            return
    except FileNotFoundError:
        pass

    dst.parent.mkdir(parents=True, exist_ok=True)
    # Copy to a per-process temp name first so a concurrent reader never sees a
    # half-written file under the final path.
    tmp = dst.with_name(f".{dst.name}.{os.getpid()}.part")
    try:
        shutil.copy2(src, tmp)
        os.replace(tmp, dst)
    finally:
        tmp.unlink(missing_ok=True)


async def _stage_path(path_str: str, umo: str) -> str | None:
    """Copy one attachment into the workspace, returning the staged path.

    Returns ``None`` when the path needs no staging (not an attachment, or
    already inside the workspace) or when staging failed.
    """
    if not path_str:
        return None

    src = Path(path_str).resolve(strict=False)
    if not _is_under(src, attachments_root()):
        # Records converted to wav, plugin-provided files and anything a
        # non-webchat path produced already live outside the shared directory.
        return None
    if not src.is_file():
        return None

    try:
        workspace_root = await resolve_workspace_root_for_umo(umo)
    except Exception:
        logger.exception(
            "[attachment-staging] failed to resolve the workspace root for %s", umo
        )
        return None

    # The on-disk name is server-generated (uuid-prefixed) so it is already
    # unique and safe, but it arrives here as untrusted text; keep the basename
    # only so nothing can climb out of the staging directory.
    basename = PurePosixPath(src.name.replace("\\", "/")).name
    if basename in {"", ".", ".."}:
        return None

    dst = (workspace_root / STAGING_DIR_NAME / basename).resolve(strict=False)
    if not _is_under(dst, workspace_root.resolve(strict=False)):
        return None

    try:
        await asyncio.to_thread(_copy_if_needed, src, dst)
    except Exception:
        logger.exception("[attachment-staging] failed to stage %s for %s", src, umo)
        return None

    return str(dst)


async def stage_webchat_attachments(
    chain: list,
    umo: str,
    *,
    _depth: int = 0,
) -> None:
    """Repoint every media component in ``chain`` at a workspace-local copy.

    Mutates components in place so that callers downstream of message
    conversion -- which read ``File.file_`` / ``Image.path`` and friends -- see
    only workspace paths and never the shared attachment directory.

    Args:
        chain: Message components, as built by ``parse_webchat_message_parts``.
        umo: Unified message origin, used to resolve the session workspace.
    """
    if not chain or not umo:
        return

    for comp in chain:
        if isinstance(comp, Reply):
            if comp.chain and _depth < _MAX_REPLY_DEPTH:
                await stage_webchat_attachments(comp.chain, umo, _depth=_depth + 1)
            continue

        if isinstance(comp, File):
            # `File.file` is a property that may trigger a download; the local
            # path lives in `file_`.
            staged = await _stage_path(comp.file_ or "", umo)
            if staged:
                comp.file_ = staged
            continue

        if isinstance(comp, (Image, Video)):
            # Built by `fromFileSystem`, so `file` is a file:// URI and `path`
            # is the plain path. Keep both consistent.
            staged = await _stage_path(comp.path or "", umo)
            if staged:
                comp.path = staged
                comp.file = Path(staged).as_uri()
            continue

        if isinstance(comp, Record):
            staged = await _stage_path(comp.file or "", umo)
            if staged:
                comp.file = staged
                comp.url = staged
                if comp.path:
                    comp.path = staged
            continue
