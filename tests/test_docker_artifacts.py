from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _dockerignore_entries() -> set[str]:
    return {
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def test_dockerfile_has_secure_streamlit_runtime_contract() -> None:
    dockerfile = _read("Dockerfile")

    assert "FROM python:3.11-slim-bookworm" in dockerfile
    assert "WORKDIR /app" in dockerfile
    assert "requirements.txt" in dockerfile
    assert "constraints.txt" in dockerfile
    assert "-r requirements.txt" in dockerfile
    assert "-c constraints.txt" in dockerfile
    assert "APP_ENV=production" in dockerfile
    assert "AUTH_REQUIRED=true" in dockerfile
    assert "DATA_DIR=/data" in dockerfile
    assert "EXPOSE 8501" in dockerfile
    assert "USER appuser" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "_stcore/health" in dockerfile
    assert '"streamlit"' in dockerfile
    assert '"run"' in dockerfile
    assert '"app.py"' in dockerfile
    assert "--server.address=0.0.0.0" in dockerfile
    assert "--server.port=8501" in dockerfile
    assert "--server.headless=true" in dockerfile
    assert "--browser.gatherUsageStats=false" in dockerfile
    assert "python app.py" not in dockerfile
    assert "C:\\Users" not in dockerfile
    assert "/Users/" not in dockerfile


def test_dockerignore_excludes_secrets_and_generated_runtime_state() -> None:
    entries = _dockerignore_entries()

    required_entries = {
        ".git",
        ".mypy_cache/",
        ".pytest_cache/",
        ".ruff_cache/",
        ".streamlit/secrets.toml",
        ".venv/",
        "__pycache__/",
        "*.py[cod]",
        ".coverage",
        ".coverage.*",
        "htmlcov/",
        "Dependencies/.env",
        "Dependencies/all_instrument*.csv",
        "data/cache/",
        "data/universes/*",
        "!data/universes/hemant_super_45.csv",
        "!data/universes/hemant_good_45.csv",
        "!data/universes/hemant_good_200.csv",
        "!data/universes/hemant_super_good_200_union.csv",
        "data/*.db",
        "data/*.db-wal",
        "data/*.db-shm",
        "*.sqlite",
        "*.sqlite3",
        "venv/",
        "env/",
    }
    assert required_entries <= entries


def test_readme_and_operations_document_docker_runtime() -> None:
    readme = _read("README.md")
    operations = _read("docs/operations.md")

    assert "docker build -t streamlit-scanner-app ." in readme
    assert "-p 8501:8501" in readme
    assert "streamlit-scanner-data:/data" in readme
    assert "APP_ENV=development" in readme
    assert "AUTH_REQUIRED=false" in readme
    assert "APP_ENV=production" in readme
    assert "DATA_DIR=/data" in readme
    assert "DATABASE_URL=postgresql+psycopg://" in readme
    assert ".streamlit/secrets.toml" in readme

    assert "Docker / container deployment" in operations
    assert "docker run" in operations
    assert "streamlit-scanner-data:/data" in operations
    assert "DATA_DIR=/data" in operations
    assert "DATABASE_URL" in operations
    assert "--entrypoint python" in operations
    assert "-m backend.jobs.run_daily_scan" in operations


def test_architecture_docs_link_deployment_runtime_lld() -> None:
    architecture_index = _read("docs/architecture/README.md")
    hld = _read("docs/architecture/high-level-design.md")
    lld = _read("docs/architecture/components/deployment-runtime.md")

    assert "[deployment-runtime.md](components/deployment-runtime.md)" in architecture_index
    assert "[deployment-runtime](components/deployment-runtime.md)" in hld
    assert "Dockerfile" in lld
    assert ".dockerignore" in lld
    assert "0.0.0.0:8501" in lld
    assert "DATA_DIR=/data" in lld
    assert "non-root" in lld
    assert "/_stcore/health" in lld
    assert "docker-build" in hld
