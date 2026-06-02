"""Daily candle loading and local caching.

Every real screener will need historical candles. Without this layer, each
screener would have to repeat the same Dhan API calls, cache checks, and error
handling. This module centralizes that work so screeners can stay small.
"""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from backend.config import DAILY_CACHE_DIR, dhan_rate_limit_retry_delays, dhan_request_delay_seconds
from backend.dhan_client import DhanDataClient, DhanRateLimitError


# Module-level logger. Streamlit captures stderr, so logger output appears in the
# terminal that runs the app. Keeping `getLogger(__name__)` instead of
# `getLogger("daily_data_loader")` lets users mute just this module if needed.
logger = logging.getLogger(__name__)


# A progress callback receives (completed_count, total_count, current_symbol).
# The Streamlit UI uses this to drive st.progress(...) and a status line; tests
# and CLI callers can pass None and skip the bookkeeping entirely.
ProgressCallback = Callable[[int, int, str], None]


def _coerce_date(value: date | datetime | str) -> date:
    """Normalize the many date inputs we accept into a `datetime.date`."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    # Strings are parsed leniently: ISO dates, "YYYY/MM/DD", etc. all work.
    parsed = pd.to_datetime(str(value), errors="coerce")
    if pd.isna(parsed):
        raise ValueError(f"Could not interpret value as a date: {value!r}")
    return parsed.date()


def safe_file_stem(value: object) -> str:
    """Turn symbols/security IDs into Windows-safe filename fragments.

    The regex below replaces every character that is NOT an ASCII letter, digit,
    dot, underscore, or hyphen with a single underscore. We also explicitly
    reject values that collapse to `.` or `..` so a malicious or buggy symbol
    cannot produce a path component that climbs out of the cache directory.
    """
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", str(value or "").strip())
    cleaned = cleaned.strip("._")
    if cleaned in {"", ".", ".."}:
        return "unknown"
    return cleaned


def _date_bounds(candles: pd.DataFrame) -> tuple[date | None, date | None]:
    """Return the first/last valid candle dates in a cached frame.

    Cache decisions need both ends of the range. A file with today's candle but
    no old history is not good enough for a long-lookback screener.
    """
    if candles.empty or "timestamp" not in candles.columns:
        return None, None
    timestamps = pd.to_datetime(candles["timestamp"], errors="coerce").dropna()
    if timestamps.empty:
        return None, None
    return timestamps.min().date(), timestamps.max().date()


@dataclass
class BatchLoadResult:
    """One batch fetch result: successful frames plus non-fatal failures."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    failures: list[dict[str, object]] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    api_attempts: int = 0
    rate_limit_retries: int = 0


class DailyDataLoader:
    """
    Fetch daily candles through DhanDataClient and cache them as local Parquet.

    Screeners call this class instead of the Dhan SDK directly. That keeps API
    credentials, response normalization, cache paths, and per-symbol error
    handling out of strategy files.
    """

    def __init__(
        self,
        client: DhanDataClient | None,
        cache_dir: Path | str = DAILY_CACHE_DIR,
        request_delay_seconds: float | None = None,
        rate_limit_retry_delays: list[float] | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
    ):
        # The Dhan client is optional so cache-only callers (the legacy-file
        # cleanup step, the chart UI's `read_cached_history`) can build a loader
        # even when credentials are missing. Fetch methods will fail loudly
        # rather than silently if `client is None` — that is the safer default.
        self.client = client
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.request_delay_seconds = (
            dhan_request_delay_seconds()
            if request_delay_seconds is None
            else max(0.0, float(request_delay_seconds))
        )
        self.rate_limit_retry_delays = (
            dhan_rate_limit_retry_delays()
            if rate_limit_retry_delays is None
            else [max(0.0, float(delay)) for delay in rate_limit_retry_delays]
        )
        self.sleep_func = sleep_func
        # These fields remember the last run for Streamlit status text. They are
        # not used for trading decisions.
        self.last_failures: list[dict[str, object]] = []
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0
        self._api_attempts = 0
        self._rate_limit_retries = 0

    def cache_path(self, symbol: str, security_id: str | int) -> Path:
        """Return the stable cache parquet path for a single stock.

        The cache stores the largest history we have ever fetched for the
        instrument. Date ranges are NOT part of the filename: one file per
        `(symbol, security_id)`, refreshed in place when newer candles arrive.
        """
        file_name = f"{safe_file_stem(symbol)}_{safe_file_stem(security_id)}.parquet"
        return self.cache_dir / file_name

    def checked_path(self, symbol: str, security_id: str | int) -> Path:
        """Return the sidecar that remembers empty incremental checks.

        When Dhan returns no rows for a weekend/holiday/pre-open request, the
        parquet cannot advance. This marker prevents the next app launch from
        paying for the exact same empty request again.
        """
        return self.cache_path(symbol, security_id).with_suffix(".checked")

    def _read_checked_through(self, symbol: str, security_id: str | int) -> date | None:
        """Read the latest date we already checked for an empty increment."""
        path = self.checked_path(symbol, security_id)
        if not path.exists():
            return None
        try:
            return _coerce_date(path.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    def _write_checked_through(
        self,
        symbol: str,
        security_id: str | int,
        checked_date: date,
    ) -> None:
        """Persist the date of a no-new-rows incremental check."""
        try:
            path = self.checked_path(symbol, security_id)
            path.write_text(checked_date.isoformat(), encoding="utf-8")
        except OSError:
            logger.warning("Could not write daily-cache checked marker for %s", symbol)

    def read_cached_history(self, symbol: str, security_id: str | int) -> pd.DataFrame:
        """Return the cached daily candles for one stock; empty DataFrame if missing.

        Used by the chart UI: we want to render whatever is already on disk
        without ever falling back to a live Dhan fetch (that work belongs to
        the CLI prefetch).
        """
        path = self.cache_path(symbol, security_id)
        if not path.exists():
            return pd.DataFrame()
        try:
            return pd.read_parquet(path)
        except Exception:
            logger.exception("Failed to read cached parquet for %s", symbol)
            return pd.DataFrame()

    def _slice_to_range(
        self,
        candles: pd.DataFrame,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
    ) -> pd.DataFrame:
        """Return rows of `candles` whose timestamp falls within [start, end]."""
        if candles.empty or "timestamp" not in candles.columns:
            return candles
        start_ts = pd.Timestamp(_coerce_date(start_date))
        # End is inclusive at end-of-day so a screener asking for today still
        # captures today's daily candle once it lands.
        end_ts = pd.Timestamp(_coerce_date(end_date)) + pd.Timedelta(hours=23, minutes=59, seconds=59)
        timestamps = pd.to_datetime(candles["timestamp"], errors="coerce")
        mask = (timestamps >= start_ts) & (timestamps <= end_ts)
        return candles.loc[mask].reset_index(drop=True)

    def get_daily_history(
        self,
        instrument: Mapping[str, object] | pd.Series,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        force_refresh: bool = False,
    ) -> tuple[pd.DataFrame, bool]:
        """
        Return daily candles for one instrument, sliced to the requested range.

        The boolean indicates whether the result was answered without hitting
        Dhan (i.e., served entirely from the local Parquet cache).
        """
        row = dict(instrument)
        # Universe CSV rows are the source of truth for how to ask Dhan for a
        # stock. Defaults keep manual CSVs usable if optional columns are blank.
        symbol = str(row.get("symbol", "")).strip().upper()
        security_id = str(row.get("security_id", "")).strip()
        exchange_segment = str(row.get("exchange_segment", "NSE_EQ") or "NSE_EQ").strip()
        instrument_type = str(row.get("instrument_type", "EQUITY") or "EQUITY").strip()

        if not symbol:
            raise ValueError("Instrument row is missing symbol")
        if not security_id:
            raise ValueError(f"{symbol} is missing security_id")

        path = self.cache_path(symbol, security_id)
        if path.exists() and not force_refresh:
            # Cache hit only when the file covers the entire requested range.
            # A partial parquet is common after interrupted prefetches; slicing
            # it would silently run long-lookback screeners on too little data.
            cached = pd.read_parquet(path)
            first_date, last_date = _date_bounds(cached)
            requested_start = _coerce_date(start_date)
            requested_end = _coerce_date(end_date)
            if (
                first_date is not None
                and last_date is not None
                and first_date <= requested_start
                and last_date >= requested_end
            ):
                return self._slice_to_range(cached, start_date, end_date), True

        # Cache miss (or force_refresh): fetch the requested window from Dhan
        # and save under the stable filename for future calls.
        candles = self._fetch_with_rate_limit_retries(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=start_date,
            to_date=end_date,
        )
        if not candles.empty:
            path.parent.mkdir(parents=True, exist_ok=True)
            candles.to_parquet(path, index=False)
        return self._slice_to_range(candles, start_date, end_date), False

    def ensure_daily_history(
        self,
        instrument: Mapping[str, object] | pd.Series,
        years_back: int = 10,
        today: date | None = None,
    ) -> tuple[pd.DataFrame, str]:
        """Top up a single stock's cached parquet to cover today's data.

        Returns `(frame, status)` where `status` is one of:
          - "fresh"           cache already covers today, no API call
          - "incremental"     fetched and appended N missing days
          - "fresh_download"  no cache existed, fetched the full window
          - "backfilled"      cache existed but missed older requested history

        This is the engine behind the CLI prefetch. The Streamlit UI never
        calls it directly; it reads whatever is already on disk via
        `read_cached_history(...)` or `get_daily_history(...)`.
        """
        row = dict(instrument)
        symbol = str(row.get("symbol", "")).strip().upper()
        security_id = str(row.get("security_id", "")).strip()
        exchange_segment = str(row.get("exchange_segment", "NSE_EQ") or "NSE_EQ").strip()
        instrument_type = str(row.get("instrument_type", "EQUITY") or "EQUITY").strip()
        if not symbol:
            raise ValueError("Instrument row is missing symbol")
        if not security_id:
            raise ValueError(f"{symbol} is missing security_id")

        today = today or date.today()
        # `replace(year=...)` is the simplest way to subtract whole years. The
        # Feb-29 guard handles the leap-day edge case: stepping back from
        # 2024-02-29 by 10y lands on 2014-02-29, which doesn't exist.
        try:
            start = today.replace(year=today.year - int(years_back))
        except ValueError:
            start = today.replace(month=2, day=28, year=today.year - int(years_back))

        path = self.cache_path(symbol, security_id)
        if not path.exists():
            candles = self._fetch_with_rate_limit_retries(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=start,
                to_date=today,
            )
            if not candles.empty:
                path.parent.mkdir(parents=True, exist_ok=True)
                candles.to_parquet(path, index=False)
            return candles, "fresh_download"

        cached = pd.read_parquet(path)
        if cached.empty or "timestamp" not in cached.columns:
            # Defensive: a corrupt/empty cache is treated like no cache. Refetch
            # the full window so the user is not stuck with a bad file.
            candles = self._fetch_with_rate_limit_retries(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=start,
                to_date=today,
            )
            if not candles.empty:
                candles.to_parquet(path, index=False)
            return candles, "fresh_download"

        first_date, last_date = _date_bounds(cached)
        if first_date is None or last_date is None:
            # The parquet had a timestamp column but every value was NaT. Treat
            # that like a corrupted cache: refetch the full window so we end
            # up with usable data.
            candles = self._fetch_with_rate_limit_retries(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=start,
                to_date=today,
            )
            if not candles.empty:
                candles.to_parquet(path, index=False)
            return candles, "fresh_download"

        if first_date > start:
            # The cache may be current at the back but missing years at the
            # front, usually after an old interrupted prefetch. Refetch the
            # intended full window so long-lookback screeners see real history.
            candles = self._fetch_with_rate_limit_retries(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=start,
                to_date=today,
            )
            if not candles.empty:
                candles.to_parquet(path, index=False)
                return candles, "backfilled"
            return cached, "fresh"

        if last_date >= today:
            return cached, "fresh"

        checked_through = self._read_checked_through(symbol, security_id)
        if checked_through is not None and checked_through >= today:
            # We already asked Dhan for this exact missing tail and got no rows
            # (weekend, holiday, or pre-open). Avoid repeating the same empty
            # request on every app launch.
            return cached, "fresh"

        # Incremental top-up: request from (last_date + 1) so we never re-pay
        # for candles already in the parquet. The merge below also dedupes in
        # case Dhan happens to return an overlap.
        incremental_start = last_date + timedelta(days=1)
        new_rows = self._fetch_with_rate_limit_retries(
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=incremental_start,
            to_date=today,
        )
        if new_rows.empty:
            # The API had no new candles (weekend, holiday, or pre-open).
            # Treat that as "fresh" so the progress line stays accurate.
            self._write_checked_through(symbol, security_id, today)
            return cached, "fresh"

        merged = (
            pd.concat([cached, new_rows], ignore_index=True)
            .drop_duplicates(subset=["timestamp"], keep="last")
            .sort_values("timestamp")
            .reset_index(drop=True)
        )
        merged.to_parquet(path, index=False)
        return merged, "incremental"

    def _sleep(self, seconds: float) -> None:
        """Sleep only when a positive delay is configured."""
        if seconds > 0:
            self.sleep_func(float(seconds))

    def _fetch_with_rate_limit_retries(
        self,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        from_date: date | datetime | str,
        to_date: date | datetime | str,
    ) -> pd.DataFrame:
        """Fetch one symbol with a gentle pause and deterministic DH-904 backoff."""
        # The Stochastic data-fetch script uses a small pause between symbols so
        # it does not hammer Dhan. We apply that pause only for real cache misses,
        # never when reading local Parquet.
        self._sleep(self.request_delay_seconds)

        retry_index = 0
        while True:
            try:
                self._api_attempts += 1
                self.last_api_attempts = self._api_attempts
                return self.client.fetch_daily_candles(
                    security_id=security_id,
                    exchange_segment=exchange_segment,
                    instrument_type=instrument_type,
                    from_date=from_date,
                    to_date=to_date,
                )
            except DhanRateLimitError:
                if retry_index >= len(self.rate_limit_retry_delays):
                    raise
                delay = self.rate_limit_retry_delays[retry_index]
                retry_index += 1
                self._rate_limit_retries += 1
                self.last_rate_limit_retries = self._rate_limit_retries
                self._sleep(delay)

    def load_universe_history(
        self,
        universe_df: pd.DataFrame,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        max_symbols: int | None = None,
        force_refresh: bool = False,
        progress_callback: ProgressCallback | None = None,
    ) -> BatchLoadResult:
        """Fetch daily history for a universe while collecting per-symbol failures.

        `max_symbols` is kept on the API for backwards compatibility with tests
        and external callers, but the Streamlit UI no longer exposes it; in
        normal use this method scans every mapped row in the universe.

        `progress_callback`, when provided, is called once per processed symbol
        with `(completed_count, total_count, current_symbol)`. The Streamlit UI
        uses that to drive a live progress bar; tests pass `None`.
        """
        self._api_attempts = 0
        self._rate_limit_retries = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0

        if universe_df.empty:
            result = BatchLoadResult()
            self._remember(result)
            return result

        work = universe_df.copy()
        if "mapping_status" in work.columns:
            # Only mapped rows have a Dhan security_id. Missing mappings are
            # kept in the CSV for visibility, but cannot be fetched.
            work = work.loc[work["mapping_status"].astype(str).str.lower().eq("mapped")].copy()
        if max_symbols is not None and int(max_symbols) > 0:
            # Tests and CLI callers may still cap the batch; the UI does not.
            work = work.head(int(max_symbols)).copy()

        total = len(work)
        result = BatchLoadResult()
        for index, row in enumerate(work.to_dict("records"), start=1):
            symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
            try:
                candles, from_cache = self.get_daily_history(
                    row,
                    start_date=start_date,
                    end_date=end_date,
                    force_refresh=force_refresh,
                )
                if from_cache:
                    result.cache_hits += 1
                else:
                    result.cache_misses += 1
                result.frames[symbol] = candles
            except Exception as exc:
                # One bad symbol should not kill the whole scan. The screener
                # can still work with the symbols that fetched successfully.
                # We log with `exception(...)` so the full traceback reaches the
                # terminal, but the UI only shows a short message.
                logger.exception("Failed to load history for %s", symbol)
                result.failures.append(
                    {
                        "symbol": symbol,
                        "security_id": row.get("security_id", ""),
                        "message": str(exc),
                    }
                )

            if progress_callback is not None:
                # The callback runs after each symbol, regardless of cache hit /
                # miss / failure, so the UI bar always advances monotonically.
                try:
                    progress_callback(index, total, symbol)
                except Exception:
                    # A broken UI callback must never crash the batch. Log and
                    # carry on so the screener still receives its frames.
                    logger.exception("Progress callback raised for %s", symbol)

        result.api_attempts = self._api_attempts
        result.rate_limit_retries = self._rate_limit_retries
        self._remember(result)
        return result

    def _remember(self, result: BatchLoadResult) -> None:
        """Expose the latest batch summary to the Streamlit UI."""
        self.last_failures = result.failures
        self.last_cache_hits = result.cache_hits
        self.last_cache_misses = result.cache_misses
        self.last_api_attempts = result.api_attempts
        self.last_rate_limit_retries = result.rate_limit_retries

    def cleanup_legacy_cache_files(self) -> int:
        """Delete parquet files that follow the legacy date-suffixed naming.

        The previous cache layout encoded the requested date range in the
        filename, which meant subtly different scan windows wrote separate
        files. The new layout is `{symbol}_{security_id}.parquet`. Any file
        whose stem ends with two 8-digit date strings (e.g.
        `RELIANCE_2885_20150601_20250601.parquet`) is leftover from the old
        layout and is safe to remove. Returns the count deleted.
        """
        if not self.cache_dir.exists():
            return 0
        deleted = 0
        for path in self.cache_dir.glob("*.parquet"):
            parts = path.stem.split("_")
            # The legacy pattern has at least four parts and the LAST two must
            # both be 8-digit numeric date strings. That avoids accidentally
            # deleting a symbol that happens to legitimately contain numbers.
            if len(parts) >= 4 and parts[-2].isdigit() and parts[-1].isdigit() and len(parts[-2]) == 8 and len(parts[-1]) == 8:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    logger.exception("Could not delete legacy cache file %s", path)
        return deleted
