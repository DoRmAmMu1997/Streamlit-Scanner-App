"""Contract tests for the DEPLOY-003 Render Blueprint and its docs.

Like ``test_docker_artifacts.py``, these are string/data-level checks against the
checked-in files — no Render account or network needed. They lock the Blueprint
contract: the service graph, the managed-database wiring, the disk topology, the
$PORT-aware web command, the universe-refresh cron command, and — most important
for safety — that **no secret carries a committed value** (every secret env var
is ``sync: false``). Actual Render provisioning happens on the platform.
"""

from __future__ import annotations

from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]

# Env keys that must never be committed with a literal value in the Blueprint.
_SECRET_KEYS = {
    "DHAN_CLIENT_ID",
    "DHAN_ACCESS_TOKEN",
    "ALLOWED_EMAILS",
    "ADMIN_EMAILS",
    "SERPAPI_API_KEY",
}


def _read(path: str) -> str:
    return (REPO_ROOT / path).read_text(encoding="utf-8")


def _blueprint() -> dict:
    """Parse render.yaml as data so the service graph can be asserted directly."""
    return yaml.safe_load(_read("render.yaml"))


def _service(blueprint: dict, name: str) -> dict:
    for service in blueprint["services"]:
        if service["name"] == name:
            return service
    raise AssertionError(f"render.yaml has no service named {name!r}")


def _env_map(service: dict) -> dict[str, dict]:
    """Map a service's envVars list to {key: entry} for easy assertions."""
    return {entry["key"]: entry for entry in service.get("envVars", [])}


def test_blueprint_defines_web_cron_and_managed_database() -> None:
    """The Blueprint is exactly: one managed Postgres, one web service, one cron."""
    blueprint = _blueprint()

    database_names = {db["name"] for db in blueprint["databases"]}
    assert database_names == {"scanner-db"}

    services = {s["name"]: s["type"] for s in blueprint["services"]}
    assert services == {"scanner-web": "web", "scanner-daily-scan": "cron"}


def test_web_service_reuses_dockerfile_and_binds_render_port() -> None:
    """The web service runs the production image and serves on Render's $PORT."""
    web = _service(_blueprint(), "scanner-web")

    assert web["runtime"] == "docker"
    assert web["dockerfilePath"] == "./Dockerfile"
    assert web["healthCheckPath"] == "/_stcore/health"
    # Must bind $PORT (the image CMD hard-codes 8501, which Render would not route).
    assert "--server.port=$PORT" in web["dockerCommand"]
    assert "streamlit run app.py" in web["dockerCommand"]


def test_web_service_persists_data_on_a_configurable_disk() -> None:
    """A persistent disk backs DATA_DIR, and the path is configurable (they match)."""
    web = _service(_blueprint(), "scanner-web")

    disk = web["disk"]
    assert disk["name"] == "scanner-data"
    assert disk["sizeGB"] >= 1
    env = _env_map(web)
    # The acceptance criterion: the persistent disk path is configurable via env.
    assert env["DATA_DIR"]["value"] == disk["mountPath"]


def test_database_url_is_auto_wired_from_the_managed_database() -> None:
    """Both services read DATABASE_URL from the managed Postgres, never hardcoded."""
    blueprint = _blueprint()
    for name in ("scanner-web", "scanner-daily-scan"):
        env = _env_map(_service(blueprint, name))
        from_db = env["DATABASE_URL"]["fromDatabase"]
        assert from_db == {"name": "scanner-db", "property": "connectionString"}


def test_web_service_is_fail_closed_production() -> None:
    """The web service runs production with auth required and JSON logs."""
    env = _env_map(_service(_blueprint(), "scanner-web"))
    assert env["APP_ENV"]["value"] == "production"
    assert env["AUTH_REQUIRED"]["value"] == "true"
    assert env["LOG_FORMAT"]["value"] == "json"


def test_cron_refreshes_universes_then_runs_the_daily_scan() -> None:
    """The ephemeral cron rebuilds universe CSVs before scanning, on a schedule."""
    cron = _service(_blueprint(), "scanner-daily-scan")

    assert cron["schedule"].strip() != ""
    assert cron["runtime"] == "docker"
    command = cron["dockerCommand"]
    assert "refresh_universe_files" in command
    assert "python -m backend.jobs.run_daily_scan" in command
    # The cron deliberately has no persistent disk (Render disks are single-attach).
    assert "disk" not in cron


def test_blueprint_commits_no_secret_values() -> None:
    """Every secret is `sync: false` (dashboard-provided), never a committed value."""
    blueprint = _blueprint()
    for name in ("scanner-web", "scanner-daily-scan"):
        env = _env_map(_service(blueprint, name))
        for key in _SECRET_KEYS & env.keys():
            entry = env[key]
            assert entry.get("sync") is False, f"{name}:{key} must be sync: false"
            assert "value" not in entry, f"{name}:{key} must not commit a value"


def test_docs_document_the_render_deployment() -> None:
    """README + operations runbook + the architecture docs cover DEPLOY-003."""
    readme = _read("README.md")
    operations = _read("docs/operations.md")
    hld = _read("docs/architecture/high-level-design.md")
    lld = _read("docs/architecture/components/deployment-runtime.md")

    assert "Deploying to Render" in readme
    assert "render.yaml" in readme
    assert "Deploying to Render" in operations
    assert "render.yaml" in operations
    # The HLD/LLD record the topology decision and the DB-URL normalization.
    assert "DEPLOY-003" in hld
    assert "render.yaml" in lld
    assert "single-attach" in lld.lower() or "single attach" in lld.lower()
