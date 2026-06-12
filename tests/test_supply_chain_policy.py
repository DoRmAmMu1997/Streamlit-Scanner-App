"""Repository policy tests for supply-chain hygiene.

These tests intentionally read documentation and CI files. They protect the
project's security workflow from quiet drift: if someone removes dependency
auditing, static analysis, or the constraints install path, the normal pytest
suite will call that out before a PR lands.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_ci_workflow_runs_quality_and_dependency_security_checks():
    """CI should run the same checks maintainers run locally."""
    workflow = ROOT / ".github" / "workflows" / "quality-and-security.yml"
    text = workflow.read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt" in text
    assert "python -m pytest -q" in text
    assert "--cov-fail-under=" in text
    assert "python -m compileall -q app.py backend screeners tests" in text
    assert "python -m ruff check app.py backend screeners Dependencies tests" in text
    assert "python -m mypy" in text
    assert "python -m bandit -r app.py backend screeners Dependencies -q" in text
    assert "python -m pip_audit -r requirements.txt -r requirements-dev.txt" in text


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


def test_readme_documents_constraints_and_security_audit_commands():
    """The README should teach users which installs are runtime vs dev."""
    text = (ROOT / "README.md").read_text(encoding="utf-8")

    assert "pip install -r requirements.txt -c constraints.txt" in text
    assert "pip install -r requirements-dev.txt -c constraints.txt" in text
    assert "python -m bandit -r app.py backend screeners Dependencies -q" in text
    assert "python -m pip_audit -r requirements.txt" in text
    assert "requirements-optional.txt" in text
