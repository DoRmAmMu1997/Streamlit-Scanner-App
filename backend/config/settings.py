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
# OBS-001 log rendering modes. "auto" picks JSON in production and readable text
# in development; "json"/"text" force one rendering regardless of environment.
_VALID_LOG_FORMATS = {"auto", "json", "text"}
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class SettingsError(RuntimeError):
    """Raised when runtime settings are invalid or unsafe.

    Beginner note:
    A plain ``ValueError`` would also work technically, but this custom error
    makes it obvious to callers that the problem is deployment configuration,
    not a bug in a screener or a failed Dhan/SerpAPI network call.
    """


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

    Field guide:
    - string fields hold the cleaned value from the environment or ``""`` when
      absent;
    - email fields are immutable lower-case sets so allowlist comparisons are
      case-insensitive and cannot be mutated by accident;
    - ``database_url_from_env`` and ``data_dir_from_env`` remember whether the
      value came from an explicit deploy setting. Production validation uses
      those flags to distinguish "operator configured this" from "we fell back
      to a local development default".
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
    # OBS-001: how log lines are rendered. "auto" (default) => JSON in production,
    # readable text in development. Defaulted so any future AppSettings(...) build
    # that predates this field still works.
    log_format: str = "auto"
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
        """Return a log/debug-safe summary that never includes secret values.

        This is the object to print when debugging config. It intentionally says
        whether a secret exists (``has_serpapi_api_key``) without showing the
        actual secret. That gives operators useful setup information without
        leaking credentials into terminal logs, Streamlit UI, or CI artifacts.
        """
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
            "log_format": self.log_format,
            "auth_required": self.auth_required,
        }

    def __repr__(self) -> str:
        """Print only the secret-safe summary."""
        return f"AppSettings({self.safe_dict()!r})"


def load_environment() -> None:
    """Load only the scanner app's local ``Dependencies/.env`` file.

    Hosting platforms normally inject environment variables directly. Local
    developers usually keep them in ``Dependencies/.env``. Loading that one file
    here keeps the rest of the codebase from importing ``dotenv`` or knowing
    where the local env file lives.
    """
    if load_dotenv is None:
        return
    if ENV_PATH.exists():
        # override=False means shell/deployment env vars win over local .env.
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def _clean_env_value(value: Any) -> str:
    """Normalize env values so KEY=value and KEY="value" both work.

    Env vars are always text. People often paste values with surrounding quotes,
    especially on Windows or from dashboard UIs. Stripping one matching quote
    pair makes those common inputs behave like the unquoted form.
    """
    cleaned = str(value or "").strip()
    if cleaned.startswith(('"', "'")) and cleaned.endswith(('"', "'")):
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def _env_value(source: Mapping[str, str], name: str, *aliases: str) -> str:
    """Read the canonical env name first, then any legacy aliases.

    DEPLOY-004 introduces new names such as ``APP_ENV`` and ``DHAN_CLIENT_ID``.
    Older checkouts may still have ``SCANNER_ENV`` or ``DHAN_CLIENT_CODE`` in
    their local ``.env`` file. This helper lets new names win while keeping the
    old names functional during migration.
    """
    for key in (name, *aliases):
        value = _clean_env_value(source.get(key))
        if value:
            return value
    return ""


def _normalize_app_env(raw: str) -> str:
    """Collapse common env spellings while leaving custom names readable.

    The app only has special behavior for development-like and production-like
    values. A custom value such as ``staging`` is kept as ``staging`` so logs and
    summaries remain honest, but it does not trigger production-only validation.
    """
    value = (raw or "development").strip().lower()
    if value in {"dev", "development", "local"}:
        return "development"
    if value in _PRODUCTION_ENV_VALUES:
        return "production"
    return value or "development"


def _parse_bool(raw: str, *, default: bool) -> bool:
    """Parse an env boolean with a caller-chosen default.

    Strings like ``true`` and ``1`` are easy for humans to type but Python will
    treat every non-empty string as truthy. Explicit parsing prevents
    ``AUTH_REQUIRED=false`` from accidentally behaving as True.
    """
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


def _parse_log_format(raw: str) -> str:
    """Return a log rendering mode ('auto'/'json'/'text') or raise clearly.

    Beginner note:
    This is the OBS-001 sibling of ``_parse_log_level``. 'auto' (the default)
    renders machine-readable JSON in production and human-readable text in
    development. 'json' or 'text' force one rendering regardless of environment,
    which is handy for testing JSON locally or keeping a prod console readable.
    """
    value = _clean_env_value(raw).lower() or "auto"
    if value not in _VALID_LOG_FORMATS:
        raise SettingsError(
            f"Invalid LOG_FORMAT {value!r}. Expected one of: "
            f"{', '.join(sorted(_VALID_LOG_FORMATS))}."
        )
    return value


def _parse_email_set(raw: str) -> frozenset[str]:
    """Split a comma-separated email list into normalized lowercase values.

    A frozenset is immutable, which is useful here because a settings object is
    meant to describe one startup configuration. Callers can read it, but they
    should not append or remove emails behind the settings module's back.
    """
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
    # Test code passes a tiny mapping so it can control every input precisely.
    # Runtime code passes nothing, so we first load Dependencies/.env and then
    # read the real process environment. python-dotenv uses override=False, so a
    # hosting platform's env vars still beat local .env values.
    if env is None:
        load_environment()
        source: Mapping[str, str] = os.environ
    else:
        source = env

    # APP_ENV is the canonical DEPLOY-004 name. SCANNER_ENV is accepted only as
    # a compatibility alias for older local files.
    app_env = _normalize_app_env(_env_value(source, "APP_ENV", "SCANNER_ENV"))

    # DATA_DIR controls all generated runtime data, not just the database. In
    # development it defaults to the repo's git-ignored data/ folder.
    data_dir_value = _env_value(source, "DATA_DIR")
    data_dir = Path(data_dir_value).expanduser() if data_dir_value else DEFAULT_DATA_DIR

    # DATABASE_URL is optional locally because SQLite is good enough for a fresh
    # checkout. Production validation below requires it to be explicit.
    database_url_value = _env_value(source, "DATABASE_URL")
    database_url = database_url_value or f"sqlite:///{(data_dir / 'scanner.db').as_posix()}"

    # LOG_LEVEL is the normal deploy knob. SCANNER_DEBUG=1 remains a small
    # backwards-compatible shortcut for local debugging.
    log_level_raw = _env_value(source, "LOG_LEVEL")
    if not log_level_raw and _env_value(source, "SCANNER_DEBUG").lower() in _TRUE_VALUES:
        log_level_raw = "DEBUG"

    # Keep construction in one visible block so reviewing a new env var later is
    # straightforward: add the field, parse it here, and test it in
    # tests/test_settings.py.
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
        log_format=_parse_log_format(_env_value(source, "LOG_FORMAT")),
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
    """Fail clearly when production is missing required runtime config.

    Local development optimizes for convenience. Production optimizes for
    explicitness and fail-closed safety: no hidden SQLite fallback, no implicit
    repo-local data directory, no missing Dhan credentials, and no disabled auth.
    """
    settings = settings or get_settings()
    if not settings.is_production:
        return settings

    missing: list[str] = []
    # In production, a local SQLite fallback would be surprising and easy to
    # lose on a redeploy, so DATABASE_URL must be explicitly provided.
    if not settings.database_url_from_env:
        missing.append("DATABASE_URL")
    # DATA_DIR should point at a persistent disk/volume in production. Reusing
    # the development data/ fallback would make generated files ephemeral.
    if not settings.data_dir_from_env:
        missing.append("DATA_DIR")
    # Dhan credentials are required because every real scan needs market data.
    if not settings.dhan_client_id:
        missing.append("DHAN_CLIENT_ID")
    if not settings.dhan_access_token:
        missing.append("DHAN_ACCESS_TOKEN")
    # Production must not expose the scanner without the auth gate.
    if settings.auth_required is False:
        missing.append("AUTH_REQUIRED cannot be false in production")
    # With auth enabled, at least one allowlist/admin email must exist so the
    # app fails closed instead of letting everyone through.
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
    """Return configured secret-like values for redaction helpers.

    The full database URL is included because it may contain a username and
    password. Dhan client ids are included too: they are not passwords, but they
    are account identifiers and should not be echoed into UI error panels.
    """
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
    """Create runtime folders that are safe to generate locally.

    This helper deliberately creates directories, not files. It prepares the
    folder skeleton the app expects while leaving secrets, database contents, and
    downloaded market data under the user's control.
    """
    settings = get_settings()
    # Use settings-derived paths instead of module constants so DATA_DIR can
    # redirect every generated artifact to a persistent volume in deployment.
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
    """Return Dhan credentials from the centralized settings object.

    ``required=False`` is used by the UI so it can show a setup message instead
    of crashing on first launch. ``required=True`` is used by code paths that
    cannot do useful work without credentials.
    """
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
    """Return the Claude model used by this app's Claude Agent SDK features.

    This remains a small standalone helper because ``CLAUDE_AGENT_MODEL`` is a
    feature-tuning knob, not part of DEPLOY-004's production-required settings.
    """
    if load_dotenv is not None:
        load_environment()
    return _clean_env_value(os.getenv("CLAUDE_AGENT_MODEL")) or DEFAULT_FUNDAMENTALS_MODEL


def get_agent_fast_mode() -> bool:
    """Return True when the AI agents should run in low-latency fast mode.

    The env value is intentionally permissive (1/true/yes/on) because this is a
    developer/operator convenience switch, not a security boundary.
    """
    load_environment()
    raw = _clean_env_value(os.getenv("SCANNER_AGENT_FAST_MODE")).lower()
    return raw in _TRUE_VALUES


def credential_status() -> dict[str, object]:
    """Return a UI-friendly credential summary without exposing secrets.

    Streamlit only needs booleans to decide whether to show a "credentials
    missing" message. Returning the actual token here would create an avoidable
    leak path into UI state or test failure output.
    """
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
