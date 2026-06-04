"""Central runtime settings for local runs and production deployments.

Beginner note:
Environment variables are strings supplied by the shell, a hosting platform, or
``Dependencies/.env``. This module turns those strings into one typed object
(``AppSettings``) so the rest of the app does not need to remember which names
exist, which aliases are old-but-supported, or which values are secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled by credential_status()
    load_dotenv = None


# Resolve project paths from this file's location, not the user's current
# terminal folder. settings.py lives in backend/config/, so parents[2] is the
# repository root.
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEPENDENCIES_DIR = PROJECT_ROOT / "Dependencies"
ENV_PATH = DEPENDENCIES_DIR / ".env"
DEFAULT_DATA_DIR = PROJECT_ROOT / "data"
SCREENERS_DIR = PROJECT_ROOT / "screeners"

# Default Claude model used by the Claude Agent SDK features (Check
# Fundamentals, Technical Analysis AI, and 67 Ka Funda AI).
DEFAULT_FUNDAMENTALS_MODEL = "claude-sonnet-4-6"

# Public URLs used to build stock universes. These do not require Dhan
# credentials. Dhan credentials are only needed when fetching candle data.
DHAN_SCRIP_MASTER_URL = "https://images.dhan.co/api-data/api-scrip-master-detailed.csv"
NIFTY_100_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty100list.csv"
NIFTY_500_URL = "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"

# Some official/public CSV endpoints reject clients that look like scripts.
# Sending a normal browser-like User-Agent makes those downloads more reliable.
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0 Safari/537.36"
    )
}

_PRODUCTION_ENV_VALUES = {"prod", "production"}
_VALID_LOG_LEVELS = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class SettingsError(RuntimeError):
    """Raised when runtime settings are invalid or unsafe."""


@dataclass(frozen=True)
class DhanCredentials:
    """The two fields needed to create an authenticated DhanHQ SDK client."""

    client_code: str
    access_token: str


@dataclass(frozen=True, repr=False)
class AppSettings:
    """Typed runtime configuration read from environment variables.

    ``repr=False`` plus the custom ``__repr__`` below are deliberate. A default
    dataclass repr would print API keys and database passwords into logs or test
    failures. This object only prints a safe summary.
    """

    app_env: str
    database_url: str
    data_dir: Path
    allowed_emails: frozenset[str]
    admin_emails: frozenset[str]
    anthropic_api_key: str
    serpapi_api_key: str
    dhan_client_id: str
    dhan_access_token: str
    log_level: str
    auth_required: bool
    database_url_from_env: bool = False
    data_dir_from_env: bool = False

    @property
    def is_production(self) -> bool:
        """Return True for production-like environments."""
        return self.app_env in _PRODUCTION_ENV_VALUES

    @property
    def universe_dir(self) -> Path:
        """Directory containing generated universe CSV files."""
        return self.data_dir / "universes"

    @property
    def daily_cache_dir(self) -> Path:
        """Directory containing cached daily candle parquet files."""
        return self.data_dir / "cache" / "daily"

    @property
    def fundamentals_cache_dir(self) -> Path:
        """Directory containing cached fundamentals JSON files."""
        return self.data_dir / "cache" / "fundamentals"

    @property
    def fundamentals_pdf_dir(self) -> Path:
        """Directory containing downloaded concall PDFs and extracted text."""
        return self.fundamentals_cache_dir / "pdfs"

    def safe_dict(self) -> dict[str, object]:
        """Return a log/debug-safe summary that never includes secret values."""
        return {
            "app_env": self.app_env,
            "is_production": self.is_production,
            "data_dir": str(self.data_dir),
            "has_database_url": bool(self.database_url),
            "database_url_from_env": self.database_url_from_env,
            "data_dir_from_env": self.data_dir_from_env,
            "allowed_email_count": len(self.allowed_emails),
            "admin_email_count": len(self.admin_emails),
            "has_anthropic_api_key": bool(self.anthropic_api_key),
            "has_serpapi_api_key": bool(self.serpapi_api_key),
            "has_dhan_client_id": bool(self.dhan_client_id),
            "has_dhan_access_token": bool(self.dhan_access_token),
            "log_level": self.log_level,
            "auth_required": self.auth_required,
        }

    def __repr__(self) -> str:
        """Print only the secret-safe summary."""
        return f"AppSettings({self.safe_dict()!r})"


def load_environment() -> None:
    """Load only the scanner app's local ``Dependencies/.env`` file."""
    if load_dotenv is None:
        return
    if ENV_PATH.exists():
        # override=False means shell/deployment env vars win over local .env.
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def _clean_env_value(value: Any) -> str:
    """Normalize env values so KEY=value and KEY="value" both work."""
    cleaned = str(value or "").strip()
    if cleaned.startswith(('"', "'")) and cleaned.endswith(('"', "'")):
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def _env_value(source: Mapping[str, str], name: str, *aliases: str) -> str:
    """Read the canonical env name first, then any legacy aliases."""
    for key in (name, *aliases):
        value = _clean_env_value(source.get(key))
        if value:
            return value
    return ""


def _normalize_app_env(raw: str) -> str:
    """Collapse common env spellings while leaving custom names readable."""
    value = (raw or "development").strip().lower()
    if value in {"dev", "development", "local"}:
        return "development"
    if value in _PRODUCTION_ENV_VALUES:
        return "production"
    return value or "development"


def _parse_bool(raw: str, *, default: bool) -> bool:
    """Parse an env boolean with a caller-chosen default."""
    value = _clean_env_value(raw).lower()
    if not value:
        return default
    if value in _TRUE_VALUES:
        return True
    if value in _FALSE_VALUES:
        return False
    raise SettingsError(
        "AUTH_REQUIRED must be one of true/false, yes/no, on/off, or 1/0."
    )


def _parse_log_level(raw: str) -> str:
    """Return a logging level name or raise a clear settings error."""
    value = _clean_env_value(raw).upper() or "WARNING"
    if value not in _VALID_LOG_LEVELS:
        raise SettingsError(
            f"Invalid LOG_LEVEL {value!r}. Expected one of: "
            f"{', '.join(sorted(_VALID_LOG_LEVELS))}."
        )
    return value


def _parse_email_set(raw: str) -> frozenset[str]:
    """Split a comma-separated email list into normalized lowercase values."""
    return frozenset(
        email
        for part in _clean_env_value(raw).split(",")
        if (email := part.strip().lower())
    )


def get_settings(
    *,
    env: Mapping[str, str] | None = None,
    validate: bool = False,
) -> AppSettings:
    """Read environment-backed runtime settings.

    Tests can pass ``env={...}`` to avoid reading the developer's real
    ``Dependencies/.env`` file. Normal application code calls ``get_settings()``
    with no arguments so the local dotenv file and process environment are read.
    """
    if env is None:
        load_environment()
        source: Mapping[str, str] = os.environ
    else:
        source = env

    app_env = _normalize_app_env(_env_value(source, "APP_ENV", "SCANNER_ENV"))
    data_dir_value = _env_value(source, "DATA_DIR")
    data_dir = Path(data_dir_value).expanduser() if data_dir_value else DEFAULT_DATA_DIR

    database_url_value = _env_value(source, "DATABASE_URL")
    database_url = database_url_value or f"sqlite:///{(data_dir / 'scanner.db').as_posix()}"

    log_level_raw = _env_value(source, "LOG_LEVEL")
    if not log_level_raw and _env_value(source, "SCANNER_DEBUG").lower() in _TRUE_VALUES:
        log_level_raw = "DEBUG"

    settings = AppSettings(
        app_env=app_env,
        database_url=database_url,
        data_dir=data_dir,
        allowed_emails=_parse_email_set(_env_value(source, "ALLOWED_EMAILS")),
        admin_emails=_parse_email_set(_env_value(source, "ADMIN_EMAILS")),
        anthropic_api_key=_env_value(source, "ANTHROPIC_API_KEY"),
        serpapi_api_key=_env_value(source, "SERPAPI_API_KEY"),
        dhan_client_id=_env_value(source, "DHAN_CLIENT_ID", "DHAN_CLIENT_CODE"),
        dhan_access_token=_env_value(source, "DHAN_ACCESS_TOKEN"),
        log_level=_parse_log_level(log_level_raw),
        auth_required=_parse_bool(
            _env_value(source, "AUTH_REQUIRED"),
            default=app_env in _PRODUCTION_ENV_VALUES,
        ),
        database_url_from_env=bool(database_url_value),
        data_dir_from_env=bool(data_dir_value),
    )
    if validate:
        validate_production_settings(settings)
    return settings


def validate_production_settings(settings: AppSettings | None = None) -> AppSettings:
    """Fail clearly when production is missing required runtime config."""
    settings = settings or get_settings()
    if not settings.is_production:
        return settings

    missing: list[str] = []
    if not settings.database_url_from_env:
        missing.append("DATABASE_URL")
    if not settings.data_dir_from_env:
        missing.append("DATA_DIR")
    if not settings.dhan_client_id:
        missing.append("DHAN_CLIENT_ID")
    if not settings.dhan_access_token:
        missing.append("DHAN_ACCESS_TOKEN")
    if settings.auth_required is False:
        missing.append("AUTH_REQUIRED cannot be false in production")
    if settings.auth_required and not (settings.allowed_emails or settings.admin_emails):
        missing.append("ALLOWED_EMAILS or ADMIN_EMAILS")

    if missing:
        raise SettingsError(
            "Invalid production settings. Missing or unsafe: "
            + ", ".join(missing)
            + "."
        )
    return settings


def secret_values(settings: AppSettings | None = None) -> list[str]:
    """Return configured secret-like values for redaction helpers."""
    settings = settings or get_settings()
    return [
        value
        for value in (
            settings.database_url,
            settings.anthropic_api_key,
            settings.serpapi_api_key,
            settings.dhan_client_id,
            settings.dhan_access_token,
        )
        if value
    ]


def ensure_project_dirs() -> None:
    """Create runtime folders that are safe to generate locally."""
    settings = get_settings()
    for path in (
        DEPENDENCIES_DIR,
        settings.universe_dir,
        settings.daily_cache_dir,
        settings.fundamentals_cache_dir,
        settings.fundamentals_pdf_dir,
        SCREENERS_DIR,
    ):
        path.mkdir(parents=True, exist_ok=True)


def get_dhan_credentials(required: bool = False) -> DhanCredentials | None:
    """Return Dhan credentials from the centralized settings object."""
    settings = get_settings()
    if settings.dhan_client_id and settings.dhan_access_token:
        return DhanCredentials(
            client_code=settings.dhan_client_id,
            access_token=settings.dhan_access_token,
        )

    if required:
        missing = []
        if not settings.dhan_client_id:
            missing.append("DHAN_CLIENT_ID")
        if not settings.dhan_access_token:
            missing.append("DHAN_ACCESS_TOKEN")
        raise RuntimeError(
            f"Missing Dhan credential(s): {', '.join(missing)}. "
            f"Create {ENV_PATH} from Dependencies/.env.example, then run "
            "python Dependencies/dhan_token_setup.py if you need a fresh token."
        )
    return None


def get_fundamentals_model() -> str:
    """Return the Claude model used by this app's Claude Agent SDK features."""
    if load_dotenv is not None:
        load_environment()
    return _clean_env_value(os.getenv("CLAUDE_AGENT_MODEL")) or DEFAULT_FUNDAMENTALS_MODEL


def get_agent_fast_mode() -> bool:
    """Return True when the AI agents should run in low-latency fast mode."""
    load_environment()
    raw = _clean_env_value(os.getenv("SCANNER_AGENT_FAST_MODE")).lower()
    return raw in _TRUE_VALUES


def credential_status() -> dict[str, object]:
    """Return a UI-friendly credential summary without exposing secrets."""
    settings = get_settings()
    return {
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.exists(),
        "has_client_code": bool(settings.dhan_client_id),
        "has_access_token": bool(settings.dhan_access_token),
        "ready": bool(settings.dhan_client_id and settings.dhan_access_token),
    }


def dhan_request_delay_seconds() -> float:
    """Read the pause between Dhan history cache misses, defaulting to 0.50s."""
    load_environment()
    raw_value = _clean_env_value(os.getenv("SCANNER_DHAN_REQUEST_DELAY_SECONDS"))
    if not raw_value:
        return 0.5
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return 0.5
    return parsed if parsed >= 0 else 0.5


def dhan_rate_limit_retry_delays() -> list[float]:
    """Read DH-904 retry backoff delays, defaulting to 2s, 5s, and 10s."""
    load_environment()
    defaults = [2.0, 5.0, 10.0]
    raw_value = _clean_env_value(os.getenv("SCANNER_DHAN_RATE_LIMIT_RETRY_DELAYS"))
    if not raw_value:
        return defaults
    try:
        delays = [float(part.strip()) for part in raw_value.split(",") if part.strip()]
    except (TypeError, ValueError):
        return defaults
    if not delays or any(delay < 0 for delay in delays):
        return defaults
    return delays
