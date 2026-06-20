"""VALID-003A aggregate metrics over stored signal forward returns."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy.orm import Session

from backend.storage.models import ForwardReturnStatus
from backend.storage.repository import (
    ForwardReturnMetricRecord,
    get_forward_return_metric_records,
)
from backend.validation._pricing import PCT_QUANT


@dataclass(frozen=True)
class ValidationMetricFilters:
    """Echo of the filters used to build a validation metrics summary."""

    screener_key: str | None = None
    universe_key: str | None = None
    horizon_days: int | None = None
    signal_date_from: dt.date | None = None
    signal_date_to: dt.date | None = None


@dataclass(frozen=True)
class BestWorstSignal:
    """One signal selected as the best or worst computed forward return."""

    run_id: int
    result_id: int
    symbol: str
    signal_date: dt.date | None
    horizon_days: int
    forward_return_pct: Decimal
    excess_return_pct: Decimal | None = None


@dataclass(frozen=True)
class ValidationMetricRow:
    """Aggregate performance metrics for one screener/universe/horizon group."""

    screener_key: str
    universe_key: str
    horizon_days: int
    # Observed signal-date window for this group (the earliest/latest signal that
    # actually landed here) -- distinct from the *requested* bounds on
    # ``ValidationMetricFilters``.
    first_signal_date: dt.date | None
    last_signal_date: dt.date | None
    total_signals: int
    computed_count: int
    pending_count: int
    insufficient_data_count: int
    hit_rate_pct: Decimal | None
    average_forward_return_pct: Decimal | None
    median_forward_return_pct: Decimal | None
    average_excess_return_pct: Decimal | None
    median_excess_return_pct: Decimal | None
    average_mae_pct: Decimal | None
    average_mfe_pct: Decimal | None
    best_signal: BestWorstSignal | None
    worst_signal: BestWorstSignal | None


@dataclass(frozen=True)
class ValidationSummary:
    """Grouped validation metric rows plus overall measurement counts.

    The ``*_measurements`` totals count one row per ``signal x horizon`` after
    de-duplication, **not** distinct signals: a single signal measured at 20/60/120
    days contributes three measurements. Per-signal counts live on each
    ``ValidationMetricRow`` (which is already scoped to one horizon).
    """

    filters: ValidationMetricFilters
    rows: tuple[ValidationMetricRow, ...]
    total_measurements: int
    computed_measurements: int
    pending_measurements: int
    insufficient_data_measurements: int


@dataclass(frozen=True)
class ValidationReturnBucket:
    """Computed-return histogram bucket for one screener/universe/horizon group."""

    screener_key: str
    universe_key: str
    horizon_days: int
    bucket_label: str
    computed_count: int


@dataclass(frozen=True)
class ValidationBenchmarkRow:
    """Benchmark-relative dashboard row for one screener/universe/horizon group."""

    screener_key: str
    universe_key: str
    horizon_days: int
    computed_count: int
    hit_rate_pct: Decimal | None
    average_excess_return_pct: Decimal | None
    median_excess_return_pct: Decimal | None


@dataclass(frozen=True)
class ValidationTimeSeriesPoint:
    """One monthly signal-count point for the dashboard timeline."""

    screener_key: str
    universe_key: str
    horizon_days: int
    period_start: dt.date
    total_signals: int
    computed_count: int
    pending_count: int
    insufficient_data_count: int


@dataclass(frozen=True)
class ValidationSectorConcentrationRow:
    """Sector-level signal concentration for one screener/universe/horizon group."""

    screener_key: str
    universe_key: str
    horizon_days: int
    sector: str
    total_signals: int
    computed_count: int
    share_of_group_pct: Decimal
    hit_rate_pct: Decimal | None
    average_forward_return_pct: Decimal | None


@dataclass(frozen=True)
class ValidationDashboardSummary:
    """Richer read model used by the VALID-004 Streamlit dashboard.

    ``metric_summary`` is the existing VALID-003A summary table contract. The
    extra tuples are derived from the same de-duplicated records so the dashboard
    sections agree with one another and never re-count overlapping reruns.
    """

    metric_summary: ValidationSummary
    return_distribution: tuple[ValidationReturnBucket, ...]
    # One per-horizon row per group, carrying both the win rate (hit_rate_pct) and
    # the benchmark-relative excess columns. The dashboard renders two sections
    # from this single tuple (win rate vs benchmark-relative) rather than storing
    # the same rows twice.
    benchmark_relative_rows: tuple[ValidationBenchmarkRow, ...]
    signal_count_over_time: tuple[ValidationTimeSeriesPoint, ...]
    sector_concentration: tuple[ValidationSectorConcentrationRow, ...]


def summarize_validation_metrics(
    session: Session,
    *,
    screener_key: str | None = None,
    universe_key: str | None = None,
    horizon_days: int | None = None,
    signal_date_from: dt.date | None = None,
    signal_date_to: dt.date | None = None,
) -> ValidationSummary:
    """Aggregate stored forward-return rows into screener performance metrics.

    This is a read model over rows that VALID-002 already computed. It does not
    fetch prices, run screeners, or fill missing rows; pending and insufficient
    measurements stay visible as separate counts so they never become hidden
    losses in hit-rate or average-return calculations.
    """
    filters = ValidationMetricFilters(
        screener_key=screener_key,
        universe_key=universe_key,
        horizon_days=horizon_days,
        signal_date_from=signal_date_from,
        signal_date_to=signal_date_to,
    )
    records = _metric_records_for_filters(session, filters)
    return _build_summary(filters, records)


def summarize_validation_dashboard(
    session: Session,
    *,
    screener_key: str | None = None,
    universe_key: str | None = None,
    horizon_days: int | None = None,
    signal_date_from: dt.date | None = None,
    signal_date_to: dt.date | None = None,
    sector_lookup: Mapping[Any, str] | None = None,
) -> ValidationDashboardSummary:
    """Build all read-only dashboard sections from stored validation rows.

    No prices are fetched here. VALID-004's dashboard is a read model over rows
    the compute job already stored; the only optional enrichment is a local
    ``sector_lookup`` mapping. Missing sector metadata becomes ``"Unknown"`` so
    the UI can be honest without inventing classifications.
    """
    filters = ValidationMetricFilters(
        screener_key=screener_key,
        universe_key=universe_key,
        horizon_days=horizon_days,
        signal_date_from=signal_date_from,
        signal_date_to=signal_date_to,
    )
    records = _metric_records_for_filters(session, filters)
    metric_summary = _build_summary(filters, records)
    return ValidationDashboardSummary(
        metric_summary=metric_summary,
        return_distribution=_return_distribution(records),
        benchmark_relative_rows=_benchmark_rows(metric_summary.rows),
        signal_count_over_time=_signal_count_over_time(records),
        sector_concentration=_sector_concentration(records, sector_lookup=sector_lookup),
    )


def _metric_records_for_filters(
    session: Session, filters: ValidationMetricFilters
) -> list[ForwardReturnMetricRecord]:
    return _dedupe_latest_run(
        get_forward_return_metric_records(
            session,
            screener_key=filters.screener_key,
            universe_key=filters.universe_key,
            horizon_days=filters.horizon_days,
            signal_date_from=filters.signal_date_from,
            signal_date_to=filters.signal_date_to,
        )
    )


def _build_summary(
    filters: ValidationMetricFilters,
    records: list[ForwardReturnMetricRecord],
) -> ValidationSummary:
    groups: dict[tuple[str, str, int], list[ForwardReturnMetricRecord]] = defaultdict(list)
    for record in records:
        groups[(record.screener_key, record.universe_key, record.horizon_days)].append(record)

    rows = tuple(
        _build_metric_row(group_records)
        for _group_key, group_records in sorted(groups.items(), key=lambda item: item[0])
    )
    return ValidationSummary(
        filters=filters,
        rows=rows,
        total_measurements=len(records),
        computed_measurements=sum(
            1 for record in records if record.status is ForwardReturnStatus.COMPUTED
        ),
        pending_measurements=sum(
            1 for record in records if record.status is ForwardReturnStatus.PENDING
        ),
        insufficient_data_measurements=sum(
            1
            for record in records
            if record.status is ForwardReturnStatus.INSUFFICIENT_DATA
        ),
    )


def _benchmark_rows(
    rows: tuple[ValidationMetricRow, ...],
) -> tuple[ValidationBenchmarkRow, ...]:
    return tuple(
        ValidationBenchmarkRow(
            screener_key=row.screener_key,
            universe_key=row.universe_key,
            horizon_days=row.horizon_days,
            computed_count=row.computed_count,
            hit_rate_pct=row.hit_rate_pct,
            average_excess_return_pct=row.average_excess_return_pct,
            median_excess_return_pct=row.median_excess_return_pct,
        )
        for row in rows
    )


def _return_distribution(
    records: list[ForwardReturnMetricRecord],
) -> tuple[ValidationReturnBucket, ...]:
    buckets: dict[tuple[str, str, int, str], int] = defaultdict(int)
    for record in records:
        if (
            record.status is not ForwardReturnStatus.COMPUTED
            or record.forward_return_pct is None
        ):
            continue
        buckets[
            (
                record.screener_key,
                record.universe_key,
                record.horizon_days,
                _return_bucket_label(record.forward_return_pct),
            )
        ] += 1

    return tuple(
        ValidationReturnBucket(
            screener_key=screener_key,
            universe_key=universe_key,
            horizon_days=horizon_days,
            bucket_label=bucket_label,
            computed_count=count,
        )
        for (screener_key, universe_key, horizon_days, bucket_label), count in sorted(
            buckets.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[0][2],
                _BUCKET_ORDER[item[0][3]],
            ),
        )
    )


def _signal_count_over_time(
    records: list[ForwardReturnMetricRecord],
) -> tuple[ValidationTimeSeriesPoint, ...]:
    grouped: dict[
        tuple[str, str, int, dt.date], list[ForwardReturnMetricRecord]
    ] = defaultdict(list)
    for record in records:
        if record.signal_date is None:
            continue
        grouped[
            (
                record.screener_key,
                record.universe_key,
                record.horizon_days,
                dt.date(record.signal_date.year, record.signal_date.month, 1),
            )
        ].append(record)

    return tuple(
        ValidationTimeSeriesPoint(
            screener_key=screener_key,
            universe_key=universe_key,
            horizon_days=horizon_days,
            period_start=period_start,
            total_signals=len(group_records),
            computed_count=sum(
                1
                for record in group_records
                if record.status is ForwardReturnStatus.COMPUTED
            ),
            pending_count=sum(
                1
                for record in group_records
                if record.status is ForwardReturnStatus.PENDING
            ),
            insufficient_data_count=sum(
                1
                for record in group_records
                if record.status is ForwardReturnStatus.INSUFFICIENT_DATA
            ),
        )
        for (screener_key, universe_key, horizon_days, period_start), group_records in sorted(
            grouped.items(), key=lambda item: item[0]
        )
    )


def _sector_concentration(
    records: list[ForwardReturnMetricRecord],
    *,
    sector_lookup: Mapping[Any, str] | None,
) -> tuple[ValidationSectorConcentrationRow, ...]:
    group_totals: dict[tuple[str, str, int], int] = defaultdict(int)
    sector_groups: dict[
        tuple[str, str, int, str], list[ForwardReturnMetricRecord]
    ] = defaultdict(list)
    for record in records:
        group_key = (record.screener_key, record.universe_key, record.horizon_days)
        group_totals[group_key] += 1
        sector_groups[
            (
                record.screener_key,
                record.universe_key,
                record.horizon_days,
                _sector_for_record(record, sector_lookup),
            )
        ].append(record)

    rows: list[ValidationSectorConcentrationRow] = []
    for (
        screener_key,
        universe_key,
        horizon_days,
        sector,
    ), group_records in sorted(sector_groups.items(), key=lambda item: item[0]):
        computed_records = [
            record
            for record in group_records
            if record.status is ForwardReturnStatus.COMPUTED
        ]
        # ``computed_count`` matches the main summary table: every COMPUTED row,
        # not only those with a stored return. ``computed_returns`` (non-null only)
        # drives hit rate and average, exactly like ``_build_metric_row``.
        computed_returns = _values(
            record.forward_return_pct for record in computed_records
        )
        rows.append(
            ValidationSectorConcentrationRow(
                screener_key=screener_key,
                universe_key=universe_key,
                horizon_days=horizon_days,
                sector=sector,
                total_signals=len(group_records),
                computed_count=len(computed_records),
                share_of_group_pct=_pct(
                    len(group_records),
                    group_totals[(screener_key, universe_key, horizon_days)],
                ),
                hit_rate_pct=_hit_rate(computed_returns),
                average_forward_return_pct=_average(computed_returns),
            )
        )
    return tuple(rows)


_BUCKET_LABELS = (
    "<= -10%",
    "-10% to 0%",
    "0% to 10%",
    "10% to 20%",
    ">= 20%",
)
_BUCKET_ORDER = {label: index for index, label in enumerate(_BUCKET_LABELS)}


def _return_bucket_label(value: Decimal) -> str:
    if value <= Decimal("-10"):
        return "<= -10%"
    if value < Decimal("0"):
        return "-10% to 0%"
    if value < Decimal("10"):
        return "0% to 10%"
    if value < Decimal("20"):
        return "10% to 20%"
    return ">= 20%"


def _pct(part: int, whole: int) -> Decimal:
    if whole <= 0:
        return Decimal("0").quantize(PCT_QUANT)
    return ((Decimal(part) / Decimal(whole)) * Decimal("100")).quantize(PCT_QUANT)


def _sector_for_record(
    record: ForwardReturnMetricRecord,
    sector_lookup: Mapping[Any, str] | None,
) -> str:
    if not sector_lookup:
        return "Unknown"
    symbol = record.symbol.upper().strip()
    # ``load_universe_sector_lookup`` keys on (universe_key, UPPER_SYMBOL); that is
    # the only shape the dashboard passes, so a single lookup is enough.
    sector = sector_lookup.get((record.universe_key, symbol))
    if sector is None or not str(sector).strip():
        return "Unknown"
    return str(sector).strip()


def _dedupe_latest_run(
    records: list[ForwardReturnMetricRecord],
) -> list[ForwardReturnMetricRecord]:
    """Keep one record per signal+horizon, preferring the most recent run.

    The same shortlisted signal can be measured by more than one run -- a daily
    job retried after a failure, or an overlapping backfill -- which would
    otherwise count one real signal several times and skew hit rate and averages.
    The key is ``(screener, universe, symbol, signal_date, horizon)``; the winner
    is the record from the run with the greatest ``(started_at, run_id)``. Signals
    with no ``signal_date`` de-duplicate within their own key.
    """
    DedupeKey = tuple[str, str, str, dt.date | None, int]
    latest: dict[DedupeKey, ForwardReturnMetricRecord] = {}
    for record in records:
        key: DedupeKey = (
            record.screener_key,
            record.universe_key,
            record.symbol,
            record.signal_date,
            record.horizon_days,
        )
        current = latest.get(key)
        if current is None or (record.run_started_at, record.run_id) > (
            current.run_started_at,
            current.run_id,
        ):
            latest[key] = record
    return list(latest.values())


def _build_metric_row(records: list[ForwardReturnMetricRecord]) -> ValidationMetricRow:
    first = records[0]
    computed_records = [
        record for record in records if record.status is ForwardReturnStatus.COMPUTED
    ]
    computed_returns = _values(record.forward_return_pct for record in computed_records)
    excess_returns = _values(record.excess_return_pct for record in computed_records)
    mae_values = _values(record.max_adverse_excursion_pct for record in computed_records)
    mfe_values = _values(record.max_favorable_excursion_pct for record in computed_records)
    signal_dates = [record.signal_date for record in records if record.signal_date is not None]
    # Build the deterministically ordered candidate list once; best and worst both
    # read from it instead of re-sorting the same rows twice.
    eligible = _eligible_best_worst_records(computed_records)

    return ValidationMetricRow(
        screener_key=first.screener_key,
        universe_key=first.universe_key,
        horizon_days=first.horizon_days,
        first_signal_date=min(signal_dates) if signal_dates else None,
        last_signal_date=max(signal_dates) if signal_dates else None,
        total_signals=len(records),
        computed_count=len(computed_records),
        pending_count=sum(1 for record in records if record.status is ForwardReturnStatus.PENDING),
        insufficient_data_count=sum(
            1 for record in records if record.status is ForwardReturnStatus.INSUFFICIENT_DATA
        ),
        hit_rate_pct=_hit_rate(computed_returns),
        average_forward_return_pct=_average(computed_returns),
        median_forward_return_pct=_median(computed_returns),
        average_excess_return_pct=_average(excess_returns),
        median_excess_return_pct=_median(excess_returns),
        average_mae_pct=_average(mae_values),
        average_mfe_pct=_average(mfe_values),
        best_signal=_best_signal(eligible),
        worst_signal=_worst_signal(eligible),
    )


def _values(values: Iterable[Decimal | None]) -> list[Decimal]:
    # Drop NULL measurements (benchmark/excess/MAE/MFE are all optional). Every
    # Numeric column maps to Decimal, so the ``isinstance`` check both removes
    # ``None`` and refuses to average a stray non-Decimal rather than crashing.
    return [value for value in values if isinstance(value, Decimal)]


def _average(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    return (sum(values, Decimal("0")) / Decimal(len(values))).quantize(PCT_QUANT)


def _median(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint].quantize(PCT_QUANT)
    return ((ordered[midpoint - 1] + ordered[midpoint]) / Decimal("2")).quantize(PCT_QUANT)


def _hit_rate(values: list[Decimal]) -> Decimal | None:
    if not values:
        return None
    winners = sum(1 for value in values if value > Decimal("0"))
    return ((Decimal(winners) / Decimal(len(values))) * Decimal("100")).quantize(PCT_QUANT)


def _best_signal(eligible: list[ForwardReturnMetricRecord]) -> BestWorstSignal | None:
    if not eligible:
        return None
    return _as_best_worst_signal(max(eligible, key=_forward_return_value))


def _worst_signal(eligible: list[ForwardReturnMetricRecord]) -> BestWorstSignal | None:
    if not eligible:
        return None
    return _as_best_worst_signal(min(eligible, key=_forward_return_value))


def _eligible_best_worst_records(
    records: list[ForwardReturnMetricRecord],
) -> list[ForwardReturnMetricRecord]:
    # Sorting before min/max gives deterministic ties: earliest signal date, then
    # lowest result id, exactly matching the repository's stable row ordering.
    return sorted(
        [record for record in records if record.forward_return_pct is not None],
        key=lambda record: (
            record.signal_date or dt.date.max,
            record.result_id,
        ),
    )


def _forward_return_value(record: ForwardReturnMetricRecord) -> Decimal:
    if record.forward_return_pct is None:
        raise ValueError("best/worst signal requires a stored forward return")
    return record.forward_return_pct


def _as_best_worst_signal(record: ForwardReturnMetricRecord) -> BestWorstSignal:
    return BestWorstSignal(
        run_id=record.run_id,
        result_id=record.result_id,
        symbol=record.symbol,
        signal_date=record.signal_date,
        horizon_days=record.horizon_days,
        forward_return_pct=_forward_return_value(record),
        excess_return_pct=record.excess_return_pct,
    )
