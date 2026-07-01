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


def _public_definitions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef]:
    """Return module definitions plus class methods covered by the teaching policy."""
    definitions: list[ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            definitions.append(node)
            if isinstance(node, ast.ClassDef):
                definitions.extend(
                    child
                    for child in node.body
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                )
    return definitions


def test_ipo_networking_is_isolated_to_sources_and_all_ipo_code_is_ui_free() -> None:
    """Permit HTTP only in source adapters and the exact IPO-003 downloader."""
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
                ipo_relative = path.relative_to(IPO_PACKAGE).as_posix()
                network_allowed = (
                    "sources" in path.relative_to(IPO_PACKAGE).parts
                    or ipo_relative == "documents/downloader.py"
                )
                # Keeping this exception file-specific matters: allowing the
                # whole documents package would let future parsers quietly add
                # unrelated network calls outside the reviewed trust boundary.
                if is_network and not network_allowed:
                    violations.append(f"{relative}:{node.lineno} imports network module {module}")
                if is_ui:
                    violations.append(f"{relative}:{node.lineno} imports UI module {module}")

    assert not violations, "IPO boundaries were violated:\n" + "\n".join(violations)


def test_ipo_owned_code_and_tests_keep_beginner_friendly_docstrings() -> None:
    """Prevent later IPO edits from silently eroding the requested teaching layer."""
    files = list(IPO_PACKAGE.rglob("*.py"))
    files.extend(
        [
            ROOT / "backend" / "jobs" / "scan_ipo_filings.py",
            ROOT / "backend" / "storage" / "ipo_repository.py",
        ]
    )
    files.extend((ROOT / "migrations" / "versions").glob("*ipo*.py"))
    files.extend((ROOT / "tests").glob("test_ipo*.py"))
    files.append(ROOT / "tests" / "test_scan_ipo_filings_job.py")

    missing: list[str] = []
    for path in sorted(set(files)):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if ast.get_docstring(tree) is None:
            missing.append(f"{path.relative_to(ROOT).as_posix()}:1 module")
        for definition in _public_definitions(tree):
            if ast.get_docstring(definition) is None:
                missing.append(
                    f"{path.relative_to(ROOT).as_posix()}:{definition.lineno} "
                    f"{definition.name}"
                )

    assert not missing, "IPO teaching docstrings are missing:\n" + "\n".join(missing)
