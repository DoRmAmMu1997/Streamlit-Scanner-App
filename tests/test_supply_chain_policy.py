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


def test_ci_workflow_runs_quality_and_dependency_security_checks():
    """CI should run the same checks maintainers run locally."""
    workflow = ROOT / ".github" / "workflows" / "quality-and-security.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt" in text
    assert 'python-version: ["3.11", "3.12"]' in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert (
        "python -m pytest -q --cov=backend --cov=screeners --cov-fail-under=84"
        in text
    )
    assert "python -m compileall -q app.py backend screeners tests" in text
    assert "python -m ruff check app.py backend screeners Dependencies tests" in text
    assert "python -m mypy" in text
    assert "python -m bandit -r app.py backend screeners Dependencies -q" in text
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
        r"^(app\.py|backend/|screeners/|Dependencies/|tests/)"
    )
    assert all("--fix" not in hook.get("args", []) for hook in hooks)


def test_constraints_pin_direct_runtime_and_developer_dependencies():
    """Direct dependencies should have a documented, repeatable pin set."""
    text = (ROOT / "constraints.txt").read_text(encoding="utf-8")
    required_names = [
        "streamlit",
        "pandas",
        "numpy",
        "pyarrow",
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


def test_readme_documents_local_quality_and_security_commands():
    """The README should teach users how to reproduce the CI checks locally."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -c constraints.txt" in text
    assert "pip install -r requirements-dev.txt -c constraints.txt" in text
    assert (
        "python -m pytest -q --cov=backend --cov=screeners --cov-fail-under=84"
        in text
    )
    assert "python -m bandit -r app.py backend screeners Dependencies -q" in text
    assert "python -m pip_audit -r constraints.txt" in text
    assert "python -m pre_commit validate-config .pre-commit-config.yaml" in text
    assert "python -m pre_commit run --all-files" in text
    assert "requirements-optional.txt" in text
