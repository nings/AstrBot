import os
from pathlib import Path

import pytest

from astrbot.api.message_components import File, Image, Reply
from astrbot.core.platform.sources.webchat import attachment_staging
from astrbot.core.tools.computer_tools import fs

UMO = "webchat:FriendMessage:webchat!alice!conversation-1"
OTHER_UMO = "webchat:FriendMessage:webchat!bob!conversation-2"


@pytest.fixture
def staging_env(tmp_path, monkeypatch):
    """Point both the attachment dir and the workspace root at ``tmp_path``."""
    attachments = tmp_path / "attachments"
    attachments.mkdir()
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    monkeypatch.setattr(
        attachment_staging, "attachments_root", lambda: attachments.resolve()
    )

    async def fake_resolve(umo, db=None):
        return (workspaces / umo.replace(":", "_").replace("!", "_")).resolve()

    monkeypatch.setattr(attachment_staging, "resolve_workspace_root_for_umo", fake_resolve)
    monkeypatch.setattr(fs.computer_util, "get_astrbot_workspaces_path", lambda: str(workspaces))
    return attachments, workspaces


def _read_is_allowed(path: str, umo: str, workspace_root: Path) -> bool:
    """Run the real restricted-mode read check a non-admin user gets."""
    try:
        fs._normalize_rw_path(
            path,
            restricted=True,
            local_env=True,
            umo=umo,
            write=False,
            current_workspace_root=workspace_root,
        )
    except PermissionError:
        return False
    return True


async def _workspace_of(umo: str) -> Path:
    return await attachment_staging.resolve_workspace_root_for_umo(umo)


@pytest.mark.asyncio
async def test_shared_attachment_dir_is_unreadable_without_staging(staging_env):
    """The failure this module exists to fix: uploads are outside the allowlist."""
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")

    assert not _read_is_allowed(str(upload), UMO, await _workspace_of(UMO))


@pytest.mark.asyncio
async def test_staged_file_becomes_readable(staging_env):
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    comp = File(name="notice.pdf", file=str(upload))

    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert comp.file_ != str(upload)
    assert _read_is_allowed(comp.file_, UMO, await _workspace_of(UMO))
    assert Path(comp.file_).read_bytes() == b"%PDF-1.4\n"


@pytest.mark.asyncio
async def test_staged_file_is_a_copy_not_a_hard_link(staging_env):
    """Hard links are rejected by ``fs._reject_multi_link_file``."""
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    comp = File(name="notice.pdf", file=str(upload))

    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert os.stat(comp.file_).st_nlink == 1


@pytest.mark.asyncio
async def test_images_and_quoted_attachments_are_staged(staging_env):
    attachments, _ = staging_env
    upload = attachments / "abc123-scan.png"
    upload.write_bytes(b"\x89PNG\r\n\x1a\n")
    quoted = attachments / "def456-prior.pdf"
    quoted.write_bytes(b"%PDF-1.4\n")

    image = Image.fromFileSystem(str(upload))
    reply = Reply(id="1", chain=[File(name="prior.pdf", file=str(quoted))])
    await attachment_staging.stage_webchat_attachments([image, reply], UMO)

    workspace = await _workspace_of(UMO)
    assert _read_is_allowed(image.path, UMO, workspace)
    assert image.file == Path(image.path).as_uri()
    assert _read_is_allowed(reply.chain[0].file_, UMO, workspace)


@pytest.mark.asyncio
async def test_another_users_staged_file_stays_unreadable(staging_env):
    """Staging must not recreate the cross-tenant exposure it avoids."""
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    comp = File(name="notice.pdf", file=str(upload))

    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert not _read_is_allowed(comp.file_, OTHER_UMO, await _workspace_of(OTHER_UMO))


@pytest.mark.asyncio
async def test_paths_outside_the_attachment_dir_are_untouched(staging_env, tmp_path):
    elsewhere = tmp_path / "plugin-provided.txt"
    elsewhere.write_text("x", encoding="utf-8")
    comp = File(name="plugin-provided.txt", file=str(elsewhere))

    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert comp.file_ == str(elsewhere)


@pytest.mark.asyncio
async def test_restaging_does_not_recopy(staging_env):
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    comp = File(name="notice.pdf", file=str(upload))

    await attachment_staging.stage_webchat_attachments([comp], UMO)
    first = Path(comp.file_).stat().st_mtime_ns
    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert Path(comp.file_).stat().st_mtime_ns == first


@pytest.mark.asyncio
async def test_staging_failure_leaves_the_original_path(staging_env, monkeypatch):
    """A broken copy must degrade to pre-staging behavior, not raise."""
    attachments, _ = staging_env
    upload = attachments / "abc123-notice.pdf"
    upload.write_bytes(b"%PDF-1.4\n")
    comp = File(name="notice.pdf", file=str(upload))

    def boom(_src, _dst):
        raise OSError("disk full")

    monkeypatch.setattr(attachment_staging, "_copy_if_needed", boom)
    await attachment_staging.stage_webchat_attachments([comp], UMO)

    assert comp.file_ == str(upload)
