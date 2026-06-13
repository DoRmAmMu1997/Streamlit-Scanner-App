"""Backwards-compatible configuration exports.

DEPLOY-004 moved runtime settings into ``backend.config.settings``. This package
still exposes the old names from ``backend.config`` so existing imports keep
working while new deployment code can use the typed settings object directly.

Beginner note:
Python treats a folder with ``__init__.py`` as a package. That means existing
code can still say ``from backend.config import DATA_DIR`` even though
``backend/config.py`` became the ``backend/config/`` folder. This file is the
bridge between the old import style and the new settings module.
"""

from __future__ import annotations

from .settings import (
    DEFAULT_DATA_DIR,
    DEFAULT_FUNDAMENTALS_MODEL,
    DEPENDENCIES_DIR,
    DHAN_SCRIP_MASTER_URL,
    ENV_PATH,
    NIFTY_100_URL,
    NIFTY_500_URL,
    PROJECT_ROOT,
    REQUEST_HEADERS,
    SCREENERS_DIR,
    AppSettings,
    DhanCredentials,
    SettingsError,
    _clean_env_value,
    credential_status,
    dhan_fetch_workers,
    dhan_rate_limit_retry_delays,
    dhan_request_delay_seconds,
    ensure_project_dirs,
    get_agent_fast_mode,
    get_dhan_credentials,
    get_fundamentals_model,
    get_settings,
    load_environment,
    secret_values,
    validate_production_settings,
)

try:
    _settings = get_settings()
except SettingsError:
    # A malformed value (e.g. LOG_LEVEL=chatty or AUTH_REQUIRED=maybe) must not
    # break importing these legacy path constants, or any module that imports
    # backend.config (app, backend.storage.database, Alembic's env.py). Fall back
    # to safe development defaults for the snapshot below; the real configuration
    # error is re-raised with a friendly message by get_settings() /
    # validate_production_settings() at app startup.
    _settings = get_settings(env={})

# Historical path constants. They are evaluated at import time from the current
# settings so deployments that set DATA_DIR before startup still get the right
# runtime folders, while old callers can keep importing these names.
#
# Important subtlety:
# These constants are snapshots. New code that needs to observe env changes made
# during a test should call get_settings() directly; old code that just needs the
# startup paths can keep using DATA_DIR / DAILY_CACHE_DIR exactly as before.
DATA_DIR = _settings.data_dir
UNIVERSE_DIR = _settings.universe_dir
DAILY_CACHE_DIR = _settings.daily_cache_dir
FUNDAMENTALS_CACHE_DIR = _settings.fundamentals_cache_dir
FUNDAMENTALS_PDF_DIR = _settings.fundamentals_pdf_dir

# __all__ documents the public surface of backend.config. It also helps readers
# see which names are intentionally supported versus merely imported as an
# implementation detail above.
__all__ = [
    "DAILY_CACHE_DIR",
    "DATA_DIR",
    "DEFAULT_DATA_DIR",
    "DEFAULT_FUNDAMENTALS_MODEL",
    "DEPENDENCIES_DIR",
    "DHAN_SCRIP_MASTER_URL",
    "ENV_PATH",
    "FUNDAMENTALS_CACHE_DIR",
    "FUNDAMENTALS_PDF_DIR",
    "NIFTY_100_URL",
    "NIFTY_500_URL",
    "PROJECT_ROOT",
    "REQUEST_HEADERS",
    "SCREENERS_DIR",
    "UNIVERSE_DIR",
    "AppSettings",
    "DhanCredentials",
    "SettingsError",
    "_clean_env_value",
    "credential_status",
    "dhan_fetch_workers",
    "dhan_rate_limit_retry_delays",
    "dhan_request_delay_seconds",
    "ensure_project_dirs",
    "get_agent_fast_mode",
    "get_dhan_credentials",
    "get_fundamentals_model",
    "get_settings",
    "load_environment",
    "secret_values",
    "validate_production_settings",
]
