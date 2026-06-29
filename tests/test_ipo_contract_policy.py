"""Static IPO-001 boundary guards."""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IPO_PACKAGE = ROOT / "backend" / "ipo"
BANNED_IMPORT_ROOTS = {
    "dhanhq",
    "httpx",
    "playwright",
    "requests",
    "selenium",
    "streamlit",
    "urllib.request",
}


def test_ipo_domain_has_no_network_or_ui_dependencies() -> None:
    """IPO-001 is a pure backend contract: no scraper, HTTP client, or Streamlit."""
    violations: list[str] = []
    files = sorted(IPO_PACKAGE.glob("*.py"))
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
                if any(
                    module == banned or module.startswith(f"{banned}.")
                    for banned in BANNED_IMPORT_ROOTS
                ):
                    relative = path.relative_to(ROOT).as_posix()
                    violations.append(f"{relative}:{node.lineno} imports {module}")

    assert not violations, "IPO-001 must remain offline and UI-free:\n" + "\n".join(violations)
