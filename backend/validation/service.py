"""VALID-002 service for filling stored signal forward returns."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import pandas as pd
from sqlalchemy.orm import Session

from backend.data_quality import validate_candles
from backend.storage.database import SessionFactory
from backend.storage.models import ForwardReturnStatus
from backend.storage.repository import (
    BenchmarkForwardReturnWork,
    ForwardReturnWorkItem,
    get_forward_return_work_items,
    mark_forward_return_attempted,
    update_forward_return_benchmark,
    upsert_forward_return,
)
from backend.universe_loader import load_universe, mapped_only
from backend.validation.benchmarks import (
    BenchmarkLeg,
    BenchmarkSpec,
    benchmark_for_universe,
    compute_benchmark_leg,
)
from backend.validation.forward_return import (
    FORWARD_RETURN_HORIZONS,
    MISSING_FUTURE_DATA_GRACE_DAYS,
    ForwardReturnPoint,
    compute_forward_return,
    positive_integral,
)

logger = logging.getLogger(__name__)

# A configured benchmark that still cannot produce a leg this long after the
# stock leg became terminal is finalized as missing. It mirrors the stock
# leg's own grace period so neither leg can stay queued forever.
BENCHMARK_RETRY_GRACE_DAYS = MISSING_FUTURE_DATA_GRACE_DAYS


class DailyHistoryLoader(Protocol):
    """Small protocol matching the existing DailyDataLoader method we need."""

    def get_daily_history(
        self,
        instrument: Mapping[str, object] | pd.Series,
        start_date: dt.date,
        end_date: dt.date,
        force_refresh: bool = False,
        *,
        preserve_malformed_rows: bool = False,
    ) -> tuple[pd.DataFrame, bool]: ...


UniverseLoader = Callable[[str], pd.DataFrame]
BenchmarkResolver = Callable[[str], BenchmarkSpec | None]


@dataclass
class ForwardReturnRunSummary:
    """Counts from one service pass, useful for jobs and tests."""

    total_signals: int = 0
    computed: int = 0
    pending: int = 0
    insufficient: int = 0
    benchmark_computed: int = 0
    benchmark_missing: int = 0


class ForwardReturnBatchError(RuntimeError):
    """A fatal batch failure carrying only already committed progress.

    Beginner note:
        Earlier signals commit independently. Callers must preserve this summary
        instead of reporting zero work when a later signal rolls back.
    """

    def __init__(self, summary: ForwardReturnRunSummary) -> None:
        super().__init__("Forward-return batch failed after partial progress")
        self.summary = summary


def compute_pending_forward_returns(
    session_factory: SessionFactory,
    loader: DailyHistoryLoader,
    *,
    as_of: dt.date | None = None,
    horizons: Sequence[int] = FORWARD_RETURN_HORIZONS,
    limit: int | None = None,
    universe_loader: UniverseLoader = load_universe,
    benchmark_resolver: BenchmarkResolver = benchmark_for_universe,
) -> ForwardReturnRunSummary:
    """Measure one fair batch using short, independently committed transactions.

    Args:
        session_factory: Context factory committing on success and rolling back
            on failure; the worker owns when each context opens and closes.
        loader: Historical loader; requests end at ``as_of`` without future slack.
        as_of: Last observable calendar date, defaulting to today.
        horizons: Positive integral trading-bar counts, deduplicated in order.
        limit: Maximum distinct signals selected once; None means unbounded.
        universe_loader: Local instrument mapping resolver.
        benchmark_resolver: Optional configured index for each universe.

    Returns:
        Counts for committed signals and their unresolved measurements only.

    Raises:
        ValueError: Invalid horizon or limit, before any external work.
        ForwardReturnBatchError: At least one signal failed, raised after every
            other selected signal was processed. It carries the summary of the
            committed signals and the first failure as its cause.

    Beginner note:
        Detached scalar work leaves the read transaction before any universe or
        provider access. Each signal is fully calculated first, then persisted
        atomically in one short context. A later failure cannot erase earlier
        commits, and retrying a pending horizon cannot rewrite a terminal one.
        A failing signal gets its attempt recorded in its own transaction and
        the batch moves on; otherwise oldest-first selection would hand that
        same signal to every future batch and stall the whole queue.
    """
    normalized_horizons = tuple(dict.fromkeys(positive_integral(h, name="horizon") for h in horizons))
    if limit is not None:
        limit = positive_integral(limit, name="limit")
    summary = ForwardReturnRunSummary()
    if not normalized_horizons:
        return summary
    as_of_date = as_of or dt.date.today()
    with session_factory() as session:
        signals = get_forward_return_work_items(session, horizons=normalized_horizons, limit=limit)
    universe_cache: dict[str, pd.DataFrame | None] = {}
    benchmark_cache: dict[tuple[str, dt.date, dt.date], pd.DataFrame | None] = {}
    first_failure: Exception | None = None

    for signal in signals:
        try:
            points = _stock_points(signal, loader, as_of_date, universe_loader, universe_cache)
            stock_results = [
                (point, *_benchmark_for_point(
                    point, signal.universe_key, signal.signal_date, as_of_date,
                    loader, benchmark_resolver, benchmark_cache,
                ))
                for point in points
            ]
            benchmark_results = []
            for work in signal.benchmark_horizons:
                point = ForwardReturnPoint(
                    horizon_days=work.horizon_days, status=ForwardReturnStatus.COMPUTED,
                    entry_date=work.entry_date, exit_date=work.exit_date,
                    forward_return_pct=work.forward_return_pct,
                )
                benchmark, retry = _benchmark_for_point(
                    point, signal.universe_key, signal.signal_date, as_of_date,
                    loader, benchmark_resolver, benchmark_cache,
                )
                if retry and _benchmark_retry_expired(work, signal.signal_date, as_of_date):
                    retry = False
                benchmark_results.append((work.horizon_days, benchmark, retry))
            committed = ForwardReturnRunSummary(total_signals=1)
            with session_factory() as session:
                for point, benchmark, retry in stock_results:
                    _store_point(session, committed, signal.result_id, point,
                                 benchmark=benchmark, benchmark_retry_pending=retry)
                for horizon, benchmark, retry in benchmark_results:
                    changed = update_forward_return_benchmark(
                        session, result_id=signal.result_id, horizon_days=horizon,
                        benchmark=benchmark, retry_pending=retry,
                    )
                    if changed:
                        _count_benchmark(committed, benchmark)
            # Only count after context exit: commit itself can fail.
            for field in summary.__dataclass_fields__:
                setattr(summary, field, getattr(summary, field) + getattr(committed, field))
        except Exception as exc:
            first_failure = first_failure or exc
            logger.warning(
                "Forward-return signal %s failed (%s); recording the attempt and continuing",
                signal.result_id, type(exc).__name__,
            )
            _record_failed_attempt(session_factory, signal, summary)
    if first_failure is not None:
        raise ForwardReturnBatchError(summary) from first_failure
    return summary


def _record_failed_attempt(
    session_factory: SessionFactory,
    signal: ForwardReturnWorkItem,
    summary: ForwardReturnRunSummary,
) -> None:
    """Commit an attempt receipt for a failed signal, or stop if the DB cannot.

    Beginner note:
        If even this tiny write fails, the database itself is unusable and the
        remaining signals would fail the same way, so the batch stops here with
        the progress already committed.
    """
    horizons = (*signal.stock_horizons, *(work.horizon_days for work in signal.benchmark_horizons))
    try:
        with session_factory() as session:
            mark_forward_return_attempted(session, result_id=signal.result_id, horizons=horizons)
    except Exception as exc:
        raise ForwardReturnBatchError(summary) from exc


def _benchmark_retry_expired(work: BenchmarkForwardReturnWork, signal_date: dt.date, as_of: dt.date) -> bool:
    """Return whether a benchmark-only retry has outlived its grace period.

    Beginner note:
        The clock starts when the stock leg became terminal (``computed_at``),
        falling back to the signal date for legacy rows without it. Measuring
        against ``as_of`` keeps as-of replays conservative: a replay dated
        before ``computed_at`` never expires anything.
    """
    reference = work.computed_at.date() if work.computed_at is not None else signal_date
    return (as_of - reference).days > BENCHMARK_RETRY_GRACE_DAYS


def _stock_points(
    signal: ForwardReturnWorkItem,
    loader: DailyHistoryLoader,
    as_of: dt.date,
    universe_loader: UniverseLoader,
    universe_cache: dict[str, pd.DataFrame | None],
) -> list[ForwardReturnPoint]:
    """Prepare unresolved stock points without holding a database transaction.

    Beginner note:
        Bad raw provider rows may be repaired later, so they remain PENDING.
        A missing symbol in an otherwise usable universe remains terminal.
        Benchmark-only work bypasses universe and stock fetching entirely.
    """
    if not signal.stock_horizons:
        return []
    status = ForwardReturnStatus.PENDING
    if signal.signal_date <= as_of:
        universe = _universe_for_signal(signal, universe_loader, universe_cache)
        if universe is not None:
            instrument = _match_instrument(universe, signal.symbol)
            if instrument is None:
                status = ForwardReturnStatus.INSUFFICIENT_DATA
            else:
                candles = _load_history(loader, instrument, signal.signal_date, as_of)
                if candles is not None:
                    # _load_history already validated this raw frame once.
                    return [compute_forward_return(candles, signal.signal_date, horizon, as_of=as_of,
                                                   raw_validated=True)
                            for horizon in signal.stock_horizons]
    return [ForwardReturnPoint(horizon_days=h, status=status) for h in signal.stock_horizons]


def _universe_for_signal(
    signal: ForwardReturnWorkItem,
    universe_loader: UniverseLoader,
    universe_cache: dict[str, pd.DataFrame | None],
) -> pd.DataFrame | None:
    """Return the mapped-only universe for this signal's run, or None if it can't load.

    None means the universe CSV is missing/corrupt or the key is unknown — an
    environment problem the caller treats as *retryable* (PENDING), not a verdict
    about the signal. Cached per ``universe_key`` so a run's signals share one load.
    """
    universe_key = signal.universe_key
    if universe_key not in universe_cache:
        try:
            universe_cache[universe_key] = mapped_only(universe_loader(universe_key))
        except (KeyError, FileNotFoundError, ValueError):
            universe_cache[universe_key] = None

    universe = universe_cache[universe_key]
    if universe is None or universe.empty:
        return None
    return universe


def _match_instrument(universe: pd.DataFrame, symbol: str) -> dict[str, object] | None:
    """Return the loader-ready instrument row for ``symbol``, or None if absent.

    None here is *terminal* for the signal (INSUFFICIENT_DATA): the universe loaded
    fine, it simply has no row for this symbol (delisted, renamed, or never mapped).
    """
    wanted = symbol.upper().strip()
    matches = universe.loc[universe["symbol"].astype(str).str.upper().str.strip().eq(wanted)]
    if matches.empty:
        return None
    return dict(matches.iloc[0])


def _load_history(
    loader: DailyHistoryLoader,
    instrument: Mapping[str, object],
    start_date: dt.date,
    end_date: dt.date,
) -> pd.DataFrame | None:
    """Load strict historical data and reject malformed raw OHLC as retryable.

    Beginner note:
        Do not opt in to an unpublished cache tail: validation is historical.
        The provider boundary must validate before a calculator can drop rows;
        otherwise a missing entry open could shift the entry to the next day.
        This is the one caller that opts in to raw malformed rows; scans,
        charts and ranking get them stripped by the loader's default view.
    """
    try:
        candles, _from_cache = loader.get_daily_history(
            instrument, start_date, end_date, preserve_malformed_rows=True,
        )
    except Exception:
        # Treat loader failures as retryable. Marking them insufficient would turn
        # a transient broker/cache issue into a permanent validation result.
        return None
    if validate_candles(
        candles, symbol=str(instrument.get("symbol", "VALIDATION")),
        required_columns=("open", "high", "low", "close"),
        allow_identical_daily_duplicates=True,
    ).has_fatal_findings:
        return None
    return candles


def _benchmark_for_point(
    point: ForwardReturnPoint,
    universe_key: str,
    signal_date: dt.date,
    end_date: dt.date,
    loader: DailyHistoryLoader,
    benchmark_resolver: BenchmarkResolver,
    benchmark_cache: dict[tuple[str, dt.date, dt.date], pd.DataFrame | None],
) -> tuple[BenchmarkLeg | None, bool]:
    """Return an aligned index leg and whether configured work still needs retry.

    Beginner note:
        No configured benchmark is an intentional absence. A configured index
        with missing or malformed candles is temporary and must stay queued,
        even after every stock horizon becomes terminal.
    """
    if point.status is not ForwardReturnStatus.COMPUTED:
        return None, False

    spec = benchmark_resolver(universe_key)
    if spec is None:
        return None, False
    # Missing legacy dates or an as-of replay before the stock exit cannot
    # establish an index measurement. Keep configured work retryable and avoid
    # fetching a range that cannot contain the required dates.
    if point.entry_date is None or point.exit_date is None or point.exit_date > end_date:
        return BenchmarkLeg(spec.key, None, None, None), True

    cache_key = (spec.key, signal_date, end_date)
    if cache_key not in benchmark_cache:
        benchmark_cache[cache_key] = _load_history(
            loader,
            spec.instrument,
            signal_date,
            end_date,
        )
    benchmark_candles = benchmark_cache[cache_key]
    if benchmark_candles is None:
        return BenchmarkLeg(spec.key, None, None, None), True

    leg = compute_benchmark_leg(
        benchmark_candles,
        entry_date=point.entry_date,
        exit_date=point.exit_date,
        benchmark_key=spec.key,
        raw_validated=True,
    )

    return leg, leg.return_pct is None


def _store_point(
    session: Session,
    summary: ForwardReturnRunSummary,
    result_id: int,
    point: ForwardReturnPoint,
    *,
    benchmark: BenchmarkLeg | None,
    benchmark_retry_pending: bool,
) -> None:
    """Persist one pending stock leg and tally its actual durable status.

    Beginner note:
        Conditional repository writes may preserve a concurrent winner. Count
        the returned receipt, never an attempted overwrite that did not happen.
    """
    row = upsert_forward_return(
        session, result_id=result_id, point=point, benchmark=benchmark,
        benchmark_retry_pending=benchmark_retry_pending,
    )
    point = ForwardReturnPoint(horizon_days=row.horizon_days, status=row.status)
    if point.status is ForwardReturnStatus.COMPUTED:
        summary.computed += 1
        if row.benchmark_return_pct is not None:
            summary.benchmark_computed += 1
        else:
            summary.benchmark_missing += 1
    elif point.status is ForwardReturnStatus.PENDING:
        summary.pending += 1
    else:
        summary.insufficient += 1



def _count_benchmark(summary: ForwardReturnRunSummary, benchmark: BenchmarkLeg | None) -> None:
    """Count attempted benchmark work after its containing transaction commits."""
    if benchmark is not None and benchmark.return_pct is not None:
        summary.benchmark_computed += 1
    else:
        summary.benchmark_missing += 1
