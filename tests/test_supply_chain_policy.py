"""Repository policy tests for supply-chain hygiene.

These tests intentionally read documentation and CI files. They protect the
project's security workflow from quiet drift: if someone removes dependency
auditing, static analysis, or the constraints install path, the normal pytest
suite will call that out before a PR lands.
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_COMMANDS = (
    "python -m pre_commit validate-config .pre-commit-config.yaml",
    "python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84",
    "python -m compileall -q app.py backend screeners ui tests",
    "python -m ruff check app.py backend screeners ui Dependencies tests",
    "python -m mypy",
    "python -m bandit -r app.py backend screeners ui Dependencies -q",
    "python -m pip_audit -r constraints.txt",
)


def test_ci_workflow_runs_quality_and_dependency_security_checks():
    """CI should run the same checks maintainers run locally."""
    workflow = ROOT / ".github" / "workflows" / "quality-and-security.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt" in text
    assert 'python-version: ["3.11", "3.12"]' in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert (
        "python -m pytest -q --cov=backend --cov=screeners --cov=ui "
        "--cov-fail-under=84"
        in text
    )
    assert "python -m compileall -q app.py backend screeners ui tests" in text
    assert "python -m ruff check app.py backend screeners ui Dependencies tests" in text
    assert "python -m mypy" in text
    assert "python -m bandit -r app.py backend screeners ui Dependencies -q" in text
    assert "python -m pip_audit -r constraints.txt" in text
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
        "--cov-fail-under=84"
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
    for command in CI_COMMANDS:
        assert command in text
    assert "python -m pip_audit -r requirements.txt -r requirements-dev.txt" not in text


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
