"""Daily candle loading and local caching.

Every real screener will need historical candles. Without this layer, each
screener would have to repeat the same Dhan API calls, cache checks, and error
handling. This module centralizes that work so screeners can stay small.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

from backend.config import (
    DAILY_CACHE_DIR,
    dhan_fetch_workers,
    dhan_rate_limit_retry_delays,
    dhan_request_delay_seconds,
)
from backend.data_quality import CandleQualityReport, validate_candles
from backend.dhan_client import DhanDataClient, DhanRateLimitError
from backend.observability import (
    EVENT_CANDLE_DATA_QUALITY_FAILED,
    EVENT_CANDLE_DATA_QUALITY_WARNING,
    EVENT_EXTERNAL_API_FAILED,
    log_event,
)
from backend.parquet_stats import timestamp_bounds
from backend.security import redact_text

# Module-level logger. Streamlit captures stderr, so logger output appears in the
# terminal that runs the app. Keeping `getLogger(__name__)` instead of
# `getLogger("daily_data_loader")` lets users mute just this module if needed.
logger = logging.getLogger(__name__)


# Default candle-history window shared by the Streamlit prefetch/scan path and the
# headless daily job. Keeping it (and the leap-safe "subtract whole years" math in
# ``history_start_date``) in one place stops the three callers from drifting apart.
DEFAULT_HISTORY_YEARS_BACK = 10

# How long we trust a recorded "the vendor has nothing earlier than this" answer
# before probing again. Vendors do occasionally backfill history, so the belief
# expires rather than becoming permanent; a month keeps the cost negligible
# (one request per affected symbol per month) while never hiding real data for
# long. Mirrors the DATA-002 ``REPAIR_RETRY_AFTER_DAYS`` cooldown precedent.
VENDOR_EARLIEST_RECHECK_DAYS = 30


def history_start_date(
    years_back: int = DEFAULT_HISTORY_YEARS_BACK, today: date | None = None
) -> date:
    """Return ``today`` minus ``years_back`` whole years, safe for Feb 29.

    ``date.replace(year=...)`` is the simplest way to subtract whole years, but it
    raises ``ValueError`` on Feb 29 when the target year is not a leap year (for
    example 2024-02-29 minus 10y lands on 2014-02-29, which does not exist). In
    that case we fall back to Feb 28 so the cached candle window and every scan
    caller agree on the same start date.
    """
    selected_date = today or date.today()
    try:
        return selected_date.replace(year=selected_date.year - int(years_back))
    except ValueError:
        return selected_date.replace(
            month=2, day=28, year=selected_date.year - int(years_back)
        )


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


# The longest gap the weekday walk below can ever accept is a long weekend, so a
# wider gap is rejected without walking it. Purely a performance guard: a decade-old
# cache should not iterate 3,650 days to reach the same "no" a subtraction gives.
_MAX_TOLERABLE_GAP_DAYS = 7


def _only_unpublished_days_missing(last_date: date, requested_end: date) -> bool:
    """True when nothing the market has actually published is missing from the cache.

    Two kinds of day may be absent without the cache being out of date:

    - **weekends**, when the exchange did not trade at all; and
    - **``requested_end`` itself**, because the current session's end-of-day bar is
      routinely not published yet when a scan runs.

    That second carve-out is load-bearing. Without it every weekday scan would be a
    miss again, which is the whole problem DATA-003 set out to fix.

    Any *other* missing weekday means a bar the market really did publish is absent,
    so the cache is genuinely behind and must be refreshed. This replaces an earlier
    blanket "tolerate four calendar days" rule, which silently served a cache ending
    on Thursday to a Monday scan even though Friday's bar existed.
    """
    if last_date >= requested_end:
        return True
    if (requested_end - last_date).days > _MAX_TOLERABLE_GAP_DAYS:
        return False
    day = last_date + timedelta(days=1)
    while day < requested_end:
        if day.weekday() < 5:  # Monday-Friday
            return False
        day += timedelta(days=1)
    return True


def _cache_covers_range(
    first_date: date | None,
    last_date: date | None,
    requested_start: date,
    requested_end: date,
    vendor_earliest: date | None = None,
    checked_through: date | None = None,
) -> bool:
    """Return True when a cached range is good enough to answer a request.

    The **start** is compared strictly: a parquet missing early history (common
    after an interrupted prefetch) must never silently run a long-lookback
    screener on too little data.

    The **end** is satisfied by evidence rather than by a time window, in one of
    two ways:

    - ``_only_unpublished_days_missing`` — nothing absent but weekends and possibly
      the current session's own unpublished bar; or
    - ``checked_through`` — the loader's existing ``.checked`` marker, written by
      the prefetch precisely when it asked Dhan for this tail and got nothing back.
      That covers market holidays, which are weekdays and so fail the arithmetic
      above despite there being no bar to fetch.

    Note this rule and DATA-001's ``STALE_LATEST_CANDLE`` warning cover **disjoint**
    ranges: the warning fires only when the newest bar trails by *more* than
    ``STALE_LATEST_TOLERANCE_DAYS``, so anything served here is below its threshold
    and passes silently. An earlier version of this docstring claimed the warning
    still fired as a backstop — it does not, which is exactly why the end test has
    to stand on its own evidence rather than on a tolerance window.
    """
    if first_date is None or last_date is None:
        return False
    # Front and back are judged by separate evidence: how far the vendor's history
    # goes (DATA-004) and which recent days it has actually published (DATA-003).
    if not _cache_reaches_back_far_enough(first_date, requested_start, vendor_earliest):
        return False
    if _only_unpublished_days_missing(last_date, requested_end):
        return True
    return checked_through is not None and checked_through >= requested_end


def _cache_reaches_back_far_enough(
    first_date: date,
    requested_start: date,
    vendor_earliest: date | None,
) -> bool:
    """Return True when nothing earlier is missing that could still be fetched.

    Normally that means the cache literally reaches ``requested_start``. But a
    stock that listed *after* that date can never satisfy it — DhanHQ has nothing
    earlier to give — and demanding it made 200 of 577 symbols a permanent cache
    miss, re-downloading their full history on every prefetch and every scan
    (DATA-004).

    ``vendor_earliest`` is the earliest bar the vendor actually served for a probe
    that reached at least as far back as this request (see
    ``DailyDataLoader._vendor_earliest_for``). When the cache already starts at or
    before that bar, it is as complete as it can ever be, so asking again is pure
    waste. ``None`` means we have no such evidence and the strict rule applies —
    which keeps an interrupted prefetch's partial file being refetched, since
    there the vendor genuinely does have the missing years.
    """
    if first_date <= requested_start:
        return True
    return vendor_earliest is not None and first_date <= vendor_earliest


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


class _RequestPacer:
    """Enforce a minimum spacing between Dhan requests across worker threads.

    The sequential loader pauses a fixed delay before each cache-miss fetch.
    With worker threads, a per-thread pause would multiply the request rate by
    the worker count. This pacer keeps ONE global schedule instead: each caller
    reserves the next free slot under a lock, then sleeps outside the lock
    until its slot arrives, so the broker sees at most one request per delay
    window no matter how many workers are fetching.
    """

    def __init__(
        self,
        delay_seconds: float,
        sleep_func: Callable[[float], None],
        time_func: Callable[[], float] = time.monotonic,
    ) -> None:
        self.delay_seconds = max(0.0, float(delay_seconds))
        self._sleep_func = sleep_func
        self._time_func = time_func
        self._lock = threading.Lock()
        self._next_slot = 0.0

    def wait(self) -> None:
        """Block until this caller's reserved request slot arrives."""
        if self.delay_seconds <= 0:
            return
        with self._lock:
            now = self._time_func()
            slot = max(now, self._next_slot)
            self._next_slot = slot + self.delay_seconds
        pause = slot - now
        if pause > 0:
            self._sleep_func(pause)


@dataclass(frozen=True)
class PrefetchOutcome:
    """One streamed result from ``iter_ensure_universe_history``.

    ``status`` is one of ``ensure_daily_history``'s cache statuses ("fresh",
    "incremental", "fresh_download", "backfilled") or "failed", in which case
    ``message`` carries a redacted error description.
    """

    symbol: str
    status: str
    message: str | None = None


@dataclass
class BatchLoadResult:
    """One batch fetch result: successful frames plus non-fatal failures."""

    frames: dict[str, pd.DataFrame] = field(default_factory=dict)
    failures: list[dict[str, object]] = field(default_factory=list)
    # DATA-001B: one CandleQualityReport per symbol that was loaded and checked.
    # The scan service folds these into the per-run quality receipt.
    data_quality_reports: list[CandleQualityReport] = field(default_factory=list)
    cache_hits: int = 0
    cache_misses: int = 0
    api_attempts: int = 0
    rate_limit_retries: int = 0


@dataclass
class HistoryLoadItem:
    """One streamed loader event for a single symbol.

    `load_universe_history(...)` still returns a batch object for old callers.
    New streaming callers can consume these items one at a time, compute the
    screener result immediately, and avoid holding every candle frame in a big
    dictionary before strategy work starts.
    """

    symbol: str
    candles: pd.DataFrame = field(default_factory=pd.DataFrame)
    from_cache: bool = False
    failure: dict[str, object] | None = None


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
        fetch_timeout_seconds: float | None = None,
        max_consecutive_failures: int | None = None,
        sleep_func: Callable[[float], None] = time.sleep,
        fetch_workers: int | None = None,
        today_func: Callable[[], date] = date.today,
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
        self.fetch_timeout_seconds = (
            None
            if fetch_timeout_seconds is None or float(fetch_timeout_seconds) <= 0
            else float(fetch_timeout_seconds)
        )
        self.max_consecutive_failures = max(0, int(max_consecutive_failures or 0))
        self.sleep_func = sleep_func
        # Wall clock, injected like sleep_func so tests can pin it. Deliberately
        # separate from the ``today`` argument callers pass to describe a DATA
        # window: "the date I am asking about" and "the date it is now" are
        # different questions, and conflating them let a marker's 30-day expiry be
        # judged against a historical request boundary, so it never expired.
        self.today_func = today_func
        # PERF-001: 1 (the default) keeps the long-standing sequential path
        # byte-identical. Values above 1 fetch with a thread pool while the
        # shared pacer holds the global inter-request delay.
        self.fetch_workers = (
            dhan_fetch_workers() if fetch_workers is None else min(8, max(1, int(fetch_workers)))
        )
        self._pacer = _RequestPacer(self.request_delay_seconds, self.sleep_func)
        self._stats_lock = threading.Lock()
        # These fields remember the last run for Streamlit status text. They are
        # not used for trading decisions.
        self.last_failures: list[dict[str, object]] = []
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0
        self.last_data_quality_reports: list[CandleQualityReport] = []
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

    def first_bar_path(self, symbol: str, security_id: str | int) -> Path:
        """Return the sidecar recording how far back the vendor's history goes.

        A third marker alongside ``.checked`` (an empty tail) and ``.repaired``
        (a repair cooldown), following the same pattern: remember an answer the
        vendor already gave so we do not pay for the identical request forever.
        """
        return self.cache_path(symbol, security_id).with_suffix(".firstbar")

    def _vendor_earliest_for(
        self, symbol: str, security_id: str | int, requested_start: date
    ) -> date | None:
        """The vendor's earliest bar, when we have evidence that answers this request.

        Returns ``None`` — meaning "no evidence, apply the strict rule" — unless
        all three hold:

        - a marker exists and parses;
        - it was recorded within ``VENDOR_EARLIEST_RECHECK_DAYS`` of **now**
          (vendors do backfill occasionally, so the belief expires). Age is
          measured against the injected wall clock, never against the requested
          window: judging it by a request boundary meant a repeated historical
          request always computed an age of zero and the marker never expired.
        - the recorded probe reached **at least as far back** as this request.
          Learning that nothing exists before 2021 when you only asked from 2021
          says nothing about 2016, so a shallower probe must not suppress a
          deeper refetch.

        Any read or parse problem returns ``None``, so a corrupt marker can only
        ever cost an extra request — never hide history that really is missing.
        """
        path = self.first_bar_path(symbol, security_id)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            requested_from = _coerce_date(str(payload["requested_from"]))
            earliest_available = _coerce_date(str(payload["earliest_available"]))
            recorded_on = _coerce_date(str(payload["recorded_on"]))
        except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            return None
        if (self.today_func() - recorded_on).days >= VENDOR_EARLIEST_RECHECK_DAYS:
            return None
        if requested_from > requested_start:
            return None
        return earliest_available

    def _write_vendor_earliest(
        self,
        symbol: str,
        security_id: str | int,
        *,
        requested_from: date,
        earliest_available: date,
        recorded_on: date,
    ) -> None:
        """Persist what the vendor served, so the next pass need not ask again."""
        try:
            self.first_bar_path(symbol, security_id).write_text(
                json.dumps(
                    {
                        "requested_from": requested_from.isoformat(),
                        "earliest_available": earliest_available.isoformat(),
                        "recorded_on": recorded_on.isoformat(),
                    }
                ),
                encoding="utf-8",
            )
        except OSError:
            # The marker is an optimisation, never a correctness requirement.
            logger.warning("Could not write daily-cache first-bar marker for %s", symbol)

    def _record_vendor_earliest(
        self,
        symbol: str,
        security_id: str | int,
        *,
        requested_from: date | datetime | str,
        candles: pd.DataFrame,
    ) -> None:
        """Record the vendor's earliest bar when it fell short of what we asked for.

        Called after any full-window download. A response that *does* reach the
        requested start teaches us nothing worth storing, and an empty response is
        no evidence at all, so both are skipped.

        ``recorded_on`` is stamped from the injected wall clock rather than from the
        caller's requested window, so the marker ages in real time whatever range
        was asked for.
        """
        if candles.empty:
            return
        first_date, _last_date = _date_bounds(candles)
        if first_date is None:
            return
        start = _coerce_date(requested_from)
        if first_date <= start:
            return
        self._write_vendor_earliest(
            symbol,
            security_id,
            requested_from=start,
            earliest_available=first_date,
            recorded_on=self.today_func(),
        )

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
            #
            # PERF-002: ask the Parquet footer for the bounds first. When the
            # file does NOT cover the range, this skips decompressing a
            # multi-year frame that would be thrown away for a Dhan refetch.
            # A footer that cannot answer (missing statistics, odd writer)
            # falls back to the original full read, so no file that used to
            # count as a cache hit can become a miss.
            cached: pd.DataFrame | None = None
            first_date, last_date = timestamp_bounds(path)
            if first_date is None or last_date is None:
                cached = pd.read_parquet(path)
                first_date, last_date = _date_bounds(cached)
            requested_start = _coerce_date(start_date)
            requested_end = _coerce_date(end_date)
            vendor_earliest = self._vendor_earliest_for(
                symbol, security_id, requested_start
            )
            # The prefetch's marker is the only evidence that distinguishes a
            # market holiday (a weekday with no bar to fetch) from a weekday whose
            # bar we simply have not collected yet.
            checked_through = self._read_checked_through(symbol, security_id)
            if _cache_covers_range(
                first_date,
                last_date,
                requested_start,
                requested_end,
                vendor_earliest,
                checked_through,
            ):
                if cached is None:
                    # The footer is only an advisory index. The file can be
                    # replaced after the metadata read, and a valid footer
                    # does not prove every data page is readable.
                    cached = pd.read_parquet(path)
                actual_first, actual_last = _date_bounds(cached)
                if _cache_covers_range(
                    actual_first,
                    actual_last,
                    requested_start,
                    requested_end,
                    vendor_earliest,
                    checked_through,
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
        self._record_vendor_earliest(
            symbol, security_id, requested_from=start_date, candles=candles
        )
        return self._slice_to_range(candles, start_date, end_date), False

    def fetch_window(
        self,
        instrument: Mapping[str, object] | pd.Series,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
    ) -> pd.DataFrame:
        """Fetch one date window from Dhan **without touching the cache file**.

        Every other fetch path here writes what it downloads straight to the
        symbol's parquet. That is exactly wrong for the DATA-002 repair, which
        needs to pull a bounded window (say, the day around a corrupt bar) and
        merge it *over* ten years of otherwise-good history — writing the window
        directly would truncate the file to those few days.

        So this method deliberately does the network half only: the same
        rate-limit pacing, DH-904 backoff, and optional timeout as every other
        fetch, but the caller owns what happens to the result. An empty frame
        means the vendor genuinely has no rows for that window (a real holiday
        stretch, or history that predates the listing) — not an error.
        """
        row = dict(instrument)
        symbol = str(row.get("symbol", "")).strip().upper()
        security_id = str(row.get("security_id", "")).strip()
        if not symbol:
            raise ValueError("Instrument row is missing symbol")
        if not security_id:
            raise ValueError(f"{symbol} is missing security_id")

        return self._fetch_with_rate_limit_retries(
            security_id=security_id,
            exchange_segment=str(row.get("exchange_segment", "NSE_EQ") or "NSE_EQ").strip(),
            instrument_type=str(row.get("instrument_type", "EQUITY") or "EQUITY").strip(),
            from_date=start_date,
            to_date=end_date,
        )

    def ensure_daily_history(
        self,
        instrument: Mapping[str, object] | pd.Series,
        years_back: int = DEFAULT_HISTORY_YEARS_BACK,
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
        start = history_start_date(int(years_back), today)

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
            self._record_vendor_earliest(
                symbol, security_id, requested_from=start, candles=candles
            )
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
            self._record_vendor_earliest(
                symbol, security_id, requested_from=start, candles=candles
            )
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
            self._record_vendor_earliest(
                symbol, security_id, requested_from=start, candles=candles
            )
            return candles, "fresh_download"

        # A cache that starts late is either an interrupted prefetch (the vendor
        # HAS the missing years, so refetch) or a stock that listed after the
        # window opened (the vendor has nothing earlier, so refetching is waste
        # forever). ``_vendor_earliest_for`` is what tells the two apart; without
        # evidence it returns None and the original always-refetch rule applies.
        vendor_earliest = self._vendor_earliest_for(symbol, security_id, start)
        if not _cache_reaches_back_far_enough(first_date, start, vendor_earliest):
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
            self._record_vendor_earliest(
                symbol, security_id, requested_from=start, candles=candles
            )
            if not candles.empty:
                candles.to_parquet(path, index=False)
                return candles, "backfilled"
            return cached, "fresh"
        # Falling through on purpose: a later listing still needs its daily
        # top-up. Suppressing the backfill must not freeze the symbol's tail.

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
        if self.fetch_workers <= 1:
            # Sequential behavior, unchanged: a fixed pause before every fetch.
            self._sleep(self.request_delay_seconds)

        retry_index = 0
        while True:
            if self.fetch_workers > 1:
                # Every network attempt, including a DH-904 retry, reserves a
                # slot on the one shared schedule. Otherwise workers that wake
                # from the same backoff can retry in a burst.
                self._pacer.wait()
            try:
                with self._stats_lock:
                    self._api_attempts += 1
                    self.last_api_attempts = self._api_attempts
                return self._call_client_fetch(
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
                with self._stats_lock:
                    self._rate_limit_retries += 1
                    self.last_rate_limit_retries = self._rate_limit_retries
                self._sleep(delay)

    def _call_client_fetch(
        self,
        *,
        security_id: str,
        exchange_segment: str,
        instrument_type: str,
        from_date: date | datetime | str,
        to_date: date | datetime | str,
    ) -> pd.DataFrame:
        """Call Dhan with an optional wall-clock timeout.

        The Dhan SDK does not expose a documented timeout knob here, so the app
        wraps the blocking call in a tiny worker thread when a timeout is set.
        Python cannot forcibly kill a running SDK call, but `shutdown(wait=False)`
        lets the scanner move on instead of blocking the Streamlit run forever.
        """
        client = self.client
        if client is None:
            # The documented "fail loudly" contract for cache-only loaders
            # (see __init__): a clear error instead of an AttributeError.
            raise RuntimeError(
                "This DailyDataLoader was built without a Dhan client "
                "(cache-only mode); it cannot fetch new candles."
            )
        if self.fetch_timeout_seconds is None:
            return client.fetch_daily_candles(
                security_id=security_id,
                exchange_segment=exchange_segment,
                instrument_type=instrument_type,
                from_date=from_date,
                to_date=to_date,
            )

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        future = executor.submit(
            client.fetch_daily_candles,
            security_id=security_id,
            exchange_segment=exchange_segment,
            instrument_type=instrument_type,
            from_date=from_date,
            to_date=to_date,
        )
        try:
            return future.result(timeout=self.fetch_timeout_seconds)
        except concurrent.futures.TimeoutError as exc:
            future.cancel()
            raise TimeoutError(
                f"Dhan daily fetch timed out after {self.fetch_timeout_seconds:.2f}s"
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _work_rows(self, universe_df: pd.DataFrame, max_symbols: int | None) -> list[dict]:
        """Return mapped universe rows that this loader can actually fetch."""
        if universe_df.empty:
            return []
        work = universe_df.copy()
        if "mapping_status" in work.columns:
            # Only mapped rows have a Dhan security_id. Missing mappings are
            # kept in the CSV for visibility, but cannot be fetched.
            work = work.loc[work["mapping_status"].astype(str).str.lower().eq("mapped")].copy()
        if max_symbols is not None and int(max_symbols) > 0:
            # Tests and CLI callers may still cap the batch; the UI does not.
            work = work.head(int(max_symbols)).copy()
        return work.to_dict("records")

    def iter_universe_history(
        self,
        universe_df: pd.DataFrame,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        max_symbols: int | None = None,
        force_refresh: bool = False,
        progress_callback: ProgressCallback | None = None,
    ):
        """Yield one symbol's candle frame at a time.

        This is the scalable path for screeners: a strategy can compute as soon
        as the first symbol is loaded instead of waiting for the entire universe
        to be stored in memory. The batch API below is preserved for older
        callers and simply consumes this iterator into a dictionary.
        """
        self._api_attempts = 0
        self._rate_limit_retries = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0
        self.last_data_quality_reports = []

        result = BatchLoadResult()
        rows = self._work_rows(universe_df, max_symbols)
        total = len(rows)

        if self.fetch_workers <= 1:
            stream = self._iter_history_sequential(
                rows, total, result, start_date, end_date, force_refresh, progress_callback
            )
        else:
            stream = self._iter_history_parallel(
                rows, total, result, start_date, end_date, force_refresh, progress_callback
            )
        yield from stream

        result.api_attempts = self._api_attempts
        result.rate_limit_retries = self._rate_limit_retries
        self._remember(result)

    def _breaker_item(
        self, result: BatchLoadResult, row: dict, symbol: str, consecutive_failures: int
    ) -> HistoryLoadItem:
        """Record and build the failure item for a circuit-breaker skip."""
        failure = {
            "symbol": symbol,
            "security_id": row.get("security_id", ""),
            "message": (
                "Dhan circuit breaker is open after "
                f"{consecutive_failures} consecutive failure(s)."
            ),
        }
        result.failures.append(failure)
        return HistoryLoadItem(symbol=symbol, failure=failure)

    def _failure_item(
        self, result: BatchLoadResult, row: dict, symbol: str, exc: Exception
    ) -> HistoryLoadItem:
        """Record, log, and build the failure item for one fetch exception.

        Dhan/HTTP exceptions can include request details. The message is stored
        in ``last_failures`` and later rendered by Streamlit, so redact at the
        source.
        """
        safe_message = redact_text(str(exc))
        # OBS-001: external_api_failed ties a failed Dhan fetch to its symbol.
        log_event(
            logger,
            EVENT_EXTERNAL_API_FAILED,
            level=logging.WARNING,
            symbol=symbol,
            security_id=row.get("security_id", ""),
            error=safe_message,
        )
        failure = {
            "symbol": symbol,
            "security_id": row.get("security_id", ""),
            "message": safe_message,
        }
        result.failures.append(failure)
        return HistoryLoadItem(symbol=symbol, failure=failure)

    def _quality_checked_item(
        self,
        result: BatchLoadResult,
        row: dict,
        item: HistoryLoadItem,
        expected_latest_date: date | datetime | str,
    ) -> HistoryLoadItem:
        """Run DATA-001 candle-quality validation on one freshly-loaded frame.

        This is the single choke point (DATA-001B) where bad candle data is
        caught before any screener sees it. The contract:

        - A frame with a **fatal** finding is *quarantined*: we turn the
          successful item into a ``phase="data_quality"`` failure, so the symbol
          is dropped from the scan exactly like a fetch error would be.
        - A **warning-only** frame passes through unchanged (the screener still
          scans it); we just record and log the warning.
        - Either way the full ``CandleQualityReport`` is stashed on ``result`` so
          the scan service can build its per-run receipt afterwards.
        """
        # An item that already failed to load (fetch error, circuit breaker) has
        # no candles to check — leave it as-is.
        if item.failure is not None:
            return item

        expected_date = _coerce_date(expected_latest_date)
        report = validate_candles(
            item.candles,
            symbol=item.symbol,
            expected_latest_date=expected_date,
        )
        # Record every report (even clean ones) so the receipt can count how many
        # symbols were checked, not just how many had problems.
        result.data_quality_reports.append(report)
        if not report.findings:
            return item

        # Build the structured log fields once; both the fatal and warning paths
        # below reuse them. Only codes/counts are logged — never raw prices.
        finding_codes = [finding.code for finding in report.findings]
        warning_count = sum(
            1 for finding in report.findings if finding.severity == "warning"
        )
        fatal_count = sum(
            1 for finding in report.findings if finding.severity == "fatal"
        )
        event_fields = {
            "symbol": item.symbol,
            "security_id": row.get("security_id", ""),
            "finding_codes": finding_codes,
            "warning_findings": warning_count,
            "fatal_findings": fatal_count,
            "latest_date": report.latest_date.isoformat() if report.latest_date else None,
            "row_count": report.row_count,
        }
        if report.has_fatal_findings:
            # Quarantine path: log it, record a loader failure (the codes go in the
            # message, never the prices), and return a *failure* item so the
            # streaming/batch consumers skip this symbol entirely.
            log_event(
                logger,
                EVENT_CANDLE_DATA_QUALITY_FAILED,
                level=logging.WARNING,
                **event_fields,
            )
            failure = {
                "symbol": item.symbol,
                "security_id": row.get("security_id", ""),
                "phase": "data_quality",
                "message": (
                    "Candle data failed quality checks "
                    f"({', '.join(finding_codes)})."
                ),
                "quality_codes": finding_codes,
            }
            result.failures.append(failure)
            return HistoryLoadItem(symbol=item.symbol, failure=failure)

        # Warning-only path: record/log it but hand the original (usable) item back.

        log_event(
            logger,
            EVENT_CANDLE_DATA_QUALITY_WARNING,
            level=logging.WARNING,
            **event_fields,
        )
        return item

    @staticmethod
    def _notify_progress(
        progress_callback: ProgressCallback | None, index: int, total: int, symbol: str
    ) -> None:
        """Invoke the UI progress callback without letting it break the scan."""
        if progress_callback is None:
            return
        try:
            progress_callback(index, total, symbol)
        except Exception:
            logger.exception("Progress callback raised for %s", symbol)

    def _iter_history_sequential(
        self,
        rows: list[dict],
        total: int,
        result: BatchLoadResult,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        force_refresh: bool,
        progress_callback: ProgressCallback | None,
    ):
        """The long-standing one-symbol-at-a-time path (fetch_workers == 1)."""
        consecutive_failures = 0
        for index, row in enumerate(rows, start=1):
            symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
            if self.max_consecutive_failures and consecutive_failures >= self.max_consecutive_failures:
                # The circuit breaker protects the user and Dhan after repeated
                # broker/API failures. We still yield a failure item per skipped
                # symbol so progress and diagnostics stay complete.
                item = self._breaker_item(result, row, symbol, consecutive_failures)
            else:
                try:
                    candles, from_cache = self.get_daily_history(
                        row,
                        start_date=start_date,
                        end_date=end_date,
                        force_refresh=force_refresh,
                    )
                    consecutive_failures = 0
                    if from_cache:
                        result.cache_hits += 1
                    else:
                        result.cache_misses += 1
                    item = HistoryLoadItem(symbol=symbol, candles=candles, from_cache=from_cache)
                except Exception as exc:
                    consecutive_failures += 1
                    item = self._failure_item(result, row, symbol, exc)

            self._notify_progress(progress_callback, index, total, symbol)
            item = self._quality_checked_item(result, row, item, end_date)
            yield item

    def _iter_history_parallel(
        self,
        rows: list[dict],
        total: int,
        result: BatchLoadResult,
        start_date: date | datetime | str,
        end_date: date | datetime | str,
        force_refresh: bool,
        progress_callback: ProgressCallback | None,
    ):
        """Fetch with a worker pool while keeping the sequential contract.

        Workers run ONLY ``get_daily_history`` (network + parquet I/O). Items
        are consumed in submission order on the calling thread, so yields,
        failure bookkeeping, log events, and the progress callback all happen
        exactly where the sequential path runs them - Streamlit widgets in the
        callback stay on the script thread.

        Circuit-breaker semantics under parallelism: once the consecutive
        failure threshold trips, no NEW work is submitted and queued-but-not-
        started futures are cancelled, but fetches already in flight are
        consumed normally (the request already happened; discarding the data
        would help nobody). Rows never submitted yield breaker items, exactly
        like the sequential path.
        """
        window = self.fetch_workers * 2
        pending: deque[tuple[int, dict, str, concurrent.futures.Future]] = deque()
        row_iter = enumerate(rows, start=1)
        consecutive_failures = 0
        breaker_tripped = False
        breaker_failure_count = 0

        with concurrent.futures.ThreadPoolExecutor(max_workers=self.fetch_workers) as executor:

            def submit_next() -> bool:
                try:
                    index, row = next(row_iter)
                except StopIteration:
                    return False
                symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
                future = executor.submit(
                    self.get_daily_history,
                    row,
                    start_date=start_date,
                    end_date=end_date,
                    force_refresh=force_refresh,
                )
                pending.append((index, row, symbol, future))
                return True

            while len(pending) < window and submit_next():
                pass

            while pending:
                index, row, symbol, future = pending.popleft()
                if breaker_tripped and future.cancel():
                    item = self._breaker_item(result, row, symbol, breaker_failure_count)
                else:
                    try:
                        candles, from_cache = future.result()
                        consecutive_failures = 0
                        if from_cache:
                            result.cache_hits += 1
                        else:
                            result.cache_misses += 1
                        item = HistoryLoadItem(
                            symbol=symbol, candles=candles, from_cache=from_cache
                        )
                    except Exception as exc:
                        consecutive_failures += 1
                        item = self._failure_item(result, row, symbol, exc)
                        if (
                            not breaker_tripped
                            and self.max_consecutive_failures
                            and consecutive_failures >= self.max_consecutive_failures
                        ):
                            breaker_tripped = True
                            breaker_failure_count = consecutive_failures

                if not breaker_tripped:
                    submit_next()
                self._notify_progress(progress_callback, index, total, symbol)
                item = self._quality_checked_item(result, row, item, end_date)
                yield item

            if breaker_tripped:
                # Rows that were never submitted: same breaker items, same
                # progress reporting, no API traffic.
                for index, row in row_iter:
                    symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
                    item = self._breaker_item(result, row, symbol, breaker_failure_count)
                    self._notify_progress(progress_callback, index, total, symbol)
                    yield item

    def iter_ensure_universe_history(
        self,
        rows: list[dict],
        *,
        years_back: int = DEFAULT_HISTORY_YEARS_BACK,
        today: date | None = None,
    ):
        """Yield one ``PrefetchOutcome`` per universe row, parallel when configured.

        This is the streaming engine behind the CLI prefetch. With
        ``fetch_workers == 1`` it tops up one symbol at a time exactly like the
        old app-level loop; with more workers it overlaps Dhan latency and
        parquet I/O while the shared pacer keeps the global request rate at the
        configured delay. Outcomes are always yielded in input order so the
        terminal progress output stays stable and readable.
        """
        if self.fetch_workers <= 1:
            for row in rows:
                yield self._ensure_one_row(row, years_back, today)
            return

        window = self.fetch_workers * 2
        pending: deque[concurrent.futures.Future] = deque()
        row_iter = iter(rows)
        with concurrent.futures.ThreadPoolExecutor(max_workers=self.fetch_workers) as executor:

            def submit_next() -> bool:
                try:
                    row = next(row_iter)
                except StopIteration:
                    return False
                pending.append(executor.submit(self._ensure_one_row, row, years_back, today))
                return True

            while len(pending) < window and submit_next():
                pass
            while pending:
                outcome = pending.popleft().result()
                submit_next()
                yield outcome

    def _ensure_one_row(
        self, row: dict, years_back: int, today: date | None
    ) -> PrefetchOutcome:
        """Run ``ensure_daily_history`` for one row, capturing a safe outcome."""
        symbol = str(row.get("symbol", "?")).strip() or "?"
        try:
            # A prefetch freshness verdict must come from the frame itself.
            # Footer statistics can survive damaged data pages, so using them
            # here would let an unreadable cache masquerade as healthy.
            _, status = self.ensure_daily_history(row, years_back=years_back, today=today)
            return PrefetchOutcome(symbol=symbol, status=status)
        except Exception as exc:
            logger.exception("Prefetch failed for %s", symbol)
            return PrefetchOutcome(symbol=symbol, status="failed", message=redact_text(str(exc)))

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
        result = BatchLoadResult()
        for item in self.iter_universe_history(
            universe_df,
            start_date,
            end_date,
            max_symbols=max_symbols,
            force_refresh=force_refresh,
            progress_callback=progress_callback,
        ):
            if item.failure is not None:
                result.failures.append(item.failure)
                continue
            if item.from_cache:
                result.cache_hits += 1
            else:
                result.cache_misses += 1
            result.frames[item.symbol] = item.candles

        result.api_attempts = self._api_attempts
        result.rate_limit_retries = self._rate_limit_retries
        # The quality reports were accumulated on the *streaming* iterator's own
        # result; draining the loop above also ran ``iter_universe_history``'s
        # post-yield ``_remember``, which copied them onto ``last_data_quality_reports``.
        # Copy them back onto this batch result so callers of either entry point
        # (streaming or batch) see the same reports.
        result.data_quality_reports = list(self.last_data_quality_reports)
        self._remember(result)
        return result

    def _remember(self, result: BatchLoadResult) -> None:
        """Expose the latest batch summary to the Streamlit UI."""
        self.last_failures = result.failures
        self.last_data_quality_reports = result.data_quality_reports
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
            # Keep the length check first: Python stops evaluating an `and`
            # expression as soon as one condition is false, so short filenames
            # never reach the `parts[-2]` and `parts[-1]` lookups.
            has_date_pair = (
                len(parts) >= 4
                and parts[-2].isdigit()
                and parts[-1].isdigit()
                and len(parts[-2]) == 8
                and len(parts[-1]) == 8
            )
            if has_date_pair:
                try:
                    path.unlink()
                    deleted += 1
                except OSError:
                    logger.exception("Could not delete legacy cache file %s", path)
        return deleted

    def cleanup_stale_cache_files(
        self,
        *,
        max_age_days: int,
        now: datetime | None = None,
    ) -> int:
        """Remove old parquet cache files and orphan sidecar markers.

        Daily cache files can grow stale when symbols leave a universe or a
        security ID changes. This helper is intentionally explicit: callers
        choose the age threshold, and only daily parquet files plus their
        sidecar markers are touched.

        Three sidecar kinds travel with a parquet: `.checked` (the empty-increment
        marker written here), `.repaired` (the DATA-002 repair cooldown), and
        `.firstbar` (the DATA-004 record of how far back the vendor's history
        goes). All are meaningless without their parquet, so an orphan of any of
        them is removed regardless of age.
        """
        if not self.cache_dir.exists():
            return 0
        now = now or datetime.now()
        cutoff = now - timedelta(days=max(1, int(max_age_days)))
        targets: set[Path] = set()
        sidecar_suffixes = (".checked", ".repaired", ".firstbar")

        for parquet in self.cache_dir.glob("*.parquet"):
            modified = datetime.fromtimestamp(parquet.stat().st_mtime)
            if modified < cutoff:
                targets.add(parquet)
                for suffix in sidecar_suffixes:
                    sidecar = parquet.with_suffix(suffix)
                    if sidecar.exists():
                        targets.add(sidecar)

        for suffix in sidecar_suffixes:
            for sidecar in self.cache_dir.glob(f"*{suffix}"):
                # A marker without a parquet owner can never be used again.
                if not sidecar.with_suffix(".parquet").exists():
                    targets.add(sidecar)

        deleted = 0
        for path in sorted(targets):
            try:
                path.unlink()
                deleted += 1
            except OSError:
                logger.exception("Could not delete stale cache file %s", path)
        return deleted
