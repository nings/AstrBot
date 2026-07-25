"""
Custom Hatchling build hook.

Only runs when the environment variable ASTRBOT_BUILD_DASHBOARD=1 is set,
so that `uv sync` / editable installs are never affected.

Usage:
    ASTRBOT_BUILD_DASHBOARD=1 uv build

When enabled, this hook:
1. Runs `npm run build` inside the `dashboard/` directory.
2. Copies the resulting `dashboard/dist/` tree into
   `astrbot/dashboard/dist/` so the static assets are shipped
   inside the Python wheel.
3. Does the same for the end-user chat SPA: `npm run build:portal`
   produces `dashboard/dist-portal/`, which is copied into
   `astrbot/dashboard/dist-portal/` (the path
   `astrbot.portal.api.static_router.get_bundled_portal_dist_path()` resolves).
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        # Only run when explicitly requested (e.g. during CI / release builds).
        # This prevents `uv sync` / editable installs from triggering npm.
        if os.environ.get("ASTRBOT_BUILD_DASHBOARD", "").strip() != "1":
            return

        root = Path(self.root)
        dashboard_src = root / "dashboard"

        if not dashboard_src.exists():
            print(
                "[hatch_build] 'dashboard/' directory not found – skipping dashboard build.",
                file=sys.stderr,
            )
            return

        # ── Install Node dependencies if node_modules is absent ─────────────
        if not (dashboard_src / "node_modules").exists():
            print("[hatch_build] Installing dashboard Node dependencies...")
            subprocess.run(
                ["npm", "install"],
                cwd=dashboard_src,
                check=True,
            )

        # Both SPAs are built from the same source tree with separate Vite
        # configs, so each needs its own build + copy pass. `dist-portal` is the
        # end-user chat SPA; `dist` is the operator dashboard.
        self._build_and_copy(root, dashboard_src, "build", "dist", "dashboard")
        self._build_and_copy(
            root, dashboard_src, "build:portal", "dist-portal", "end-user portal"
        )

    def _build_and_copy(
        self,
        root: Path,
        dashboard_src: Path,
        npm_script: str,
        dist_dirname: str,
        label: str,
    ) -> None:
        """Run one `npm run <script>` and copy its dist into the package tree."""
        dist_src = dashboard_src / dist_dirname
        dist_target = root / "astrbot" / "dashboard" / dist_dirname

        print(f"[hatch_build] Building Vue {label} (npm run {npm_script})...")
        subprocess.run(
            ["npm", "run", npm_script],
            cwd=dashboard_src,
            check=True,
        )

        if not dist_src.exists():
            print(
                f"[hatch_build] dashboard/{dist_dirname} not found after build – skipping copy.",
                file=sys.stderr,
            )
            return

        # ── Copy into the Python package tree ────────────────────────────────
        if dist_target.exists():
            shutil.rmtree(dist_target)
        shutil.copytree(dist_src, dist_target)
        print(f"[hatch_build] {label} dist copied → {dist_target.relative_to(root)}")
