"""Capability-wall guards that import-linter cannot express.

The bulk of the webui capability wall lives in ``pyproject.toml``'s
``[tool.importlinter]`` contracts. One boundary cannot go there:
``astrbot/core/utils/`` has no ``__init__.py`` (implicit namespace package, as
is ``astrbot/core/agent/``), so grimp indexes zero modules under
``astrbot.core.utils.*`` and a ``forbidden_modules`` entry naming
``astrbot.core.utils.shared_preferences`` passes vacuously -- it is never
actually checked and no warning is emitted.

Rather than add an ``__init__.py`` to core just to satisfy the linter, the
``shared_preferences`` boundary is enforced here by parsing the AST of every
module under ``astrbot/webui/``.

See design/webui-multiuser-plan.md section 3.2.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

WEBUI_ROOT = Path(__file__).resolve().parents[1] / "astrbot" / "webui"

# core_facade is the single sanctioned door to core state.
FACADE_RELPATH = Path("services") / "core_facade.py"

# Modules only core_facade may reach, which import-linter cannot see.
NAMESPACE_FORBIDDEN_MODULES = frozenset({"astrbot.core.utils.shared_preferences"})


def _webui_modules() -> list[Path]:
    return sorted(p for p in WEBUI_ROOT.rglob("*.py"))


def _imported_modules(source: str) -> set[str]:
    """Every module name a source file imports, normalized to dotted paths.

    ``from a.b import c`` yields both ``a.b`` and ``a.b.c`` because the name
    being imported may itself be a submodule rather than an attribute, and we
    cannot tell which from the AST alone. Over-approximating is the safe
    direction for a boundary check.
    """
    tree = ast.parse(source)
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            # Relative imports stay inside astrbot.webui, so they are never
            # a core boundary violation.
            if node.level or not node.module:
                continue
            found.add(node.module)
            for alias in node.names:
                found.add(f"{node.module}.{alias.name}")
    return found


def test_webui_package_is_discoverable():
    """Guard the guard: if the layout moves, fail loudly instead of passing empty."""
    modules = _webui_modules()
    assert modules, f"no Python modules found under {WEBUI_ROOT}"
    assert (WEBUI_ROOT / FACADE_RELPATH).is_file(), (
        f"{FACADE_RELPATH} is missing -- the capability wall has no door"
    )


@pytest.mark.parametrize(
    "module_path", _webui_modules(), ids=lambda p: str(p.relative_to(WEBUI_ROOT))
)
def test_only_core_facade_imports_namespace_forbidden_modules(module_path: Path):
    """Only core_facade.py may import shared_preferences."""
    relpath = module_path.relative_to(WEBUI_ROOT)
    imported = _imported_modules(module_path.read_text(encoding="utf-8"))
    violations = sorted(imported & NAMESPACE_FORBIDDEN_MODULES)

    if relpath == FACADE_RELPATH:
        return

    assert not violations, (
        f"astrbot/webui/{relpath} imports {violations}, which only "
        f"astrbot/webui/{FACADE_RELPATH} may import. Route the call through "
        f"core_facade instead (see the plan's section 3.2 capability wall)."
    )


def test_guard_actually_detects_a_violation(tmp_path: Path):
    """A boundary check that cannot fail is worthless -- prove this one can."""
    offender = tmp_path / "offender.py"
    offender.write_text(
        "from astrbot.core.utils.shared_preferences import SharedPreferences\n",
        encoding="utf-8",
    )
    imported = _imported_modules(offender.read_text(encoding="utf-8"))
    assert imported & NAMESPACE_FORBIDDEN_MODULES, (
        "the AST walker failed to detect a deliberate violation"
    )
