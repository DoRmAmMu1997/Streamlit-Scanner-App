"""Structured quality checks for daily OHLCV candle frames."""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any, Literal, cast

import pandas as pd

DataQualitySeverity = Literal["warning", "fatal"]

DEFAULT_REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
MAX_CALENDAR_GAP_DAYS = 7
SUSPICIOUS_OVERNIGHT_GAP_PCT = 50.0
# How many calendar days the latest candle may trail ``expected_latest_date``
# before it counts as stale. Callers commonly pass today's date as "expected",
# but the newest *trading* candle is routinely a few days old on weekends,
# market holidays, or before the vendor publishes the current day's EOD bar.
# Tolerating a normal long weekend (Fri data on a Tue run = 4 days) keeps the
# stale signal meaningful instead of flagging the whole universe every off-day.
STALE_LATEST_TOLERANCE_DAYS = 4


@dataclass(frozen=True)
class DataQualityFinding:
    """One structured candle-quality issue found for a symbol."""

    code: str
    severity: DataQualitySeverity
    message: str
    affected_rows: int = 0


@dataclass(frozen=True)
class CandleQualityReport:
    """Quality result for one symbol's candle DataFrame."""

    symbol: str
    row_count: int
    latest_date: date | None
    findings: tuple[DataQualityFinding, ...]

    @property
    def has_fatal_findings(self) -> bool:
        """Return True when any finding means the frame must not be scanned."""
        return any(finding.severity == "fatal" for finding in self.findings)

    @property
    def is_usable(self) -> bool:
        """Return True when downstream scanner code may consume the frame."""
        return not self.has_fatal_findings


def validate_candles(
    df: pd.DataFrame,
    *,
    symbol: str,
    expected_latest_date: date | None = None,
    required_columns: Collection[str] | None = None,
    stale_tolerance_days: int = STALE_LATEST_TOLERANCE_DAYS,
) -> CandleQualityReport:
    """Validate a daily OHLCV frame without mutating caller-owned data.

    ``stale_tolerance_days`` is how far the latest candle may trail
    ``expected_latest_date`` before a (warning-only) ``STALE_LATEST_CANDLE``
    finding is raised. The default tolerates a normal long weekend so a run on a
    non-trading day (or before the vendor publishes the current EOD bar) does not
    flag every symbol as stale; pass ``0`` for an exact-date comparison.
    """
    symbol_label = str(symbol or "").strip().upper() or "UNKNOWN"
    row_count = len(df.index)
    if df.empty:
        return CandleQualityReport(
            symbol=symbol_label,
            row_count=row_count,
            latest_date=None,
            findings=(
                DataQualityFinding(
                    code="EMPTY_FRAME",
                    severity="fatal",
                    message=f"{symbol_label} has no candle rows.",
                ),
            ),
        )

    findings: list[DataQualityFinding] = []
    required = tuple(required_columns or DEFAULT_REQUIRED_COLUMNS)
    missing_columns = [column for column in required if column not in df.columns]
    if missing_columns:
        findings.append(
            DataQualityFinding(
                code="MISSING_REQUIRED_COLUMNS",
                severity="fatal",
                message=(
                    f"{symbol_label} is missing required candle column(s): "
                    f"{', '.join(sorted(missing_columns))}."
                ),
                affected_rows=row_count,
            )
        )

    parsed_dates = _extract_dates(df)
    if parsed_dates is None:
        findings.append(
            DataQualityFinding(
                code="MISSING_DATE_AXIS",
                severity="fatal",
                message=(
                    f"{symbol_label} must include a timestamp/date column or a "
                    "DatetimeIndex."
                ),
                affected_rows=row_count,
            )
        )
    elif parsed_dates.isna().any():
        findings.append(
            DataQualityFinding(
                code="INVALID_DATE",
                severity="fatal",
                message=f"{symbol_label} has unparseable candle date value(s).",
                affected_rows=int(parsed_dates.isna().sum()),
            )
        )

    latest_date = _latest_date(parsed_dates)
    if missing_columns or parsed_dates is None or parsed_dates.isna().any():
        return _report(symbol_label, row_count, latest_date, findings)

    duplicate_mask = parsed_dates.duplicated(keep=False)
    if duplicate_mask.any():
        findings.append(
            DataQualityFinding(
                code="DUPLICATE_DATE",
                severity="fatal",
                message=f"{symbol_label} has duplicate candle date row(s).",
                affected_rows=int(duplicate_mask.sum()),
            )
        )

    numeric = {column: pd.to_numeric(df[column], errors="coerce") for column in required}
    invalid_numeric_mask = pd.Series(False, index=df.index)
    for values in numeric.values():
        invalid_numeric_mask |= ~values.map(_is_finite)
    if invalid_numeric_mask.any():
        findings.append(
            DataQualityFinding(
                code="INVALID_NUMERIC_VALUE",
                severity="fatal",
                message=f"{symbol_label} has missing, non-numeric, NaN, or infinite OHLCV value(s).",
                affected_rows=int(invalid_numeric_mask.sum()),
            )
        )
        return _report(symbol_label, row_count, latest_date, findings)

    high = numeric["high"]
    low = numeric["low"]
    open_ = numeric["open"]
    close = numeric["close"]
    volume = numeric["volume"]

    high_below_low = high < low
    if high_below_low.any():
        findings.append(
            DataQualityFinding(
                code="HIGH_BELOW_LOW",
                severity="fatal",
                message=f"{symbol_label} has candle row(s) where high is below low.",
                affected_rows=int(high_below_low.sum()),
            )
        )

    valid_range = ~high_below_low
    open_outside = valid_range & ((open_ < low) | (open_ > high))
    if open_outside.any():
        findings.append(
            DataQualityFinding(
                code="OPEN_OUTSIDE_RANGE",
                severity="fatal",
                message=f"{symbol_label} has open price outside the candle low/high range.",
                affected_rows=int(open_outside.sum()),
            )
        )

    close_outside = valid_range & ((close < low) | (close > high))
    if close_outside.any():
        findings.append(
            DataQualityFinding(
                code="CLOSE_OUTSIDE_RANGE",
                severity="fatal",
                message=f"{symbol_label} has close price outside the candle low/high range.",
                affected_rows=int(close_outside.sum()),
            )
        )

    negative_volume = volume < 0
    if negative_volume.any():
        findings.append(
            DataQualityFinding(
                code="NEGATIVE_VOLUME",
                severity="fatal",
                message=f"{symbol_label} has negative volume value(s).",
                affected_rows=int(negative_volume.sum()),
            )
        )

    if (
        latest_date is not None
        and expected_latest_date is not None
        and (expected_latest_date - latest_date).days > max(0, stale_tolerance_days)
    ):
        findings.append(
            DataQualityFinding(
                code="STALE_LATEST_CANDLE",
                severity="warning",
                message=(
                    f"{symbol_label} latest candle is {latest_date.isoformat()}, "
                    f"before expected {expected_latest_date.isoformat()}."
                ),
                affected_rows=1,
            )
        )

    findings.extend(_calendar_gap_findings(symbol_label, parsed_dates))
    findings.extend(_price_gap_findings(symbol_label, parsed_dates, open_, close))
    return _report(symbol_label, row_count, latest_date, findings)


def _extract_dates(df: pd.DataFrame) -> pd.Series | None:
    """Return parsed daily dates from supported candle date locations."""
    if "timestamp" in df.columns:
        raw = df["timestamp"]
    elif "date" in df.columns:
        raw = df["date"]
    elif isinstance(df.index, pd.DatetimeIndex):
        raw = pd.Series(df.index, index=df.index)
    else:
        return None
    return pd.to_datetime(raw, errors="coerce").dt.date


def _latest_date(parsed_dates: pd.Series | None) -> date | None:
    """Return the latest parsed date, ignoring invalid date cells."""
    if parsed_dates is None:
        return None
    valid_dates = parsed_dates.dropna()
    if valid_dates.empty:
        return None
    return max(valid_dates)


def _is_finite(value: object) -> bool:
    """Return True for ordinary finite numeric values."""
    try:
        return bool(pd.notna(value) and math.isfinite(float(cast(Any, value))))
    except (TypeError, ValueError):
        return False


def _calendar_gap_findings(symbol: str, parsed_dates: pd.Series) -> list[DataQualityFinding]:
    ordered = sorted(parsed_dates.dropna().unique())
    if len(ordered) < 2:
        return []
    gap_count = sum(
        1
        for previous, current in pairwise(ordered)
        if (current - previous).days > MAX_CALENDAR_GAP_DAYS
    )
    if not gap_count:
        return []
    return [
        DataQualityFinding(
            code="CALENDAR_DATE_GAP",
            severity="warning",
            message=(
                f"{symbol} has candle date gap(s) greater than "
                f"{MAX_CALENDAR_GAP_DAYS} calendar days."
            ),
            affected_rows=gap_count,
        )
    ]


def _price_gap_findings(
    symbol: str,
    parsed_dates: pd.Series,
    open_: pd.Series,
    close: pd.Series,
) -> list[DataQualityFinding]:
    sortable = pd.DataFrame(
        {
            "date": parsed_dates,
            "open": open_,
            "close": close,
        }
    ).sort_values("date", kind="mergesort")
    previous_close = sortable["close"].shift(1)
    denominator = previous_close.abs()
    gap_pct = ((sortable["open"] - previous_close).abs() / denominator) * 100.0
    gap_mask = denominator.gt(0) & gap_pct.gt(SUSPICIOUS_OVERNIGHT_GAP_PCT)
    gap_count = int(gap_mask.sum())
    if not gap_count:
        return []
    return [
        DataQualityFinding(
            code="SUSPICIOUS_OVERNIGHT_PRICE_GAP",
            severity="warning",
            message=(
                f"{symbol} has overnight open-to-previous-close price gap(s) "
                f"greater than {SUSPICIOUS_OVERNIGHT_GAP_PCT:.0f}%."
            ),
            affected_rows=gap_count,
        )
    ]


def _report(
    symbol: str,
    row_count: int,
    latest_date: date | None,
    findings: list[DataQualityFinding],
) -> CandleQualityReport:
    return CandleQualityReport(
        symbol=symbol,
        row_count=row_count,
        latest_date=latest_date,
        findings=tuple(findings),
    )
