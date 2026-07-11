"""Static architecture and teaching-documentation guards for the IPO subsystem.

These tests inspect source syntax rather than executing business logic. They keep
two easy-to-erode design promises visible in CI: only reviewed adapters may use
network clients, and every IPO-owned definition must explain itself to a reader
who is still learning the repository.
"""

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

DocumentedDefinition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef

# Most targets are wholly IPO-owned, so every named definition in them belongs
# to this teaching pass. Shared files are handled separately below to avoid
# rewriting unrelated scanner, authentication, or persistence code.
FULL_DOCUMENTATION_TARGETS = (
    ROOT / "backend" / "jobs" / "scan_ipo_filings.py",
    ROOT / "backend" / "storage" / "ipo_repository.py",
    ROOT / "tests" / "test_scan_ipo_filings_job.py",
    ROOT / "tests" / "test_app_ipo_manual_page.py",
    ROOT / "ui" / "ipo_manual_page.py",
)
SHARED_DOCUMENTATION_TARGETS: dict[Path, frozenset[str]] = {
    ROOT / "backend" / "config" / "settings.py": frozenset({"ipo_document_dir"}),
    ROOT / "backend" / "storage" / "models.py": frozenset(
        {
            "IpoIssue",
            "IpoDocument",
            "IpoFinancial",
            "IpoManualExtraction",
            "IpoManualFinancialPeriod",
            "IpoManualPeerValuation",
            "IpoSubscription",
            "IpoScore",
            "IpoRecommendation",
        }
    ),
    ROOT / "tests" / "test_scan_storage_migrations.py": frozenset(
        {
            "test_ipo002_downgrade_refuses_to_discard_ingested_identity",
            "test_ipo003_downgrade_refuses_to_discard_download_provenance",
            "test_ipo004_downgrade_refuses_to_discard_manual_revisions",
        }
    ),
    ROOT / "tests" / "test_app_orchestration.py": frozenset(
        {"test_auth_disabled_local_owner_can_open_admin_ipo_extraction"}
    ),
}


def _documented_definitions(tree: ast.AST) -> list[DocumentedDefinition]:
    """Return every named definition covered by the teaching policy.

    ``ast.walk`` deliberately includes methods and nested test callbacks, not
    just public top-level functions. Nested fakes often model the hardest parts
    of the workflow—an HTTP failure, concurrent source edit, or rollback—so a
    beginner needs an explanation there just as much as on the outer test.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]


def _full_documentation_targets() -> list[Path]:
    """Build the deterministic list of files wholly owned by the IPO subsystem.

    Keeping discovery here prevents a new IPO module, migration, or focused test
    from bypassing the documentation guard merely because this test's file list
    was not manually updated in the same pull request.
    """
    files = list(IPO_PACKAGE.rglob("*.py"))
    files.extend(FULL_DOCUMENTATION_TARGETS)
    files.extend((ROOT / "migrations" / "versions").glob("*ipo*.py"))
    files.extend((ROOT / "tests").glob("test_ipo*.py"))
    return sorted(set(files))


def _shared_ipo_definitions(path: Path, tree: ast.Module) -> list[DocumentedDefinition]:
    """Select IPO-owned definitions from a file shared with other subsystems.

    Class selection includes its methods because the class owns their teaching
    contract. Function selection is name-based and intentionally narrow: the
    wider file may contain mature code unrelated to IPO-001/002/003.
    """
    selected_names = SHARED_DOCUMENTATION_TARGETS[path]
    selected: list[DocumentedDefinition] = []
    for node in _documented_definitions(tree):
        if node.name in selected_names:
            selected.append(node)
            if isinstance(node, ast.ClassDef):
                selected.extend(
                    child
                    for child in ast.walk(node)
                    if child is not node
                    and isinstance(
                        child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
                    )
                )
    return selected


def test_ipo_networking_is_isolated_to_sources_and_all_ipo_code_is_ui_free() -> None:
    """Keep network and Streamlit imports inside their reviewed trust boundaries.

    IPO-002 may fetch listing metadata from its source adapter, and IPO-003 may
    fetch one prospectus through the exact downloader module. Everything else in
    the domain package must remain offline and UI-independent so scoring and
    persistence are deterministic and reusable from jobs or tests.
    """
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
            # Skipping non-import nodes up front also narrows the type so
            # ``node.lineno`` below is known to exist (QUAL-007).
            if not isinstance(node, (ast.Import, ast.ImportFrom)):
                continue
            modules: list[str] = []
            if isinstance(node, ast.Import):
                modules.extend(alias.name for alias in node.names)
            elif node.module:
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
    """Require a docstring on every IPO-owned definition, including test fakes.

    This structural check proves coverage, not writing quality. Human review is
    still responsible for rejecting tautologies such as "provide the foo step";
    the AST guard simply ensures no future helper silently loses the teaching
    layer altogether.
    """
    missing: list[str] = []
    for path in _full_documentation_targets():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if ast.get_docstring(tree) is None:
            missing.append(f"{path.relative_to(ROOT).as_posix()}:1 module")
        for definition in _documented_definitions(tree):
            if ast.get_docstring(definition) is None:
                missing.append(
                    f"{path.relative_to(ROOT).as_posix()}:{definition.lineno} "
                    f"{definition.name}"
                )

    for path in sorted(SHARED_DOCUMENTATION_TARGETS):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for definition in _shared_ipo_definitions(path, tree):
            if ast.get_docstring(definition) is None:
                missing.append(
                    f"{path.relative_to(ROOT).as_posix()}:{definition.lineno} "
                    f"{definition.name}"
                )

    assert not missing, "IPO teaching docstrings are missing:\n" + "\n".join(missing)
