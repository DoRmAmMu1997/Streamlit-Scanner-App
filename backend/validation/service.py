"""VALID-002 service for filling stored signal forward returns."""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

import pandas as pd
from sqlalchemy.orm import Session

from backend.storage.models import ForwardReturnStatus, ScanResult
from backend.storage.repository import (
    get_signals_needing_forward_returns,
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
    ForwardReturnPoint,
    compute_forward_return,
)


class DailyHistoryLoader(Protocol):
    """Small protocol matching the existing DailyDataLoader method we need."""

    def get_daily_history(
        self,
        instrument: Mapping[str, object] | pd.Series,
        start_date: dt.date,
        end_date: dt.date,
        force_refresh: bool = False,
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


def compute_pending_forward_returns(
    session: Session,
    loader: DailyHistoryLoader,
    *,
    as_of: dt.date | None = None,
    horizons: Sequence[int] = FORWARD_RETURN_HORIZONS,
    limit: int | None = None,
    universe_loader: UniverseLoader = load_universe,
    benchmark_resolver: BenchmarkResolver = benchmark_for_universe,
) -> ForwardReturnRunSummary:
    """Compute missing or pending forward-return rows for stored signals.

    VALID-002 stops at this callable service. A later scheduler can decide when
    to call it; this function only owns the idempotent read-compute-upsert pass.
    """
    normalized_horizons = tuple(int(horizon) for horizon in horizons)
    as_of_date = as_of or dt.date.today()
    signals = get_signals_needing_forward_returns(
        session,
        horizons=normalized_horizons,
        limit=limit,
    )
    summary = ForwardReturnRunSummary(total_signals=len(signals))
    universe_cache: dict[str, pd.DataFrame | None] = {}
    benchmark_cache: dict[tuple[str, dt.date, dt.date], pd.DataFrame | None] = {}

    for signal in signals:
        if signal.signal_date is None:
            continue

        instrument = _resolve_instrument(signal, universe_loader, universe_cache)
        if instrument is None:
            for horizon in normalized_horizons:
                _store_point(
                    session,
                    summary,
                    signal.id,
                    ForwardReturnPoint(
                        horizon_days=horizon,
                        status=ForwardReturnStatus.INSUFFICIENT_DATA,
                    ),
                    benchmark=None,
                )
            continue

        end_date = _history_end_date(signal.signal_date, as_of_date, normalized_horizons)
        candles = _load_history(loader, instrument, signal.signal_date, end_date)
        if candles is None:
            for horizon in normalized_horizons:
                _store_point(
                    session,
                    summary,
                    signal.id,
                    ForwardReturnPoint(horizon_days=horizon, status=ForwardReturnStatus.PENDING),
                    benchmark=None,
                )
            continue

        for horizon in normalized_horizons:
            point = compute_forward_return(
                candles,
                signal.signal_date,
                horizon,
                as_of=as_of_date,
            )
            benchmark = _benchmark_for_point(
                point,
                signal.run.universe_key,
                signal.signal_date,
                end_date,
                loader,
                benchmark_resolver,
                benchmark_cache,
            )
            _store_point(session, summary, signal.id, point, benchmark=benchmark)

    session.flush()
    return summary


def _resolve_instrument(
    signal: ScanResult,
    universe_loader: UniverseLoader,
    universe_cache: dict[str, pd.DataFrame | None],
) -> dict[str, object] | None:
    universe_key = signal.run.universe_key
    if universe_key not in universe_cache:
        try:
            universe_cache[universe_key] = mapped_only(universe_loader(universe_key))
        except (KeyError, FileNotFoundError, ValueError):
            universe_cache[universe_key] = None

    universe = universe_cache[universe_key]
    if universe is None or universe.empty:
        return None

    symbol = signal.symbol.upper().strip()
    matches = universe.loc[universe["symbol"].astype(str).str.upper().str.strip().eq(symbol)]
    if matches.empty:
        return None
    return dict(matches.iloc[0])


def _load_history(
    loader: DailyHistoryLoader,
    instrument: Mapping[str, object],
    start_date: dt.date,
    end_date: dt.date,
) -> pd.DataFrame | None:
    try:
        candles, _from_cache = loader.get_daily_history(instrument, start_date, end_date)
    except Exception:
        # Treat loader failures as retryable. Marking them insufficient would turn
        # a transient broker/cache issue into a permanent validation result.
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
) -> BenchmarkLeg | None:
    if (
        point.status is not ForwardReturnStatus.COMPUTED
        or point.entry_date is None
        or point.exit_date is None
    ):
        return None

    spec = benchmark_resolver(universe_key)
    if spec is None:
        return None

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
        return None

    return compute_benchmark_leg(
        benchmark_candles,
        entry_date=point.entry_date,
        exit_date=point.exit_date,
        benchmark_key=spec.key,
    )


def _store_point(
    session: Session,
    summary: ForwardReturnRunSummary,
    result_id: int,
    point: ForwardReturnPoint,
    *,
    benchmark: BenchmarkLeg | None,
) -> None:
    upsert_forward_return(session, result_id=result_id, point=point, benchmark=benchmark)
    if point.status is ForwardReturnStatus.COMPUTED:
        summary.computed += 1
        if benchmark is not None and benchmark.return_pct is not None:
            summary.benchmark_computed += 1
        else:
            summary.benchmark_missing += 1
    elif point.status is ForwardReturnStatus.PENDING:
        summary.pending += 1
    else:
        summary.insufficient += 1


def _history_end_date(
    signal_date: dt.date,
    as_of: dt.date,
    horizons: Sequence[int],
) -> dt.date:
    max_horizon = max(horizons, default=0)
    horizon_buffer = signal_date + dt.timedelta(days=max_horizon * 3)
    return max(as_of, horizon_buffer)
