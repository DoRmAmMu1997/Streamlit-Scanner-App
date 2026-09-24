"""Repository policy tests for supply-chain hygiene.

These tests intentionally read documentation and CI files. They protect the
project's security workflow from quiet drift: if someone removes dependency
auditing, static analysis, or the constraints install path, the normal pytest
suite will call that out before a PR lands.
"""

from __future__ import annotations

import copy
import re
import tomllib
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
QUAL_007_IGNORE_ERRORS_BASELINE = frozenset(
    {
        "test_app_comparison_page",
        "test_app_validation_page",
        "test_auth_session",
        "test_daily_data_loader",
        "test_daily_scan_job",
        "test_dhan_client",
        "test_forward_return_service",
        "test_indicators",
        "test_ipo_document_downloader",
        "test_ipo_models",
        "test_ipo_ratio_engine",
        "test_ipo_repository",
        "test_ipo_scorecard",
        "test_notifications_channels",
        "test_notifications_report",
        "test_notifications_service",
        "test_pdf_reader",
        "test_real_screeners",
        "test_result_contract",
        "test_scan_run_integration",
        "test_scan_service",
        "test_scan_storage_repository",
        "test_scanner_base",
        "test_scoring_model",
        "test_screener_in_client",
        "test_screener_registry",
        "test_sixty_seven_agent",
        "test_sixty_seven_search_client",
        "test_technical_analysis_agent",
    }
)
CI_COMMANDS = (
    "python -m pre_commit validate-config .pre-commit-config.yaml",
    "python -m pytest -q --cov=app --cov=backend --cov=screeners --cov=ui --cov-fail-under=89",
    "python -m compileall -q app.py backend screeners ui tests",
    "python -m ruff check app.py backend screeners ui Dependencies tests",
    "python -m mypy",
    "python -m bandit -r app.py backend screeners ui Dependencies -q",
    "python -m pip_audit -r constraints.txt",
    "docker build --tag streamlit-scanner-app:ci .",
    "docker compose config",
    "docker compose up --build --wait --wait-timeout 180",
    "docker compose down --volumes --remove-orphans",
)
DEVELOPMENT_TOOLS = frozenset(
    {"pytest", "pytest-cov", "ruff", "bandit", "pip-audit", "mypy", "pre-commit"}
)


def _assert_qual_007_ignore_errors_only_shrinks(config: dict) -> None:
    """Keep QUAL-007's temporary mypy debt list from silently growing.

    Beginner note: the override is a migration aid, not a permanent escape
    hatch. Existing entries may be removed as tests gain types, but adding a
    new test module would hide fresh errors from CI and must fail this policy
    test.
    """
    mypy = config["tool"]["mypy"]
    assert "tests" in mypy["files"]

    ignored_overrides = [
        override
        for override in mypy.get("overrides", [])
        if override.get("ignore_errors") is True
    ]
    assert len(ignored_overrides) == 1

    modules = ignored_overrides[0]["module"]
    assert len(modules) == len(set(modules)), "ignore_errors modules must be unique"
    assert set(modules) <= QUAL_007_IGNORE_ERRORS_BASELINE
    for module in modules:
        assert (ROOT / "tests" / f"{module}.py").is_file(), module


def test_qual_007_mypy_ignore_errors_debt_can_only_shrink():
    """The checked-in mypy override must stay within its reviewed baseline."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)

    _assert_qual_007_ignore_errors_only_shrinks(config)


def test_qual_007_mypy_ignore_errors_guard_rejects_new_modules():
    """Prove the policy guard fails if a future edit expands the debt list."""
    with (ROOT / "pyproject.toml").open("rb") as handle:
        config = tomllib.load(handle)
    expanded = copy.deepcopy(config)
    ignored_override = next(
        override
        for override in expanded["tool"]["mypy"]["overrides"]
        if override.get("ignore_errors") is True
    )
    ignored_override["module"].append("test_new_untyped_debt")

    with pytest.raises(AssertionError):
        _assert_qual_007_ignore_errors_only_shrinks(expanded)


def _ruff_pin_from_constraints() -> str:
    """Return the exact ruff version CI installs, e.g. ``0.16.3``."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    match = re.search(r"^ruff==(?P<version>[^\s#]+)\s*$", text, flags=re.MULTILINE)
    assert match is not None, "constraints.txt must pin ruff with an exact =="
    return match.group("version")


def _ruff_pre_commit_rev(config: dict) -> str:
    """Return the rev the local ruff hook is pinned to, without its ``v``."""
    repos = [
        repository
        for repository in config["repos"]
        if repository["repo"].rstrip("/").endswith("astral-sh/ruff-pre-commit")
    ]
    assert len(repos) == 1, "expected exactly one ruff-pre-commit repo entry"
    rev = str(repos[0]["rev"])
    assert rev.startswith("v"), f"expected a vX.Y.Z tag, got {rev!r}"
    return rev[1:]


def test_pre_commit_ruff_rev_matches_the_constraints_pin():
    """The commit hook must lint with the same ruff version CI installs.

    Beginner note (QUAL-008):
    `.pre-commit-config.yaml` pins its own copy of ruff by git tag, while CI
    installs the `ruff==` pin from `constraints.txt`. Nothing tied the two
    together, and they drifted a whole minor version apart (hook v0.15.1 vs
    CI 0.16.3) - so the hook could pass code that CI then rejected, which
    defeats the point of having a commit-time check at all. The config file
    already asked for this invariant in a comment; this test is what actually
    holds it.
    """
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))

    assert _ruff_pre_commit_rev(config) == _ruff_pin_from_constraints()


def test_pre_commit_ruff_rev_guard_rejects_a_drifted_pin():
    """Prove the guard fails when the hook and the constraints pin disagree."""
    config = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    drifted = copy.deepcopy(config)
    for repository in drifted["repos"]:
        if repository["repo"].rstrip("/").endswith("astral-sh/ruff-pre-commit"):
            repository["rev"] = "v0.0.1"

    assert _ruff_pre_commit_rev(drifted) != _ruff_pin_from_constraints()


def test_ci_workflow_runs_quality_and_dependency_security_checks():
    """CI should run the same checks maintainers run locally."""
    workflow = ROOT / ".github" / "workflows" / "quality-and-security.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in text
    assert "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt" in text
    assert 'python-version: ["3.11", "3.12"]' in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert (
        "python -m pytest -q --cov=app --cov=backend --cov=screeners --cov=ui "
        "--cov-fail-under=89"
        in text
    )
    assert "python -m compileall -q app.py backend screeners ui tests" in text
    assert "python -m ruff check app.py backend screeners ui Dependencies tests" in text
    assert "python -m mypy" in text
    assert "python -m bandit -r app.py backend screeners ui Dependencies -q" in text
    assert "python -m pip_audit -r constraints.txt" in text
    assert "docker build --tag streamlit-scanner-app:ci ." in text
    assert "Copy Compose environment template" in text
    assert "Copy Streamlit secrets template" in text
    assert "docker compose config" in text
    assert "docker compose up --build --wait --wait-timeout 180" in text
    assert "docker compose down --volumes --remove-orphans" in text
    assert "python -m pip_audit -r requirements.txt -r requirements-dev.txt" not in text


def test_pre_commit_configuration_is_non_rewriting():
    """Local hooks should catch mistakes without silently editing source files."""
    config_path = ROOT / ".pre-commit-config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    hooks = [
        hook
        for repository in config["repos"]
        for hook in repository.get("hooks", [])
    ]
    hooks_by_id = {hook["id"]: hook for hook in hooks}

    assert {
        "ruff",
        "check-merge-conflict",
        "check-yaml",
        "check-added-large-files",
        "debug-statements",
    } <= hooks_by_id.keys()
    assert hooks_by_id["ruff"]["files"] == (
        r"^(app\.py|backend/|screeners/|ui/|Dependencies/|tests/)"
    )
    assert all("--fix" not in hook.get("args", []) for hook in hooks)


def test_constraints_pin_direct_runtime_and_developer_dependencies():
    """Direct dependencies should have a documented, repeatable pin set."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    required_names = [
        "streamlit",
        "authlib",
        "pandas",
        "numpy",
        "pyarrow",
        "sqlalchemy",
        "alembic",
        "psycopg",
        "psycopg-binary",
        "requests",
        "urllib3",
        "python-dotenv",
        "dhanhq",
        "pyyaml",
        "beautifulsoup4",
        "lxml",
        "pdfplumber",
        "claude-agent-sdk",
        "pytest",
        "ruff",
        "bandit",
        "pip-audit",
        "mypy",
        "types-requests",
        "types-PyYAML",
        "pandas-stubs",
        "types-pytz",
        "pytest-cov",
        "pre-commit",
    ]

    for name in required_names:
        assert re.search(rf"^{re.escape(name)}==", text, flags=re.IGNORECASE | re.MULTILINE), name


# Minimum safe versions for pins that were bumped to escape a known advisory.
# These are *floors*, not exact pins: a maintenance PR may move any of them
# forward, but never back below the version where the issue was fixed.
SECURITY_FLOOR_PINS = {
    "python-dotenv": (1, 2, 2),
    "lxml": (6, 1, 0),
    "pytest": (9, 0, 3),
}


def _pinned_version(text: str, name: str) -> tuple[int, ...]:
    """Return a constraints.txt pin as a comparable integer tuple.

    Deliberately hand-rolled rather than using ``packaging``: this module is the
    supply-chain guard, so it should not itself depend on a distribution that is
    not in ``constraints.txt``. Only the leading numeric components are compared,
    which is all these floors need (a date-suffixed stub pin like
    ``2.3.3.260113`` still parses fine).
    """
    match = re.search(rf"^{re.escape(name)}==([0-9][0-9.]*)", text, flags=re.MULTILINE)
    assert match, f"{name} must be pinned in constraints.txt"
    return tuple(int(part) for part in match.group(1).rstrip(".").split("."))


def test_constraints_use_security_reviewed_dependency_versions():
    """Known-vulnerable direct pins must not re-enter the installed environment."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")

    for name, floor in SECURITY_FLOOR_PINS.items():
        pinned = _pinned_version(text, name)
        assert pinned >= floor, (
            f"{name} is pinned at {pinned}, below the security floor {floor}"
        )


def test_runtime_requirements_install_the_documented_postgres_driver():
    """The documented psycopg SQLAlchemy URL must work after normal setup."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert re.search(r"^psycopg\[binary\]$", text, flags=re.IGNORECASE | re.MULTILINE)


def test_developer_tools_stay_out_of_the_runtime_requirements():
    """Verification tooling must not ship inside the production image.

    Beginner note (SEC-004):
    `Dockerfile` installs `requirements.txt` and nothing else, so every name in
    that file lands in the deployed container. `pytest` was listed there under a
    "Test runner." heading as well as in `requirements-dev.txt`, so the test
    runner and its dependency tree were shipped to production for no benefit.
    Each of the names below has a legitimate home in `requirements-dev.txt`; the
    point of this guard is that they only have one home.
    """
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    dev = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")

    _assert_developer_tools_stay_out_of_runtime_requirements(runtime, dev)


def _normalize_requirement_name(name: str) -> str:
    """Return the canonical spelling used when comparing requirement names.

    Beginner note: Python package names are case-insensitive, and packaging
    treats runs of dots, hyphens, and underscores as equivalent. Canonicalizing
    them before comparison prevents a policy bypass through cosmetic spelling.

    Args:
        name: The project name token extracted from a requirements line.

    Returns:
        A case-folded name with equivalent separators represented as hyphens.
    """
    return re.sub(r"[-_.]+", "-", name).casefold()


def _requirement_names(text: str) -> set[str]:
    """Extract normalized project names from simple requirements-file text.

    Beginner note: this guard only needs the project token, not dependency
    resolution. Removing comments and environment markers keeps the check small
    while still covering the requirement forms maintainers use. The leading-name
    match naturally leaves extras and version syntax out of the name; lines
    beginning with an option are ignored because they do not name a project.

    Args:
        text: Requirements-file contents to inspect.

    Returns:
        The normalized project names found in the supplied text.
    """
    names: set[str] = set()
    for raw_line in text.splitlines():
        line = raw_line.split("#", maxsplit=1)[0].strip()
        if not line or line.startswith("-"):
            continue
        line = line.split(";", maxsplit=1)[0].strip()
        match = re.match(r"([A-Za-z0-9][A-Za-z0-9._-]*)", line)
        if match:
            names.add(_normalize_requirement_name(match.group(1)))
    return names


def _assert_developer_tools_stay_out_of_runtime_requirements(
    runtime: str, dev: str
) -> None:
    """Assert that developer tools have only a development-requirements home.

    Beginner note: ``Dockerfile`` installs the runtime file into production,
    while CI installs both files. Checking both sides catches accidentally
    shipping a test tool and accidentally deleting the tool from CI at once.

    Args:
        runtime: Contents of the production requirements file.
        dev: Contents of the development requirements file.

    Raises:
        AssertionError: If a development tool is in the runtime set or absent
            from the development set.
    """
    runtime_names = _requirement_names(runtime)
    dev_names = _requirement_names(dev)
    for name in DEVELOPMENT_TOOLS:
        normalized_name = _normalize_requirement_name(name)
        assert normalized_name not in runtime_names, (
            f"{name} is a developer tool and must not be in requirements.txt"
        )
        assert normalized_name in dev_names, (
            f"{name} should still be declared in requirements-dev.txt"
        )


@pytest.mark.parametrize(
    "runtime_line",
    (
        "pytest==9.1.1",
        "pytest # inline comment",
        'pytest ; python_version >= "3.11"',
        "pytest_cov",
        "pytest.cov",
        "pytest[extra]>=9",
    ),
)
def test_developer_tool_guard_rejects_requirement_syntax_variants(
    monkeypatch: pytest.MonkeyPatch, runtime_line: str
):
    """The guard must reject tool declarations hidden by requirement syntax.

    Beginner note: the original guard matched an entire line such as exactly
    ``pytest``. A version, comment, marker, extra, or alternate separator made
    the same project invisible to that check, allowing it back into production.
    """
    runtime = f"requests\n{runtime_line}\npsycopg[binary]\n"
    dev = "pytest\npytest-cov\nruff\nbandit\npip-audit\nmypy\npre-commit\n"

    with pytest.raises(AssertionError, match="pytest"):
        _run_developer_tool_guard_with_sources(monkeypatch, runtime, dev)


def test_developer_tool_guard_ignores_benign_runtime_requirements(
    monkeypatch: pytest.MonkeyPatch,
):
    """Runtime packages must not be mistaken for development tooling.

    Beginner note: a parser that flags every requirement, or matches partial
    names, could reject legitimate runtime packages and hide the real policy
    failure. This case proves ordinary runtime dependencies remain allowed.
    """
    runtime = "requests>=2\nPyYAML\npsycopg[binary]\n"
    dev = "pytest\npytest-cov\nruff\nbandit\npip-audit\nmypy\npre-commit\n"

    _run_developer_tool_guard_with_sources(monkeypatch, runtime, dev)


def test_developer_tool_guard_accepts_requirement_syntax_in_development_file(
    monkeypatch: pytest.MonkeyPatch,
):
    """The presence check accepts normal requirement syntax in the dev file.

    Beginner note: the policy has two halves. It must reject tools in the image
    inputs and still recognize them when CI declares versions, extras, markers,
    comments, or equivalent project-name spelling in its own input.
    """
    runtime = "requests>=2\nPyYAML\npsycopg[binary]\n"
    dev = (
        "pytest==9.1.1 # pinned test runner\n"
        'pytest.cov[plugin]>=7 ; python_version >= "3.11"\n'
        "Ruff\n"
        'BANDIT ; python_version >= "3.11"\n'
        "pip_audit[security]\n"
        "MyPy # static types\n"
        "pre.commit\n"
    )

    _run_developer_tool_guard_with_sources(monkeypatch, runtime, dev)


def test_developer_tool_guard_requires_each_tool_in_development_requirements(
    monkeypatch: pytest.MonkeyPatch,
):
    """Removing a tool from the development file must fail the policy guard.

    Beginner note: a clean runtime file alone does not prove CI is configured;
    silently dropping a tool from the development file would make its checks
    unavailable. The missing ``mypy`` declaration must therefore fail loudly.
    """
    runtime = "requests\n"
    dev = "pytest\npytest-cov\nruff\nbandit\npip-audit\npre-commit\n"

    with pytest.raises(AssertionError, match="mypy"):
        _run_developer_tool_guard_with_sources(monkeypatch, runtime, dev)


def test_developer_tool_guard_normalizes_case_and_name_separators(
    monkeypatch: pytest.MonkeyPatch,
):
    """Case and separator spelling must not bypass the guard.

    Beginner note: if only one spelling were normalized, an equivalent name
    such as ``pytest_cov`` or ``pre_commit`` could bypass the development-file
    presence check. This verifies those alternate forms remain recognized.
    """
    runtime = "requests\n"
    dev = "PyTeSt\npytest_cov\nruff\nbandit\npip-audit\nmypy\npre_commit\n"

    _run_developer_tool_guard_with_sources(monkeypatch, runtime, dev)


def _run_developer_tool_guard_with_sources(
    monkeypatch: pytest.MonkeyPatch, runtime: str, dev: str
) -> None:
    """Run the policy guard against supplied text without touching files.

    Beginner note: replacing only the two file reads keeps these regressions
    focused on the real guard while avoiding temporary files or edits to the
    checked-in dependency declarations.

    Args:
        monkeypatch: Pytest fixture that restores ``Path.read_text`` afterward.
        runtime: In-memory production requirements contents.
        dev: In-memory development requirements contents.

    Returns:
        None. The wrapped guard raises ``AssertionError`` for a policy failure.
    """
    original_read_text = Path.read_text

    def read_text(
        path: Path, encoding: str | None = None, errors: str | None = None
    ) -> str:
        if path == ROOT / "requirements.txt":
            return runtime
        if path == ROOT / "requirements-dev.txt":
            return dev
        return original_read_text(path, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", read_text)
    test_developer_tools_stay_out_of_the_runtime_requirements()


def test_readme_documents_local_quality_and_security_commands():
    """The README should teach users how to reproduce the CI checks locally."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -c constraints.txt" in text
    assert "pip install -r requirements-dev.txt -c constraints.txt" in text
    assert (
        "python -m pytest -q --cov=app --cov=backend --cov=screeners --cov=ui "
        "--cov-fail-under=89"
        in text
    )
    assert "python -m compileall -q app.py backend screeners ui tests" in text
    assert "python -m ruff check app.py backend screeners ui Dependencies tests" in text
    assert "python -m bandit -r app.py backend screeners ui Dependencies -q" in text
    assert "python -m pip_audit -r constraints.txt" in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert "python -m pre_commit run --all-files" in text
    assert "requirements-optional.txt" in text


def test_operations_guide_matches_scheduler_database_and_ci_contracts():
    """Operations guidance should remain executable and identical to CI."""
    text = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")

    assert "CRON_TZ=Asia/Kolkata" in text
    assert "host timezone" in text.lower()
    assert "`psycopg[binary]`" in text
    assert "SCANNER_AI_MAX_ATTEMPTS=2" in text
    assert "1 disables validation retries" in text
    assert "clamped to `1`-`3`" in text
    assert "Agent SDK credit" in text
    assert "cp .env.example .env" in text
    assert "cp .streamlit/secrets.example.toml .streamlit/secrets.toml" in text
    assert "docker compose up --build" in text
    assert "docker compose run --rm scanner-ui python -m backend.jobs.run_daily_scan" in text
    for command in CI_COMMANDS:
        assert command in text
    assert "python -m pip_audit -r requirements.txt -r requirements-dev.txt" not in text


def test_postgres_guide_keeps_credentials_out_of_shell_arguments():
    """DEPLOY-004 examples should teach a secret-safe operator workflow.

    Beginner note: placeholders in a command are often replaced in-place by an
    operator. That puts the real password into shell history and, while the
    command runs, into the process argument list. A protected env file and an
    interactive ``psql`` prompt avoid both leaks.
    """
    text = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    worked_example = text.split(
        "### Worked example: self-hosted Postgres, end to end", maxsplit=1
    )[1].split("### Connection-pool behavior and guidance", maxsplit=1)[0]

    assert "chmod 600 postgres.env" in worked_example
    assert "--env-file postgres.env" in worked_example
    assert "chmod 600 Dependencies/.env" in worked_example
    assert "percent-encode" in worked_example.lower()
    assert "psql -h db-host -U scanner -d scanner -W" in worked_example
    assert "audit_logs" in worked_example

    assert "-e POSTGRES_PASSWORD=" not in worked_example
    assert "DATABASE_URL=postgresql+psycopg://scanner:<password>" not in worked_example
    assert 'psql "postgresql://scanner:<password>' not in worked_example
    assert "`audit_log`" not in worked_example


def test_container_examples_keep_runtime_secrets_out_of_process_arguments():
    """Production Docker examples should load secrets from a protected env file.

    Beginner note: ``docker run -e NAME=value`` makes the value part of the
    command line. A real password or provider token can then remain in shell
    history and may be visible to local process-inspection tools. ``--env-file``
    keeps those values out of the command arguments while preserving the same
    container environment.
    """
    text = (ROOT / "docs" / "operations.md").read_text(encoding="utf-8")
    container_examples = text.split("For production,", maxsplit=1)[1].split(
        "### Backing up scan history", maxsplit=1
    )[0]

    assert container_examples.count("--env-file Dependencies/.env") == 2
    assert "-e DATABASE_URL=" not in container_examples
    assert "-e DHAN_ACCESS_TOKEN=" not in container_examples

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    readme_production = readme.split(
        "Production containers default to fail-closed settings", maxsplit=1
    )[1].split("## Running the daily scan job", maxsplit=1)[0]
    assert "--env-file Dependencies/.env" in readme_production
    assert "-e DATABASE_URL=" not in readme_production
    assert "-e DHAN_ACCESS_TOKEN=" not in readme_production

    # The quick URL example should agree with the worked guidance: reserved
    # password characters are encoded before the URL enters the protected file.
    assert "scanner:<password>@db-host" not in text
    assert "scanner:<percent-encoded-password>@db-host" in text


def test_ai_architecture_docs_describe_validation_fallback_and_safe_errors():
    scan_service = (
        ROOT
        / "docs"
        / "architecture"
        / "components"
        / "scan-service-and-provenance.md"
    ).read_text(encoding="utf-8")
    fundamentals = (
        ROOT / "docs" / "architecture" / "components" / "fundamentals-ai.md"
    ).read_text(encoding="utf-8")

    assert (
        "technical-analysis screener keeps an eligible deterministic gate-only row"
        in scan_service
    )
    assert "67 ka funda screener produces no result row" in scan_service
    assert "failed validation after retries" not in scan_service
    assert "raw model text" in fundamentals
    assert "never included" in fundamentals


def test_screener_guide_matches_registry_chart_golden_and_ci_contracts():
    """The screener walkthrough should describe interfaces the repo really exposes."""
    text = (ROOT / "docs" / "adding-a-screener.md").read_text(encoding="utf-8")

    assert "the universe key exists in config" not in text
    assert "does not validate the universe key" in text
    assert 'std_multiplier=float(params.get("std_multiplier", 2.0))' in text
    assert "`GoldenCase`" in text
    assert "deterministic candle" in text
    assert "series.isna().any()" not in text
    for command in CI_COMMANDS:
        assert command in text
