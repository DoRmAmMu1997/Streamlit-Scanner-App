"""JOB-003 read model for latest-vs-previous scan comparisons.

The scan history tables already store every finalized shortlist, so comparison
does not need a new table. This module derives a small immutable view model from
``scan_runs`` and ``scan_results`` for the Streamlit page and CSV export.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from backend.storage.models import ScanResult, ScanRun
from backend.storage.repository import (
    get_latest_finalized_scan_runs,
    get_scan_results,
)

_EXPORT_COLUMNS = [
    "Change type",
    "Symbol",
    "Latest run",
    "Previous run",
    "Latest rating",
    "Previous rating",
    "Latest signal date",
    "Previous signal date",
    "Latest close",
    "Previous close",
    "Latest score",
    "Previous score",
    "Score source",
    "Score delta",
    "Latest reason",
    "Previous reason",
]


@dataclass(frozen=True)
class ComparisonRun:
    """Small run header safe to use after the database session closes."""

    run_id: int
    started: str
    finished: str
    status: str
    screener_key: str
    universe_key: str
    symbols_scanned: int | None
    shortlisted: int


@dataclass(frozen=True)
class _ResultSnapshot:
    """One result row normalized for symbol-set comparison."""

    symbol: str
    rating: str
    signal_date: str
    close: Decimal | None
    score: Decimal | None
    score_source: str | None
    reason: str


@dataclass(frozen=True)
class ComparisonRow:
    """One symbol's latest/previous state for a comparison section."""

    symbol: str
    latest_rating: str = ""
    previous_rating: str = ""
    latest_signal_date: str = ""
    previous_signal_date: str = ""
    latest_close: Decimal | None = None
    previous_close: Decimal | None = None
    latest_score: Decimal | None = None
    previous_score: Decimal | None = None
    score_source: str | None = None
    score_delta: Decimal | None = None
    latest_reason: str = ""
    previous_reason: str = ""


@dataclass(frozen=True)
class ScanComparison:
    """Derived latest-vs-previous comparison for one screener/universe pair."""

    latest_run: ComparisonRun
    previous_run: ComparisonRun | None
    new_today: Sequence[ComparisonRow] = ()
    repeated_from_yesterday: Sequence[ComparisonRow] = ()
    dropped_today: Sequence[ComparisonRow] = ()
    improved_score: Sequence[ComparisonRow] = ()
    degraded_score: Sequence[ComparisonRow] = ()

    def to_export_frame(self) -> pd.DataFrame:
        """Flatten all comparison sections into one stable CSV-ready frame."""
        rows: list[dict[str, object]] = []
        for section, section_rows in (
            ("New today", self.new_today),
            ("Repeated from yesterday", self.repeated_from_yesterday),
            ("Dropped today", self.dropped_today),
            ("Improved score", self.improved_score),
            ("Degraded score", self.degraded_score),
        ):
            rows.extend(
                _export_row(
                    section,
                    row,
                    latest_run_id=self.latest_run.run_id,
                    previous_run_id=(
                        self.previous_run.run_id if self.previous_run else None
                    ),
                )
                for row in section_rows
            )
        return pd.DataFrame(rows, columns=_EXPORT_COLUMNS)


def build_scan_comparison(
    session: Session,
    *,
    screener_key: str,
    universe_key: str,
) -> ScanComparison:
    """Compare the newest finalized run with the previous finalized run.

    "Today" and "yesterday" are product labels for latest and previous finalized
    runs; they are not strict calendar-day filters. That keeps manual UI scans
    and scheduled daily scans comparable through the same read model.
    """
    runs = get_latest_finalized_scan_runs(
        session,
        screener_key=screener_key,
        universe_key=universe_key,
        limit=2,
    )
    if not runs:
        raise ValueError(
            f"No finalized scan runs found for {screener_key}/{universe_key}."
        )

    latest_run = runs[0]
    latest_results = get_scan_results(session, latest_run.id)
    latest = _result_index(latest_results)
    latest_summary = _run_summary(latest_run, len(latest_results))

    if len(runs) == 1:
        return ScanComparison(latest_run=latest_summary, previous_run=None)

    previous_run = runs[1]
    previous_results = get_scan_results(session, previous_run.id)
    previous = _result_index(previous_results)
    previous_summary = _run_summary(previous_run, len(previous_results))

    latest_symbols = set(latest)
    previous_symbols = set(previous)

    new_rows = tuple(
        _comparison_row(symbol, latest=latest[symbol], previous=None)
        for symbol in sorted(latest_symbols - previous_symbols)
    )
    repeated_rows = tuple(
        _comparison_row(symbol, latest=latest[symbol], previous=previous[symbol])
        for symbol in sorted(latest_symbols & previous_symbols)
    )
    dropped_rows = tuple(
        _comparison_row(symbol, latest=None, previous=previous[symbol])
        for symbol in sorted(previous_symbols - latest_symbols)
    )

    improved_rows: list[ComparisonRow] = []
    degraded_rows: list[ComparisonRow] = []
    for row in repeated_rows:
        if row.score_delta is None:
            continue
        if row.score_delta > 0:
            improved_rows.append(row)
        elif row.score_delta < 0:
            degraded_rows.append(row)

    return ScanComparison(
        latest_run=latest_summary,
        previous_run=previous_summary,
        new_today=new_rows,
        repeated_from_yesterday=repeated_rows,
        dropped_today=dropped_rows,
        improved_score=tuple(improved_rows),
        degraded_score=tuple(degraded_rows),
    )


def _run_summary(run: ScanRun, shortlisted: int) -> ComparisonRun:
    return ComparisonRun(
        run_id=int(run.id),
        started=_format_utc_timestamp(run.started_at),
        finished=_format_utc_timestamp(run.finished_at),
        status=run.status.value,
        screener_key=run.screener_key,
        universe_key=run.universe_key,
        symbols_scanned=run.symbols_scanned,
        shortlisted=int(shortlisted),
    )


def _result_index(results: Sequence[ScanResult]) -> dict[str, _ResultSnapshot]:
    indexed: dict[str, _ResultSnapshot] = {}
    for result in results:
        symbol = str(result.symbol or "").strip().upper()
        if not symbol:
            continue
        indexed[symbol] = _snapshot(result, symbol=symbol)
    return indexed


def _snapshot(result: ScanResult, *, symbol: str) -> _ResultSnapshot:
    score, score_source = _result_score(result)
    return _ResultSnapshot(
        symbol=symbol,
        rating=result.rating or "",
        signal_date=result.signal_date.isoformat() if result.signal_date else "",
        close=result.close_price,
        score=score,
        score_source=score_source,
        reason=result.reason or "",
    )


def _result_score(result: ScanResult) -> tuple[Decimal | None, str | None]:
    if result.final_score is not None:
        return result.final_score, "final_score"
    raw = result.raw_result_json
    if isinstance(raw, Mapping) and "confidence" in raw:
        score = _decimal_or_none(raw.get("confidence"))
        if score is not None:
            return score, "confidence"
    return None, None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return score if score.is_finite() else None


def _comparison_row(
    symbol: str,
    *,
    latest: _ResultSnapshot | None,
    previous: _ResultSnapshot | None,
) -> ComparisonRow:
    score_source: str | None = None
    score_delta: Decimal | None = None
    if (
        latest is not None
        and previous is not None
        and latest.score is not None
        and previous.score is not None
        and latest.score_source == previous.score_source
    ):
        score_source = latest.score_source
        score_delta = latest.score - previous.score

    return ComparisonRow(
        symbol=symbol,
        latest_rating=latest.rating if latest else "",
        previous_rating=previous.rating if previous else "",
        latest_signal_date=latest.signal_date if latest else "",
        previous_signal_date=previous.signal_date if previous else "",
        latest_close=latest.close if latest else None,
        previous_close=previous.close if previous else None,
        latest_score=latest.score if latest else None,
        previous_score=previous.score if previous else None,
        score_source=score_source,
        score_delta=score_delta,
        latest_reason=latest.reason if latest else "",
        previous_reason=previous.reason if previous else "",
    )


def _export_row(
    section: str,
    row: ComparisonRow,
    *,
    latest_run_id: int,
    previous_run_id: int | None,
) -> dict[str, object]:
    has_latest = section != "Dropped today"
    has_previous = section != "New today"
    return {
        "Change type": section,
        "Symbol": row.symbol,
        "Latest run": latest_run_id if has_latest else None,
        "Previous run": previous_run_id if has_previous else None,
        "Latest rating": row.latest_rating,
        "Previous rating": row.previous_rating,
        "Latest signal date": row.latest_signal_date,
        "Previous signal date": row.previous_signal_date,
        "Latest close": row.latest_close,
        "Previous close": row.previous_close,
        "Latest score": row.latest_score,
        "Previous score": row.previous_score,
        "Score source": row.score_source or "",
        "Score delta": row.score_delta,
        "Latest reason": row.latest_reason,
        "Previous reason": row.previous_reason,
    }


def _format_utc_timestamp(value: dt.datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
