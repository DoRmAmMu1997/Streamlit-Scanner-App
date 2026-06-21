"""Pure RANK-002 scoring components.

Beginner note:
This file deliberately knows nothing about Streamlit, SQLAlchemy, or Dhan. Each
function receives ordinary Python/pandas values and returns either a finite
``0..100`` score or a missing marker. The model layer decides how to combine the
components and how to record missing data.

That separation matters for reliability: bad result-row numbers, malformed
cached candles, or missing volumes should only drop the affected component. They
must never produce NaN/inf final scores or crash a whole scan.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

import numpy as np
import pandas as pd

from backend.indicators import prepare_ohlc

NEUTRAL_SCORE = 50.0

# Higher raw values are better for these fields. ``confidence`` is handled first
# because AI-assisted screeners already expose a direct 0-10 conviction score.
_POSITIVE_TECHNICAL_FIELDS = (
    "pct_below_basis",
    "drawdown_pct",
    "upside_to_ath_pct",
)

# Lower raw values are better for these distance/proximity fields. Returning the
# negative value lets the shared cross-sectional normalizer keep the simple
# "higher raw -> higher score" convention.
_LOWER_IS_BETTER_TECHNICAL_FIELDS = (
    "bb_distance_pct",
    "env_distance_pct",
    "kd_retest_distance_pct",
    "proximity_pct_at_signal",
)


def cross_sectional(values: pd.Series) -> pd.Series:
    """Normalize present values to ``0..100`` within the current shortlist.

    Missing, unparsable, and infinite values remain ``NaN`` so the caller can
    drop that component for only that row. When all present values are equal,
    the data exists but carries no relative ranking information, so every
    present value receives the neutral midpoint of 50.

    Example:
    ``[10, 20, 30]`` becomes ``[0, 50, 100]``. A missing value in that same
    series stays missing, because inventing a component score would make the
    final score look more certain than it really is.
    """
    # ``pd.to_numeric`` is the first safety gate. Screener rows are assembled
    # from many strategies, so values can arrive as ints, floats, strings, or
    # accidentally as "not available" text. Coerce what we can and mark the rest
    # missing for the caller to handle row-by-row.
    numeric = pd.to_numeric(values, errors="coerce")
    numeric = numeric.where(np.isfinite(numeric), np.nan)
    present = numeric.dropna()
    if present.empty:
        return numeric

    low = float(present.min())
    high = float(present.max())
    if high <= low:
        # Equal present values mean "we know the component, but it cannot
        # separate the rows." Neutral 50 is different from NaN: the component is
        # counted in coverage, but it contributes neither an advantage nor a
        # penalty within this run.
        return numeric.where(numeric.isna(), NEUTRAL_SCORE)
    return ((numeric - low) / (high - low) * 100.0).clip(0.0, 100.0)


def technical_raw(row: Mapping[str, Any]) -> float | None:
    """Return the generic v1 technical-strength raw value for one result row.

    Screeners do not all emit the same columns yet. RANK-002 therefore uses a
    deterministic resolver:

    1. Prefer numeric ``confidence`` when present.
    2. Fall back to known deterministic strength/proximity fields.
    3. Return ``None`` when no known numeric signal exists.

    The returned value is only a raw input. ``score_candidates`` compares it
    against other rows in the same run with ``cross_sectional``.
    """
    # AI-backed or hybrid screeners can expose confidence directly. Treat it as
    # the most explicit technical-strength signal when present.
    confidence = _finite_float(row.get("confidence"))
    if confidence is not None:
        return confidence

    # These fields already mean "larger is better" for ranking, so they can be
    # passed through unchanged.
    for field in _POSITIVE_TECHNICAL_FIELDS:
        value = _finite_float(row.get(field))
        if value is not None:
            return value

    # Proximity fields mean the opposite: a smaller distance is better. Negating
    # keeps the normalizer's rule simple everywhere else: bigger raw is better.
    for field in _LOWER_IS_BETTER_TECHNICAL_FIELDS:
        value = _finite_float(row.get(field))
        if value is not None:
            return -value

    return None


def liquidity_raw(candles: pd.DataFrame, window: int) -> float | None:
    """Return trailing mean traded value, ``mean(volume * close)``.

    ``None`` means the component should be dropped. We require a full trailing
    window and a ``volume`` column because guessing liquidity from price alone
    would fabricate a score.
    """
    if candles is None or candles.empty or "volume" not in candles.columns:
        return None
    window_size = _positive_int(window)
    if window_size is None:
        return None

    try:
        frame = prepare_ohlc(candles)
    except ValueError:
        return None
    if frame.empty or len(frame) < window_size or "volume" not in frame.columns:
        return None

    # Use only the trailing configured window. A partial window would make a
    # newly cached symbol look comparable to a fully cached one even though the
    # sample size is different.
    close = pd.to_numeric(frame["close"].tail(window_size), errors="coerce")
    volume = pd.to_numeric(frame["volume"].tail(window_size), errors="coerce")
    traded = (close * volume).where(lambda series: np.isfinite(series), np.nan)
    if traded.isna().any():
        return None
    mean_value = float(traded.mean())
    if not math.isfinite(mean_value) or mean_value <= 0:
        return None
    return mean_value


def risk_score_absolute(
    candles: pd.DataFrame,
    window: int,
    vol_cap: float,
) -> float | None:
    """Score trailing volatility on a fixed ``0..100`` curve.

    The score is ``100 * clamp(1 - sigma / vol_cap, 0, 1)`` where ``sigma`` is
    the population standard deviation of daily log returns over the trailing
    window. Too little usable history means the risk component is missing, not
    neutral.
    """
    window_size = _positive_int(window)
    cap = _finite_float(vol_cap)
    if candles is None or candles.empty or window_size is None or window_size < 2:
        return None
    if cap is None or cap <= 0:
        return None

    try:
        frame = prepare_ohlc(candles)
    except ValueError:
        return None
    if frame.empty or len(frame) < window_size:
        return None

    # Log returns are scale-independent: a 2% move has the same risk meaning for
    # a Rs. 100 stock and a Rs. 1000 stock. Non-positive closes cannot be logged,
    # so they invalidate the component instead of being coerced.
    close = pd.to_numeric(frame["close"].tail(window_size), errors="coerce")
    close = close.where(np.isfinite(close), np.nan).dropna()
    if len(close) < window_size or (close <= 0).any():
        return None

    returns = np.log(close / close.shift(1)).dropna()
    if returns.empty:
        return None
    sigma = float(returns.std(ddof=0))
    if not math.isfinite(sigma):
        return None
    return max(0.0, min(100.0, 100.0 * (1.0 - sigma / cap)))


def freshness_score_absolute(
    staleness_days: int | None,
    halflife_days: float,
) -> float | None:
    """Score signal freshness using a fixed exponential decay.

    ``staleness_days`` must come from stored run/result dates, never from the
    wall clock. Negative staleness can happen when a malformed future signal date
    slips through; clamp it to zero so the score never exceeds 100.
    """
    halflife = _finite_float(halflife_days)
    if staleness_days is None or halflife is None or halflife <= 0:
        return None
    staleness = max(0, int(staleness_days))
    # Exponential decay gives a predictable reading: at exactly one halflife the
    # freshness component is 50, at two halflives it is 25, and so on.
    return max(0.0, min(100.0, 100.0 * (0.5 ** (staleness / halflife))))


def _finite_float(value: Any) -> float | None:
    """Coerce one untrusted scalar to a finite float or return ``None``."""
    if value is None:
        return None
    numeric = pd.to_numeric(pd.Series([value]), errors="coerce").iloc[0]
    try:
        result = float(numeric)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _positive_int(value: Any) -> int | None:
    """Coerce a window-like value to a positive integer."""
    numeric = _finite_float(value)
    if numeric is None:
        return None
    result = int(numeric)
    return result if result > 0 else None
