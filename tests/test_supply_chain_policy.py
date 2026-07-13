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
    "python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=87",
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


def test_ci_workflow_runs_quality_and_dependency_security_checks():
    """CI should run the same checks maintainers run locally."""
    workflow = ROOT / ".github" / "workflows" / "quality-and-security.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "permissions:\n  contents: read" in text
    assert "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt" in text
    assert 'python-version: ["3.11", "3.12"]' in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert (
        "python -m pytest -q --cov=backend --cov=screeners --cov=ui "
        "--cov-fail-under=87"
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


def test_constraints_use_security_reviewed_dependency_versions():
    """Known-vulnerable direct pins must not re-enter the installed environment."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")

    assert re.search(r"^python-dotenv==1\.2\.2$", text, flags=re.MULTILINE)
    assert re.search(r"^lxml==6\.1\.0$", text, flags=re.MULTILINE)
    assert re.search(r"^pytest==9\.0\.3$", text, flags=re.MULTILINE)


def test_runtime_requirements_install_the_documented_postgres_driver():
    """The documented psycopg SQLAlchemy URL must work after normal setup."""
    text = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert re.search(r"^psycopg\[binary\]$", text, flags=re.IGNORECASE | re.MULTILINE)


def test_readme_documents_local_quality_and_security_commands():
    """The README should teach users how to reproduce the CI checks locally."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -c constraints.txt" in text
    assert "pip install -r requirements-dev.txt -c constraints.txt" in text
    assert (
        "python -m pytest -q --cov=backend --cov=screeners --cov=ui "
        "--cov-fail-under=87"
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
