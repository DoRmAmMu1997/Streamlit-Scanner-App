from __future__ import annotations

"""Central configuration for paths, public data sources, and credentials.

Beginner note:
This app reads secrets from one local file only: `Dependencies/.env`.
Keeping secrets out of Python files is important because Python files often get
committed or shared. The `.env` file stays local and is ignored by git.
"""

import os
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - handled by credential_status()
    load_dotenv = None


# Resolve every project path from this file's location instead of the user's
# current terminal folder. That makes `streamlit run app.py` work even when the
# command is launched from a slightly different working directory.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPENDENCIES_DIR = PROJECT_ROOT / "Dependencies"
ENV_PATH = DEPENDENCIES_DIR / ".env"
DATA_DIR = PROJECT_ROOT / "data"
UNIVERSE_DIR = DATA_DIR / "universes"
DAILY_CACHE_DIR = DATA_DIR / "cache" / "daily"
SCREENERS_DIR = PROJECT_ROOT / "screeners"

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


@dataclass(frozen=True)
class DhanCredentials:
    """The two fields needed to create an authenticated DhanHQ SDK client."""

    client_code: str
    access_token: str


def ensure_project_dirs() -> None:
    """Create runtime folders that are safe to generate locally."""
    # These folders hold generated files. Creating them at runtime keeps the
    # repository lightweight while still giving the app a predictable layout.
    for path in (DEPENDENCIES_DIR, UNIVERSE_DIR, DAILY_CACHE_DIR, SCREENERS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_environment() -> None:
    """Load only the scanner app's local Dependencies/.env file."""
    if load_dotenv is None:
        # The app can still show a friendly "credentials missing" message even
        # if python-dotenv is not installed, so we avoid crashing here.
        return
    if ENV_PATH.exists():
        # override=False means shell environment variables win over .env values.
        # That is a standard pattern and helps when running in CI or a scheduler.
        load_dotenv(dotenv_path=ENV_PATH, override=False)


def _clean_env_value(value: str | None) -> str:
    """Normalize env values so KEY=value and KEY="value" both work."""
    cleaned = (value or "").strip()
    if cleaned.startswith(('"', "'")) and cleaned.endswith(('"', "'")):
        cleaned = cleaned[1:-1]
    return cleaned.strip()


def get_dhan_credentials(required: bool = False) -> DhanCredentials | None:
    """Return Dhan credentials from Dependencies/.env or the process env."""
    load_environment()
    client_code = _clean_env_value(os.getenv("DHAN_CLIENT_CODE"))
    access_token = _clean_env_value(os.getenv("DHAN_ACCESS_TOKEN"))

    if client_code and access_token:
        return DhanCredentials(client_code=client_code, access_token=access_token)

    if required:
        # Raising only when required lets the UI render a helpful setup message
        # before the user presses "Run screener".
        missing = []
        if not client_code:
            missing.append("DHAN_CLIENT_CODE")
        if not access_token:
            missing.append("DHAN_ACCESS_TOKEN")
        raise RuntimeError(
            f"Missing Dhan credential(s): {', '.join(missing)}. "
            f"Create {ENV_PATH} from Dependencies/.env.example, then run "
            "python Dependencies/dhan_token_setup.py if you need a fresh token."
        )

    return None


def credential_status() -> dict[str, object]:
    """Return a UI-friendly credential summary without exposing secrets."""
    load_environment()
    client_code = _clean_env_value(os.getenv("DHAN_CLIENT_CODE"))
    access_token = _clean_env_value(os.getenv("DHAN_ACCESS_TOKEN"))
    # Never return the actual access token to Streamlit. The UI only needs
    # booleans so it can show whether setup is complete.
    return {
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.exists(),
        "has_client_code": bool(client_code),
        "has_access_token": bool(access_token),
        "ready": bool(client_code and access_token),
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
