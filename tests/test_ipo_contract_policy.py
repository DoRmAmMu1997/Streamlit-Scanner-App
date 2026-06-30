"""Static IPO-001 boundary guards."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IPO_PACKAGE = ROOT / "backend" / "ipo"
BANNED_NETWORK_IMPORT_ROOTS = {
    "dhanhq",
    "httpx",
    "playwright",
    "requests",
    "selenium",
    "urllib.request",
}
BANNED_UI_IMPORT_ROOTS = {"streamlit"}


def test_ipo_networking_is_isolated_to_sources_and_all_ipo_code_is_ui_free() -> None:
    """IPO-002 permits HTTP only in sources and never permits Streamlit."""
    violations: list[str] = []
    files = sorted(IPO_PACKAGE.rglob("*.py"))
    assert {path.name for path in files} >= {
        "models.py",
        "repository.py",
        "scorecard.py",
        "verdict.py",
    }

    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                modules.append(node.module)
            for module in modules:
                is_network = any(
                    module == banned or module.startswith(f"{banned}.")
                    for banned in BANNED_NETWORK_IMPORT_ROOTS
                )
                is_ui = any(
                    module == banned or module.startswith(f"{banned}.")
                    for banned in BANNED_UI_IMPORT_ROOTS
                )
                relative = path.relative_to(ROOT).as_posix()
                if is_network and "sources" not in path.relative_to(IPO_PACKAGE).parts:
                    violations.append(f"{relative}:{node.lineno} imports network module {module}")
                if is_ui:
                    violations.append(f"{relative}:{node.lineno} imports UI module {module}")

    assert not violations, "IPO boundaries were violated:\n" + "\n".join(violations)
