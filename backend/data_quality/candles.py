"""Structured quality checks for daily OHLCV candle frames (DATA-001A).

Beginner note:
A "candle" is one day's price bar — open, high, low, close, and traded volume
(OHLCV) — and every screener in this app reads a pandas DataFrame of these bars.
If that data is malformed (a high below the low, a NaN price, duplicate dates) or
stale (the newest bar is weeks old), a strategy can fire a confident-looking but
completely false signal. This module is the gatekeeper that catches such data
*before* any strategy math runs.

How it works:
- ``validate_candles`` inspects one symbol's frame and returns an immutable
  ``CandleQualityReport`` — a list of ``DataQualityFinding`` objects, each with a
  stable ``code`` (e.g. ``HIGH_BELOW_LOW``) and a ``severity``.
- Severity has exactly two levels:
    * **fatal**  — the data is structurally unusable (high < low, NaN, duplicate
      dates, ...). The caller must NOT scan this frame.
    * **warning** — the data is usable but suspicious (stale, big calendar gaps,
      huge overnight price jumps). The caller records it but still scans.
- The function is *pure*: it never mutates the caller's DataFrame, never does I/O,
  and never raises for bad data (it reports it). That makes it trivial to unit
  test and safe to call from anywhere a candle frame enters the system.

Returning structured findings (instead of printing or raising) lets the loader
decide what to do per severity — see ``backend/daily_data_loader.py`` and
``docs/architecture/components/data-quality.md``.
"""

from __future__ import annotations

import math
from collections.abc import Collection
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from typing import Any, Literal, cast

import pandas as pd

# The two severities a finding can carry. Using a ``Literal`` (not a free string)
# means a typo like "fatale" is a type error, not a silently-ignored severity.
DataQualitySeverity = Literal["warning", "fatal"]

# Columns every candle frame must contain. ``validate_candles`` lets a caller
# override this, but the Dhan loader always produces exactly these five.
DEFAULT_REQUIRED_COLUMNS: tuple[str, ...] = ("open", "high", "low", "close", "volume")
# Tuning knobs for the two "suspicious but usable" (warning) checks. They are
# module constants so the thresholds live in one obvious place and tests can
# reference them instead of hard-coding magic numbers.
MAX_CALENDAR_GAP_DAYS = 7  # a gap larger than this between two bars is flagged
SUSPICIOUS_OVERNIGHT_GAP_PCT = 50.0  # an open this far from the prior close is flagged
# How many calendar days the latest candle may trail ``expected_latest_date``
# before it counts as stale. Callers commonly pass today's date as "expected",
# but the newest *trading* candle is routinely a few days old on weekends,
# market holidays, or before the vendor publishes the current day's EOD bar.
# Tolerating a normal long weekend (Fri data on a Tue run = 4 days) keeps the
# stale signal meaningful instead of flagging the whole universe every off-day.
STALE_LATEST_TOLERANCE_DAYS = 4


# ``frozen=True`` makes these dataclasses immutable: once built, a report cannot
# be edited. That is deliberate — a quality verdict is evidence, and evidence
# that downstream code could quietly mutate would be worthless for an audit.
@dataclass(frozen=True)
class DataQualityFinding:
    """One structured candle-quality issue found for a symbol.

    - ``code``: a stable machine-readable label (e.g. ``NEGATIVE_VOLUME``) that
      logs, the receipt, and tests can match on without parsing prose.
    - ``severity``: ``"fatal"`` (don't scan) or ``"warning"`` (scan, but record).
    - ``message``: a short human-readable explanation for the health page/logs.
    - ``affected_rows``: how many rows tripped the check (0 when not row-specific).
    """

    code: str
    severity: DataQualitySeverity
    message: str
    affected_rows: int = 0


@dataclass(frozen=True)
class CandleQualityReport:
    """The full quality verdict for one symbol's candle DataFrame.

    ``findings`` is empty when the frame is clean. ``latest_date`` is the newest
    parsed bar date (or ``None`` when there were no usable dates), handy for the
    stale check and for display.
    """

    symbol: str
    row_count: int
    latest_date: date | None
    findings: tuple[DataQualityFinding, ...]

    @property
    def has_fatal_findings(self) -> bool:
        """True when at least one finding is fatal — i.e. the frame must not be scanned."""
        return any(finding.severity == "fatal" for finding in self.findings)

    @property
    def is_usable(self) -> bool:
        """True when downstream scanner code may consume the frame (no fatal findings).

        Warning-only frames are still usable; only fatal findings block scanning.
        """
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

    Checks run from most-structural to least, and we *fail fast* on the
    structural ones: if the frame is empty, missing columns, or has unparseable
    dates, there is nothing meaningful to compare, so we return early with just
    those fatal findings rather than emitting confusing follow-on errors. Once the
    shape is known-good we run the per-row value checks (high<low, open/close
    range, negative volume) and finally the warning-level checks (stale, calendar
    gaps, price gaps).

    Args:
        df: the candle frame to inspect (never modified).
        symbol: ticker, used only for labelling findings.
        expected_latest_date: the date the newest bar *should* reach; enables the
            stale check. ``None`` skips it.
        required_columns: override ``DEFAULT_REQUIRED_COLUMNS`` if a caller's frame
            uses a different schema.
        stale_tolerance_days: how far the latest candle may trail
            ``expected_latest_date`` before a (warning-only) ``STALE_LATEST_CANDLE``
            finding is raised. The default tolerates a normal long weekend so a run
            on a non-trading day (or before the vendor publishes the current EOD
            bar) does not flag every symbol as stale; pass ``0`` for an exact
            comparison.
    """
    # Normalize the label once so every finding message reads the same, even if
    # the caller passed "  reliance " or an empty string.
    symbol_label = str(symbol or "").strip().upper() or "UNKNOWN"
    row_count = len(df.index)
    # No rows at all is the simplest fatal case and short-circuits everything else.
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
    # Structural check 1: are the OHLCV columns even present? Without them the
    # value checks below would raise a KeyError, so a miss here is fatal.
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

    # Structural check 2: every bar needs a date. ``_extract_dates`` returns None
    # when there is no usable date axis at all, or a Series with NaT cells when
    # some individual dates could not be parsed — both are fatal.
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

    # ``latest_date`` is computed even on the early-exit path so the report still
    # carries whatever newest date we could parse (useful for the health page).
    latest_date = _latest_date(parsed_dates)
    # Fail fast: if the columns or the date axis are broken, the value checks
    # below cannot run meaningfully, so return with just the structural findings.
    if missing_columns or parsed_dates is None or parsed_dates.isna().any():
        return _report(symbol_label, row_count, latest_date, findings)

    # Duplicate dates mean two bars claim the same day — an indicator math bug
    # waiting to happen (e.g. a doubled volume), so it is fatal. ``keep=False``
    # flags *every* member of a duplicate group, not just the repeats.
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

    # Coerce each OHLCV column to numbers; ``errors="coerce"`` turns anything
    # un-parseable (e.g. the string "N/A") into NaN instead of raising. We then
    # build one boolean mask that is True for any row whose value in *any* column
    # is not an ordinary finite number (NaN, inf, or non-numeric). ``|=`` ORs the
    # per-column masks together row-by-row.
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
        # Comparisons like ``high < low`` are meaningless once a value is NaN/inf,
        # so stop here rather than emit a pile of derived, confusing findings.
        return _report(symbol_label, row_count, latest_date, findings)

    # All five columns are now guaranteed finite, so the comparisons below are safe.
    high = numeric["high"]
    low = numeric["low"]
    open_ = numeric["open"]
    close = numeric["close"]
    volume = numeric["volume"]

    # A bar where high < low is physically impossible (the high is the day's peak).
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

    # "open/close must sit within [low, high]" only *means* anything on rows whose
    # low/high are themselves sane. ``valid_range`` masks out the high<low rows so
    # we don't double-report them as open/close-out-of-range too. ``&``/``|`` are
    # element-wise boolean AND/OR across the Series.
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

    # Warning-level check: is the newest bar too old? Only fires when the caller
    # supplied an ``expected_latest_date`` AND the gap exceeds the tolerance (see
    # the ``STALE_LATEST_TOLERANCE_DAYS`` note above for why a few days is normal).
    # The ``... is not None`` guards also let the type checker prove the dates are
    # real before the subtraction.
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

    # The last two warning checks live in helpers because they each need the dates
    # in sorted order; ``extend`` appends their (possibly empty) finding lists.
    findings.extend(_calendar_gap_findings(symbol_label, parsed_dates))
    findings.extend(_price_gap_findings(symbol_label, parsed_dates, open_, close))
    return _report(symbol_label, row_count, latest_date, findings)


def _extract_dates(df: pd.DataFrame) -> pd.Series | None:
    """Return each row's date, from whichever date location the frame uses.

    Different loaders shape the date differently, so we accept any of three: a
    ``timestamp`` column (the Dhan loader's output), a ``date`` column, or a
    ``DatetimeIndex``. Returns ``None`` when none is present (a fatal
    ``MISSING_DATE_AXIS`` upstream). ``errors="coerce"`` turns an unparseable
    value into ``NaT`` rather than raising, and ``.dt.date`` drops the time part
    so two bars on the same day compare equal.
    """
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
    """Return the newest valid bar date, or ``None`` if there are no valid dates.

    ``dropna()`` discards any ``NaT`` cells first so a single bad date can't poison
    the max.
    """
    if parsed_dates is None:
        return None
    valid_dates = parsed_dates.dropna()
    if valid_dates.empty:
        return None
    return max(valid_dates)


def _is_finite(value: object) -> bool:
    """Return True only for an ordinary finite number (not NaN, inf, or junk).

    Wrapped in try/except because ``float(value)`` raises for non-numeric input
    (e.g. a stray string); we treat any such value as "not finite" rather than
    letting the exception escape.
    """
    try:
        # cast(Any, ...) both times: pandas-stubs' notna overloads take concrete
        # scalar/array types, but this boundary helper deliberately accepts any
        # cell value and treats "cannot even check" as not-finite.
        return bool(pd.notna(cast(Any, value)) and math.isfinite(float(cast(Any, value))))
    except (TypeError, ValueError):
        return False


def _calendar_gap_findings(symbol: str, parsed_dates: pd.Series) -> list[DataQualityFinding]:
    """Flag (as a warning) any gap larger than ``MAX_CALENDAR_GAP_DAYS`` between
    consecutive trading days — a sign the vendor may have dropped data.

    ``sorted(...unique())`` gives the distinct dates in order; ``pairwise`` walks
    them two-at-a-time as ``(previous, current)`` so each ``current - previous`` is
    one inter-bar gap. Weekends (≈3 days) stay under the threshold by design.
    """
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
    """Flag (as a warning) an open that jumps more than
    ``SUSPICIOUS_OVERNIGHT_GAP_PCT`` from the prior day's close — usually a bad
    tick or an un-adjusted split rather than a real move.

    We sort by date (``mergesort`` is stable), then ``shift(1)`` lines up each
    bar's *previous* close beside its open. ``denominator.gt(0)`` skips rows with a
    zero/absent prior close so we never divide by zero (the first row's
    ``previous_close`` is NaN, which the guard also drops).
    """
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
    """Freeze the accumulated findings into the immutable report (one place so the
    ``list -> tuple`` conversion isn't repeated at every early return)."""
    return CandleQualityReport(
        symbol=symbol,
        row_count=row_count,
        latest_date=latest_date,
        findings=tuple(findings),
    )
