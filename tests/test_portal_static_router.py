"""Static-router tests for the end-user SPA.

These exist because of a real bug that shipped with zero coverage: the router
resolved its dist directory by looking for ``index.html``, but Vite names the
emitted document after its rollup input, which is ``portal.html`` (see
dashboard/vite.portal.config.ts). The dist was therefore never resolvable and
the SPA 404'd even when built and packaged correctly, and neither the build
lane nor the security review caught it because nothing exercised this module.

The filename agreement between the Vite config and the router is asserted
directly below so the two cannot drift apart again silently.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from astrbot.portal.api import static_router

REPO_ROOT = Path(__file__).resolve().parents[1]
VITE_PORTAL_CONFIG = REPO_ROOT / "dashboard" / "vite.portal.config.ts"


@pytest.fixture
def dist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A built-looking dist dir wired in as the bundled location."""
    dist_dir = tmp_path / static_router.PORTAL_DIST_DIRNAME
    (dist_dir / "assets").mkdir(parents=True)
    (dist_dir / static_router.PORTAL_ENTRY_FILENAME).write_text(
        "<!doctype html><title>portal</title>", encoding="utf-8"
    )
    (dist_dir / "assets" / "app.js").write_text("console.log(1)", encoding="utf-8")

    monkeypatch.setattr(
        static_router, "get_bundled_portal_dist_path", lambda: dist_dir
    )
    # Point the data-dir override somewhere empty so it does not win.
    monkeypatch.setattr(
        static_router, "get_astrbot_data_path", lambda: str(tmp_path / "empty_data")
    )
    return dist_dir


def test_entry_filename_matches_vite_rollup_input():
    """The router's entry filename must equal the Vite build's HTML input.

    This is the assertion that would have caught the original bug.
    """
    config = VITE_PORTAL_CONFIG.read_text(encoding="utf-8")
    inputs = re.findall(r"""URL\(\s*['"]\./([\w.-]+\.html)['"]""", config)
    assert inputs, (
        f"could not find a rollup HTML input in {VITE_PORTAL_CONFIG.name}; "
        "if the build config moved, update this test rather than deleting it"
    )
    assert static_router.PORTAL_ENTRY_FILENAME in inputs, (
        f"static_router serves '{static_router.PORTAL_ENTRY_FILENAME}' but the "
        f"Vite build emits {inputs}. The backend would 404 the whole SPA."
    )


def test_dist_dir_resolves_when_entry_document_present(dist: Path):
    assert static_router.get_portal_dist_dir() == dist


def test_dist_dir_is_none_when_only_index_html_present(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An index.html alone is NOT a valid portal dist -- that was the bug."""
    dist_dir = tmp_path / static_router.PORTAL_DIST_DIRNAME
    dist_dir.mkdir(parents=True)
    (dist_dir / "index.html").write_text("<!doctype html>", encoding="utf-8")

    monkeypatch.setattr(
        static_router, "get_bundled_portal_dist_path", lambda: dist_dir
    )
    monkeypatch.setattr(
        static_router, "get_astrbot_data_path", lambda: str(tmp_path / "empty_data")
    )
    assert static_router.get_portal_dist_dir() is None


def test_dist_dir_is_none_when_unbuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(
        static_router,
        "get_bundled_portal_dist_path",
        lambda: tmp_path / "nonexistent",
    )
    monkeypatch.setattr(
        static_router, "get_astrbot_data_path", lambda: str(tmp_path / "empty_data")
    )
    assert static_router.get_portal_dist_dir() is None


def test_data_dir_override_wins_over_bundled(
    dist: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """An operator-provided dist must take precedence over the packaged one."""
    data_dir = tmp_path / "data"
    override = data_dir / static_router.PORTAL_DIST_DIRNAME
    override.mkdir(parents=True)
    (override / static_router.PORTAL_ENTRY_FILENAME).write_text(
        "<!doctype html><title>override</title>", encoding="utf-8"
    )

    monkeypatch.setattr(static_router, "get_astrbot_data_path", lambda: str(data_dir))
    assert static_router.get_portal_dist_dir() == override


@pytest.mark.asyncio
async def test_serve_index_returns_entry_document(dist: Path):
    response = await static_router.serve_portal_index(None)
    assert getattr(response, "status_code", 200) == 200
    assert Path(response.path).name == static_router.PORTAL_ENTRY_FILENAME


@pytest.mark.asyncio
async def test_serve_index_404s_gracefully_when_unbuilt(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """A missing frontend build must 404, never crash the dashboard app."""
    monkeypatch.setattr(static_router, "get_portal_dist_dir", lambda: None)
    response = await static_router.serve_portal_index(None)
    assert response.status_code == 404
