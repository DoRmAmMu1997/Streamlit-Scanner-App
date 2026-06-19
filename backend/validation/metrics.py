"""VALID-003A aggregate metrics over stored signal forward returns."""

from __future__ import annotations

import datetime as dt
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal

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
    signal_date_from: dt.date | None
    signal_date_to: dt.date | None
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
    """Collection of grouped validation metric rows plus overall status counts."""

    filters: ValidationMetricFilters
    rows: tuple[ValidationMetricRow, ...]
    total_signals: int
    total_computed: int
    total_pending: int
    total_insufficient_data: int


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
    records = get_forward_return_metric_records(
        session,
        screener_key=screener_key,
        universe_key=universe_key,
        horizon_days=horizon_days,
        signal_date_from=signal_date_from,
        signal_date_to=signal_date_to,
    )
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
        total_signals=len(records),
        total_computed=sum(
            1 for record in records if record.status is ForwardReturnStatus.COMPUTED
        ),
        total_pending=sum(
            1 for record in records if record.status is ForwardReturnStatus.PENDING
        ),
        total_insufficient_data=sum(
            1
            for record in records
            if record.status is ForwardReturnStatus.INSUFFICIENT_DATA
        ),
    )


def _build_metric_row(records: list[ForwardReturnMetricRecord]) -> ValidationMetricRow:
    first = records[0]
    computed_records = [
        record for record in records if record.status is ForwardReturnStatus.COMPUTED
    ]
    computed_returns = _values(record.forward_return_pct for record in computed_records)
    excess_returns = _values(record.excess_return_pct for record in computed_records)
    mae_values = _values(record.max_adverse_excursion_pct for record in computed_records)
    mfe_values = _values(record.max_favorable_excursion_pct for record in computed_records)
    dated_records = [record.signal_date for record in records if record.signal_date is not None]
    best = _best_signal(computed_records)
    worst = _worst_signal(computed_records)

    return ValidationMetricRow(
        screener_key=first.screener_key,
        universe_key=first.universe_key,
        horizon_days=first.horizon_days,
        signal_date_from=min(dated_records) if dated_records else None,
        signal_date_to=max(dated_records) if dated_records else None,
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
        best_signal=best,
        worst_signal=worst,
    )


def _values(values: Iterable[Decimal | None]) -> list[Decimal]:
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


def _best_signal(records: list[ForwardReturnMetricRecord]) -> BestWorstSignal | None:
    eligible = _eligible_best_worst_records(records)
    if not eligible:
        return None
    return _as_best_worst_signal(max(eligible, key=_forward_return_value))


def _worst_signal(records: list[ForwardReturnMetricRecord]) -> BestWorstSignal | None:
    eligible = _eligible_best_worst_records(records)
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
