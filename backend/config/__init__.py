"""Backwards-compatible configuration exports.

DEPLOY-004 moved runtime settings into ``backend.config.settings``. This package
still exposes the old names from ``backend.config`` so existing imports keep
working while new deployment code can use the typed settings object directly.
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

_settings = get_settings()

# Historical path constants. They are evaluated at import time from the current
# settings so deployments that set DATA_DIR before startup still get the right
# runtime folders, while old callers can keep importing these names.
DATA_DIR = _settings.data_dir
UNIVERSE_DIR = _settings.universe_dir
DAILY_CACHE_DIR = _settings.daily_cache_dir
FUNDAMENTALS_CACHE_DIR = _settings.fundamentals_cache_dir
FUNDAMENTALS_PDF_DIR = _settings.fundamentals_pdf_dir

__all__ = [
    "AppSettings",
    "DAILY_CACHE_DIR",
    "DATA_DIR",
    "DEFAULT_DATA_DIR",
    "DEFAULT_FUNDAMENTALS_MODEL",
    "DEPENDENCIES_DIR",
    "DHAN_SCRIP_MASTER_URL",
    "DhanCredentials",
    "ENV_PATH",
    "FUNDAMENTALS_CACHE_DIR",
    "FUNDAMENTALS_PDF_DIR",
    "NIFTY_100_URL",
    "NIFTY_500_URL",
    "PROJECT_ROOT",
    "REQUEST_HEADERS",
    "SCREENERS_DIR",
    "SettingsError",
    "UNIVERSE_DIR",
    "_clean_env_value",
    "credential_status",
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
