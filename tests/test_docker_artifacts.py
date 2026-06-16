"""Contract tests for the DEPLOY-001 Docker artifacts and their docs.

These are deliberately *string-level* checks against the checked-in files
(`Dockerfile`, `.dockerignore`, README/operations, the architecture docs) — they
need no Docker daemon, so they run in the normal pytest suite on any machine.
Actually *building* the image is verified separately by the CI `docker-build`
job. The point of these tests is to lock the contract in place: if someone edits
the Dockerfile or moves a doc section, a mismatch fails here loudly.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _read(path: str) -> str:
    """Read a repo-relative text file (resolved against the repo root)."""
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _dockerignore_entries() -> set[str]:
    """Return the non-comment, non-blank patterns from `.dockerignore` as a set."""
    return {
        line.strip()
        for line in _read(".dockerignore").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def _compose() -> dict:
    """Parse the checked-in Compose file as data, not just text.

    Beginner note:
    Docker Compose YAML is executable deployment configuration. Reading it with a
    parser lets these tests assert the service graph and volume wiring directly
    instead of relying only on fragile substring checks.
    """
    return yaml.safe_load(_read("docker-compose.yml"))


def test_dockerfile_has_secure_streamlit_runtime_contract() -> None:
    """Lock the Dockerfile's key choices: slim base, constrained install, non-root
    user, production/auth defaults, exposed port, health check, the `streamlit run`
    entrypoint, and no local-path leakage."""
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
    """Ensure secrets, generated broker/cache/DB files, and dev caches stay out of
    the build context, while the tracked Hemant universe CSVs are re-included."""
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
    """The README and operations runbook must keep the documented Docker workflow
    (build, local smoke run, production run, daily-job entrypoint) in sync."""
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
    """The architecture index + HLD must link the deployment-runtime LLD, and that
    LLD must describe the core runtime contract (image, port, /data, health)."""
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


def test_docker_compose_defines_local_production_stack() -> None:
    """DEPLOY-002 should provide a two-service local production stack.

    The app service is the only thing exposed to the host. Postgres stays on the
    private Compose network so a local database password is not also an open
    laptop port.
    """
    compose = _compose()

    services = compose["services"]
    assert set(services) == {"scanner-ui", "postgres"}
    assert set(compose["volumes"]) == {"scanner-data", "postgres-data"}

    scanner = services["scanner-ui"]
    assert scanner["build"] == {"context": ".", "dockerfile": "Dockerfile"}
    assert scanner["ports"] == ["${SCANNER_UI_PORT:-8501}:8501"]
    assert "scanner-data:/data" in scanner["volumes"]
    assert (
        "${STREAMLIT_SECRETS_FILE:-.streamlit/secrets.toml}:"
        "/app/.streamlit/secrets.toml:ro"
        in scanner["volumes"]
    )
    assert scanner["depends_on"]["postgres"]["condition"] == "service_healthy"

    environment = set(scanner["environment"])
    assert "APP_ENV=production" in environment
    assert "AUTH_REQUIRED=true" in environment
    assert "DATA_DIR=/data" in environment
    assert (
        "DATABASE_URL=postgresql+psycopg://${POSTGRES_USER:-scanner}:"
        "${POSTGRES_PASSWORD:-scanner_dev_password_change_me}@postgres:5432/"
        "${POSTGRES_DB:-scanner}"
        in environment
    )
    assert "DHAN_CLIENT_ID=${DHAN_CLIENT_ID}" in environment
    assert "DHAN_ACCESS_TOKEN=${DHAN_ACCESS_TOKEN}" in environment
    assert "ALLOWED_EMAILS=${ALLOWED_EMAILS:-}" in environment
    assert "ADMIN_EMAILS=${ADMIN_EMAILS:-}" in environment

    postgres = services["postgres"]
    assert postgres["image"] == "postgres:16-bookworm"
    assert "ports" not in postgres
    assert "postgres-data:/var/lib/postgresql/data" in postgres["volumes"]
    healthcheck_command = " ".join(postgres["healthcheck"]["test"])
    assert "pg_isready" in healthcheck_command
    assert "$$POSTGRES_USER" in healthcheck_command
    assert "$$POSTGRES_DB" in healthcheck_command


def test_compose_env_template_documents_required_local_production_values() -> None:
    """The root `.env.example` is for Compose; `Dependencies/.env.example` stays
    as the non-container local Python template."""
    text = _read(".env.example")
    gitignore = _read(".gitignore")
    dockerignore_entries = _dockerignore_entries()

    assert "SCANNER_UI_PORT=8501" in text
    assert "POSTGRES_DB=scanner" in text
    assert "POSTGRES_USER=scanner" in text
    assert "POSTGRES_PASSWORD=scanner_dev_password_change_me" in text
    assert "STREAMLIT_SECRETS_FILE=.streamlit/secrets.toml" in text
    assert "DHAN_CLIENT_ID=" in text
    assert "DHAN_ACCESS_TOKEN=" in text
    assert "ALLOWED_EMAILS=" in text
    assert "ADMIN_EMAILS=" in text
    assert "SERPAPI_API_KEY=" in text
    assert "CLAUDE_AGENT_MODEL=claude-sonnet-4-6" in text
    assert "SCANNER_AGENT_FAST_MODE=0" in text
    assert "SCANNER_DHAN_FETCH_WORKERS=1" in text
    assert "LOG_FORMAT=json" in text
    assert "\n.env\n" in gitignore
    assert ".env" in dockerignore_entries


def test_docs_explain_docker_compose_local_production_workflow() -> None:
    """README, operations, HLD, and the deployment LLD should teach the same
    Compose workflow and make the single-container-vs-Compose distinction clear."""
    readme = _read("README.md")
    operations = _read("docs/operations.md")
    architecture_index = _read("docs/architecture/README.md")
    hld = _read("docs/architecture/high-level-design.md")
    lld = _read("docs/architecture/components/deployment-runtime.md")

    for text in (readme, operations):
        assert "docker compose up --build" in text
        assert "cp .env.example .env" in text
        assert "cp .streamlit/secrets.example.toml .streamlit/secrets.toml" in text
        assert "docker compose down" in text
        assert "docker compose down --volumes" in text
        assert (
            "docker compose run --rm scanner-ui python -m "
            "backend.jobs.run_daily_scan"
            in text
        )
        assert "scanner-data" in text
        assert "postgres-data" in text

    assert "Docker Compose" in architecture_index
    assert "scanner-ui" in hld
    assert "postgres" in hld
    assert "scanner-ui -> postgres" in lld
    assert "postgres-data" in lld
    assert "no host port" in lld.lower()
