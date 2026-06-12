"""Passive operational health collection for the OBS-002 admin page.

The health page is intended for an operator who is answering questions such as
"Did the last scan finish?", "Is cached market data current?", and "Is this
deployment configured to use its optional providers?" It deliberately performs
only local reads:

- scan-history queries use the existing SQLAlchemy repository;
- cache checks inspect file metadata and Parquet metadata;
- provider checks inspect settings or package installation.

It does **not** call Dhan, Claude, SerpAPI, or any other network service. This is
important because merely opening an operational page must not consume quota,
trigger rate limits, or turn a provider outage into a slow Streamlit rerun.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from backend.config.settings import AppSettings, get_settings
from backend.storage import ScanStatus, get_latest_scan_runs, session_scope


@dataclass(frozen=True)
class ServiceHealth:
    """A secret-safe readiness result for one dependency.

    ``status`` is a small display-oriented value: ``ready`` means the local
    prerequisite exists, ``warning`` means setup is incomplete, and
    ``unavailable`` means a local readiness operation failed. ``detail`` must
    never contain a credential, connection URL, or raw exception message.
    """

    name: str
    status: str
    detail: str


@dataclass(frozen=True)
class ScanRunHealth:
    """Plain scalar fields copied from a persisted scan while its session is open.

    SQLAlchemy model instances can become detached after their session closes.
    Copying only the values needed by the UI makes the snapshot safe to cache
    for Streamlit's 60-second health-page window.
    """

    run_id: int
    started_at: dt.datetime
    finished_at: dt.datetime | None
    screener_key: str
    universe_key: str
    symbols_scanned: int | None
    triggered_by: str | None
    error_message: str | None


@dataclass(frozen=True)
class AdminHealthSnapshot:
    """One immutable, cacheable view of local application readiness."""

    last_successful_scan: ScanRunHealth | None
    last_failed_scan: ScanRunHealth | None
    last_data_refresh: dt.datetime | None
    cached_symbol_count: int
    latest_candle_date: dt.date | None
    unreadable_cache_file_count: int
    cache_size_bytes: int
    data_size_bytes: int
    disk_free_bytes: int | None
    services: tuple[ServiceHealth, ...]


@dataclass(frozen=True)
class _CacheInspection:
    """Internal file-inspection result used to assemble the public snapshot."""

    cached_symbol_count: int
    latest_candle_date: dt.date | None
    unreadable_file_count: int
    cache_size_bytes: int
    last_cache_refresh: dt.datetime | None


def collect_admin_health(settings: AppSettings | None = None) -> AdminHealthSnapshot:
    """Collect the admin page's passive readiness snapshot.

    A caller may provide settings in tests; normal application code uses the
    same central ``get_settings()`` object as the rest of the app. Every
    exception boundary is intentionally narrow:

    - database failures become an ``unavailable`` service with only the Python
      exception type;
    - unreadable Parquet files increase a count while healthy files remain
      inspectable;
    - storage-stat failures produce ``None`` instead of taking down the page.

    No exception message is copied into a ``ServiceHealth`` result because
    database drivers and SDK setup errors commonly echo URLs or credentials.
    """
    current_settings = settings or get_settings()
    cache = _inspect_candle_cache(current_settings.daily_cache_dir)
    universe_refresh = _latest_file_mtime(
        current_settings.universe_dir.glob("*.csv")
    )
    last_data_refresh = _latest_datetime(cache.last_cache_refresh, universe_refresh)
    data_size_bytes = _directory_size(current_settings.data_dir)
    disk_free_bytes = _disk_free_space(current_settings.data_dir)

    successful_scan: ScanRunHealth | None = None
    failed_scan: ScanRunHealth | None = None
    try:
        with session_scope() as session:
            successful_runs = get_latest_scan_runs(
                session,
                limit=1,
                status=ScanStatus.SUCCESS,
            )
            failed_runs = get_latest_scan_runs(
                session,
                limit=1,
                status=ScanStatus.FAILED,
            )
            if successful_runs:
                successful_scan = _copy_scan_run(successful_runs[0])
            if failed_runs:
                failed_scan = _copy_scan_run(failed_runs[0])
        database_health = ServiceHealth(
            "Database",
            "ready",
            "Scan-history queries succeeded.",
        )
    except Exception as exc:  # noqa: BLE001 - health must degrade, not crash.
        database_health = ServiceHealth(
            "Database",
            "unavailable",
            f"Health query failed ({type(exc).__name__}).",
        )

    return AdminHealthSnapshot(
        last_successful_scan=successful_scan,
        last_failed_scan=failed_scan,
        last_data_refresh=last_data_refresh,
        cached_symbol_count=cache.cached_symbol_count,
        latest_candle_date=cache.latest_candle_date,
        unreadable_cache_file_count=cache.unreadable_file_count,
        cache_size_bytes=cache.cache_size_bytes,
        data_size_bytes=data_size_bytes,
        disk_free_bytes=disk_free_bytes,
        services=(
            database_health,
            _dhan_health(current_settings),
            _claude_health(),
            _serpapi_health(current_settings),
        ),
    )


def _copy_scan_run(run: Any) -> ScanRunHealth:
    """Detach the small set of scan fields used by the health page."""
    return ScanRunHealth(
        run_id=int(run.id),
        started_at=run.started_at,
        finished_at=run.finished_at,
        screener_key=str(run.screener_key),
        universe_key=str(run.universe_key),
        symbols_scanned=run.symbols_scanned,
        triggered_by=run.triggered_by,
        error_message=run.error_message,
    )


def _inspect_candle_cache(cache_dir: Path) -> _CacheInspection:
    """Inspect every daily Parquet file without loading normal candle data.

    Parquet stores optional minimum/maximum statistics per row group. The
    maximum timestamp is enough to answer "how current is the cache?" and is
    much cheaper than reading each full timestamp column. Older/unusual files
    may omit those statistics; only those files use the column fallback.
    """
    paths = sorted(cache_dir.glob("*.parquet")) if cache_dir.exists() else []
    latest_date: dt.date | None = None
    unreadable = 0
    cache_size = 0
    latest_refresh: dt.datetime | None = None

    for path in paths:
        try:
            stat = path.stat()
            cache_size += stat.st_size
            latest_refresh = _latest_datetime(
                latest_refresh,
                dt.datetime.fromtimestamp(stat.st_mtime, tz=dt.UTC),
            )
            file_date = _latest_timestamp_from_metadata(path)
            if file_date is None:
                file_date = _latest_timestamp_from_column(path)
            if file_date is not None and (latest_date is None or file_date > latest_date):
                latest_date = file_date
        except (OSError, ValueError, TypeError, KeyError, IndexError):
            unreadable += 1
        except Exception:
            # PyArrow raises several format-specific exception classes for
            # corrupt files. Treat those files as unreadable without copying a
            # potentially path-bearing exception message into the snapshot.
            unreadable += 1

    return _CacheInspection(
        cached_symbol_count=len(paths),
        latest_candle_date=latest_date,
        unreadable_file_count=unreadable,
        cache_size_bytes=cache_size,
        last_cache_refresh=latest_refresh,
    )


def _latest_timestamp_from_metadata(path: Path) -> dt.date | None:
    """Return a Parquet file's maximum timestamp statistic when available."""
    parquet_file = pq.ParquetFile(path)
    timestamp_index = parquet_file.schema_arrow.get_field_index("timestamp")
    if timestamp_index < 0:
        raise KeyError("timestamp")

    latest: dt.date | None = None
    metadata = parquet_file.metadata
    for row_group_index in range(metadata.num_row_groups):
        column = metadata.row_group(row_group_index).column(timestamp_index)
        statistics = column.statistics
        # PyArrow exposes one flag for the min/max pair. Older versions do not
        # provide a separate ``has_max`` attribute, so use the stable
        # ``has_min_max`` API before reading ``statistics.max``.
        if statistics is None or not statistics.has_min_max:
            return None
        candidate = _as_date(statistics.max)
        if candidate is not None and (latest is None or candidate > latest):
            latest = candidate
    return latest


def _latest_timestamp_from_column(path: Path) -> dt.date | None:
    """Fallback for Parquet files whose timestamp metadata has no max value."""
    table = pq.read_table(path, columns=["timestamp"])
    column = table.column("timestamp")
    latest: dt.date | None = None
    for value in column.to_pylist():
        candidate = _as_date(value)
        if candidate is not None and (latest is None or candidate > latest):
            latest = candidate
    return latest


def _as_date(value: Any) -> dt.date | None:
    """Normalize common PyArrow timestamp-statistic values to a date."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    return dt.datetime.fromisoformat(str(value)).date()


def _latest_file_mtime(paths) -> dt.datetime | None:
    """Return the newest modification time among readable generated files."""
    latest: dt.datetime | None = None
    for path in paths:
        try:
            modified = dt.datetime.fromtimestamp(path.stat().st_mtime, tz=dt.UTC)
        except OSError:
            continue
        latest = _latest_datetime(latest, modified)
    return latest


def _latest_datetime(
    first: dt.datetime | None,
    second: dt.datetime | None,
) -> dt.datetime | None:
    """Return the later optional timestamp."""
    if first is None:
        return second
    if second is None:
        return first
    return max(first, second)


def _directory_size(directory: Path) -> int:
    """Best-effort recursive byte count for generated application data."""
    if not directory.exists():
        return 0
    total = 0
    for path in directory.rglob("*"):
        if not path.is_file():
            continue
        try:
            total += path.stat().st_size
        except OSError:
            continue
    return total


def _disk_free_space(data_dir: Path) -> int | None:
    """Return free bytes on the volume that stores generated application data."""
    probe = data_dir
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        return int(shutil.disk_usage(probe).free)
    except OSError:
        return None


def _dhan_health(settings: AppSettings) -> ServiceHealth:
    """Describe Dhan credential completeness without constructing its client."""
    has_client = bool(settings.dhan_client_id)
    has_token = bool(settings.dhan_access_token)
    if has_client and has_token:
        return ServiceHealth("Dhan", "ready", "Credentials are configured.")
    if has_client or has_token:
        return ServiceHealth(
            "Dhan",
            "warning",
            "Credential configuration is incomplete.",
        )
    return ServiceHealth("Dhan", "warning", "Credentials are not configured.")


def _claude_health() -> ServiceHealth:
    """Describe local Claude Agent SDK availability without signing in."""
    try:
        installed = importlib.util.find_spec("claude_agent_sdk") is not None
    except Exception as exc:  # noqa: BLE001 - report only the safe exception type.
        return ServiceHealth(
            "Claude Agent SDK",
            "unavailable",
            f"Package check failed ({type(exc).__name__}).",
        )
    if installed:
        return ServiceHealth(
            "Claude Agent SDK",
            "ready",
            "SDK is installed; sign-in is not live-tested.",
        )
    return ServiceHealth(
        "Claude Agent SDK",
        "warning",
        "SDK is not installed.",
    )


def _serpapi_health(settings: AppSettings) -> ServiceHealth:
    """Describe SerpAPI configuration without issuing a search request."""
    if settings.serpapi_api_key:
        return ServiceHealth("SerpAPI", "ready", "API key is configured.")
    return ServiceHealth("SerpAPI", "warning", "API key is not configured.")
