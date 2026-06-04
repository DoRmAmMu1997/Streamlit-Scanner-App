"""Technical indicator helpers for screeners.

How this module is structured (beginner note):
- Every public indicator is a small "dispatcher". It first tries a fast,
  battle-tested library (`talib` or `pandas_ta`); if that library is not
  installed, or its call fails for any reason, the dispatcher falls back to a
  pure-pandas implementation kept in this same file (named `_<name>_fallback`).
- This means the app works the same whether or not the optional libraries are
  installed — the libraries just make the math faster and more standard.

The libraries are imported inside `try/except` blocks so a missing install
never crashes the app at import time.
"""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd

# Optional accelerated backend #1: TA-Lib (C library + Python bindings).
# Used for the standard indicators: EMA, SMA, Bollinger Bands, Stochastic.
try:
    import talib
except ImportError:  # pragma: no cover - exercised only when TA-Lib is absent
    talib = None

# Optional accelerated backend #2: pandas_ta (pure Python).
# Used for Heikin Ashi and SuperTrend, which TA-Lib does not provide.
try:
    import pandas_ta
except ImportError:  # pragma: no cover - exercised only when pandas_ta is absent
    pandas_ta = None


logger = logging.getLogger(__name__)


def _log_optional_backend_fallback(backend: str, indicator: str) -> None:
    """Explain why an optional indicator library was skipped for one call.

    TA-Lib and pandas_ta are acceleration packages, not required runtime
    dependencies. When one of them raises on unusual input, the app deliberately
    falls back to the pure-pandas implementation below. This tiny helper keeps
    that fallback visible in debug logs without interrupting the scan.
    """
    logger.debug(
        "%s failed while calculating %s; using the pure-pandas fallback.",
        backend,
        indicator,
        exc_info=True,
    )


def _require_columns(frame: pd.DataFrame, required_columns: list[str]) -> None:
    """Raise a clear error if a candle DataFrame is missing required columns."""
    # A missing column would otherwise fail later with a harder-to-understand
    # pandas KeyError. This message tells the screener author exactly what is
    # wrong with the input candle table.
    missing = [column for column in required_columns if column not in frame.columns]
    if missing:
        raise ValueError(f"Input candles are missing required column(s): {', '.join(missing)}")


def prepare_ohlc(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Return a clean OHLC frame sorted from oldest candle to newest candle.

    Screeners can receive data from Dhan, cache, or tests. This helper makes
    sure all indicator functions start with the same predictable shape, and
    is also what `BaseScanner.prepare_candles` calls.
    """
    # If candles is empty we still need it to be a DataFrame the screener
    # can chain `.iloc[-1]` etc. against. Returning a fresh empty frame keeps
    # the boundary tidy.
    if ohlc is None:
        return pd.DataFrame()
    if isinstance(ohlc, pd.DataFrame) and ohlc.empty:
        return pd.DataFrame(columns=list(ohlc.columns))

    _require_columns(ohlc, ["open", "high", "low", "close"])
    # Work on a copy so this helper never changes the caller's original candles.
    # That keeps one screener's indicator preparation from affecting another
    # screener that may reuse the same DataFrame later.
    frame = ohlc.copy()

    if "timestamp" in frame.columns:
        # Sort oldest-to-newest because indicators are sequential: today's value
        # depends on all candles before it. Duplicate timestamps are collapsed so
        # each day contributes only one candle.
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp")

    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            # API/CSV data can arrive as strings. Coercing bad values to NaN and
            # dropping those rows below is safer than letting string math happen.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


# Backwards-compatible alias. Some older modules still import the
# leading-underscore name; new code should use `prepare_ohlc` directly.
_prepare_ohlc = prepare_ohlc


def pivot_lows(low: pd.Series, left: int, right: int) -> pd.Series:
    """Return a boolean Series marking confirmed pivot lows.

    A pivot low is a candle whose `low` is strictly less than:
      - every low in the previous `left` candles, AND
      - every low in the next `right` candles.

    Confirmation requires `right` future candles to exist, so the LAST
    `right` rows of the input are always `False` — that is intentional, it
    prevents acting on a low that tomorrow's bar could invalidate.

    This vectorized implementation runs in O(n) regardless of `left`/`right`,
    replacing the older O(n*left*right) per-candle loop used by the Knoxville
    screener. It is reusable from any divergence-based screener that needs
    pivot detection.
    """
    left = max(1, int(left))
    right = max(1, int(right))
    series = pd.to_numeric(low, errors="coerce")

    # Rolling minimum of the `left` candles BEFORE today. `shift(1)` lines up
    # the window so today's value is compared against its left neighbors only.
    left_min = series.rolling(window=left, min_periods=left).min().shift(1)

    # For the right window we reverse the series, take a rolling min, and
    # reverse back. After `shift(1)` on the reversed view, each row sees the
    # minimum of its `right` future candles in the original ordering.
    reversed_series = series.iloc[::-1]
    right_min = (
        reversed_series.rolling(window=right, min_periods=right).min().shift(1).iloc[::-1]
    )

    # A NaN low cannot be a pivot. A pivot must be strictly less than both
    # the left and the right rolling minimum (so equal lows do not qualify).
    mask = series.notna() & (series < left_min) & (series < right_min)
    return mask.fillna(False).astype(bool)


def pivot_highs(high: pd.Series, left: int, right: int) -> pd.Series:
    """Return a boolean Series marking confirmed pivot highs.

    The mirror image of `pivot_lows`: a pivot high is a candle whose `high` is
    strictly GREATER than every high in the previous `left` candles AND every
    high in the next `right` candles. As with pivot lows, confirmation needs
    `right` future candles, so the last `right` rows are always `False`.

    Used by `major_levels` to find resistance pivots (where price repeatedly
    failed to push higher) alongside the support pivots from `pivot_lows`.
    """
    left = max(1, int(left))
    right = max(1, int(right))
    series = pd.to_numeric(high, errors="coerce")

    # Rolling maximum of the `left` candles BEFORE today.
    left_max = series.rolling(window=left, min_periods=left).max().shift(1)
    # Rolling maximum of the `right` candles AFTER today (reverse-roll trick).
    reversed_series = series.iloc[::-1]
    right_max = (
        reversed_series.rolling(window=right, min_periods=right).max().shift(1).iloc[::-1]
    )

    mask = series.notna() & (series > left_max) & (series > right_max)
    return mask.fillna(False).astype(bool)


def major_levels(
    frame: pd.DataFrame,
    *,
    left: int = 5,
    right: int = 5,
    cluster_pct: float = 2.0,
    min_touches: int = 3,
) -> list[dict[str, float | int | str]]:
    """Return major support/resistance levels clustered from confirmed pivots.

    "Major" means *multi-touch over the whole history*: a price zone that the
    market has respected repeatedly across all the candles in `frame` (the app
    feeds ~10 years), not a one-off swing. The steps:

    1. Collect every confirmed pivot low (support pivot) and pivot high
       (resistance pivot) across the entire frame via `pivot_lows`/`pivot_highs`.
    2. Sort those pivot prices and walk them in ascending order, grouping
       consecutive prices that sit within `cluster_pct` percent of the running
       cluster's first price into one cluster (a price "zone").
    3. Keep only clusters touched at least `min_touches` times. Each surviving
       cluster becomes one level whose `price` is the mean of its pivots and
       whose `kind` is "support", "resistance", or "both" depending on which
       pivot types fell into it.

    Returns a list of `{"price": float, "touches": int, "kind": str}` sorted by
    price ascending. Empty when the frame is too short to confirm pivots.

    The result feeds two consumers: the Technical Analysis screener's cheap gate
    (is the latest close near a support, or breaking above a resistance?) and
    the LLM agent's numeric context (so it reasons from real levels, not the raw
    candle dump alone).
    """
    if frame is None or frame.empty or "low" not in frame or "high" not in frame:
        return []

    work = frame.reset_index(drop=True)
    low_mask = pivot_lows(work["low"], left=left, right=right)
    high_mask = pivot_highs(work["high"], left=left, right=right)

    # Each pivot contributes one (price, kind) "touch". Supports come from pivot
    # lows, resistances from pivot highs.
    pivots: list[tuple[float, str]] = []
    for price in work.loc[low_mask, "low"]:
        if pd.notna(price):
            pivots.append((float(price), "support"))
    for price in work.loc[high_mask, "high"]:
        if pd.notna(price):
            pivots.append((float(price), "resistance"))
    if not pivots:
        return []

    pivots.sort(key=lambda item: item[0])
    fraction = max(0.0, float(cluster_pct) / 100.0)
    min_touches = max(1, int(min_touches))

    levels: list[dict[str, float | int | str]] = []
    # Greedy left-to-right clustering: a pivot joins the current cluster while it
    # stays within `cluster_pct` of the cluster's anchor (its lowest price);
    # otherwise it starts a new cluster.
    cluster_prices: list[float] = [pivots[0][0]]
    cluster_kinds: set[str] = {pivots[0][1]}
    anchor = pivots[0][0]

    def _flush() -> None:
        if len(cluster_prices) < min_touches:
            return
        kind = (
            "both"
            if {"support", "resistance"}.issubset(cluster_kinds)
            else next(iter(cluster_kinds))
        )
        levels.append(
            {
                "price": sum(cluster_prices) / len(cluster_prices),
                "touches": len(cluster_prices),
                "kind": kind,
            }
        )

    for price, kind in pivots[1:]:
        if anchor > 0 and (price - anchor) / anchor <= fraction:
            cluster_prices.append(price)
            cluster_kinds.add(kind)
        else:
            _flush()
            cluster_prices = [price]
            cluster_kinds = {kind}
            anchor = price
    _flush()

    return levels


# ---------------------------------------------------------------------------
# Level relevance scoring + weekly (higher-timeframe) resampling
# ---------------------------------------------------------------------------


# Relative weights for the five relevance components. They sum to 1.0 so the
# final `relevance` score always lands in [0, 1] (1 = maximally relevant).
# Proximity is weighted highest because a level you can act on *now* matters more
# than an old one far from price; touches and recency come next.
_RELEVANCE_WEIGHTS = {
    "touches": 0.25,
    "recency": 0.25,
    "proximity": 0.30,
    "volume": 0.10,
    "reaction": 0.10,
}


def rank_levels(
    frame: pd.DataFrame,
    levels: list[dict],
    *,
    band_pct: float = 1.0,
    recency_halflife_bars: int = 120,
    reaction_bars: int = 5,
) -> list[dict]:
    """Score each support/resistance level by how *relevant* it is right now.

    Why this exists (beginner note)
    -------------------------------
    `major_levels` finds price zones the market has respected, but it ranks them
    only by raw touch count. A level touched 6 times back in 2015 and never since
    is far less *relevant* today than a 3-touch level price is sitting on this
    week. This function answers the user's question — "which S/R is relevant and
    which is not" — by blending five intuitive signals into one 0..1 `relevance`
    score:

    1. **touches**   — more touches = stronger zone (relative to the busiest level).
    2. **recency**   — how long since price last visited the level (exponential
       decay: a level last tested `recency_halflife_bars` candles ago scores 0.5).
    3. **proximity** — how close the latest close is to the level (actionability).
    4. **volume**    — average volume on the touch bars vs the whole window (did
       real participation happen there?). Neutral 0.5 when there is no volume data.
    5. **reaction**  — how hard price bounced/rejected away from the level after
       touching it (a level that produced big reactions is "respected").

    Each input level dict is `{"price", "touches", "kind"}` (from `major_levels`).
    The returned dicts are copies with extra fields, sorted by `relevance`
    descending::

        {..., "relevance": float, "components": {...}, "last_touch_bars_ago": int,
         "distance_pct": float, "flipped": bool}

    `flipped` flags a level price has closed on BOTH sides of (a polarity flip,
    which traders treat as significant); `kind="both"` is always flipped.
    """
    if not levels or frame is None or frame.empty:
        return []

    work = frame.reset_index(drop=True)
    highs = pd.to_numeric(work["high"], errors="coerce").to_numpy(dtype="float64")
    lows = pd.to_numeric(work["low"], errors="coerce").to_numpy(dtype="float64")
    closes = pd.to_numeric(work["close"], errors="coerce").to_numpy(dtype="float64")
    n = len(work)
    last_close = float(closes[-1]) if n else 0.0

    has_volume = "volume" in work.columns
    if has_volume:
        volumes = pd.to_numeric(work["volume"], errors="coerce").to_numpy(dtype="float64")
        overall_avg_volume = float(np.nanmean(volumes)) if n else 0.0
    else:
        volumes = None
        overall_avg_volume = 0.0

    # Touch count is scored RELATIVE to the busiest level in this set.
    max_touches = max((int(lvl.get("touches", 0)) for lvl in levels), default=1) or 1
    halflife = max(1, int(recency_halflife_bars))

    scored: list[dict] = []
    for lvl in levels:
        price = float(lvl["price"])
        if price <= 0:
            continue
        # A candle "touches" the level when its high-low range crosses a thin band
        # of +/- band_pct around the level price.
        band = price * float(band_pct) / 100.0
        lo_band, hi_band = price - band, price + band
        touch_mask = (lows <= hi_band) & (highs >= lo_band)
        touch_positions = np.where(touch_mask)[0]

        # --- recency: bars since the most recent touch, exponentially decayed ---
        if touch_positions.size:
            last_touch_bars_ago = int(n - 1 - touch_positions[-1])
        else:
            last_touch_bars_ago = n  # never revisited inside the window
        recency_score = 0.5 ** (last_touch_bars_ago / halflife)

        # --- touches: relative to the busiest level ---
        touch_score = min(1.0, int(lvl.get("touches", 0)) / max_touches)

        # --- proximity: 1/(1+d/2.5) → exactly at level = 1.0, ~2.5% away ≈ 0.5 ---
        distance_pct = abs(last_close - price) / last_close * 100.0 if last_close > 0 else 1e9
        proximity_score = 1.0 / (1.0 + distance_pct / 2.5)

        # --- volume on the touch bars vs the whole window ---
        volume_score = 0.5  # neutral default when we cannot measure it
        if volumes is not None and touch_positions.size and overall_avg_volume > 0:
            touch_avg_volume = float(np.nanmean(volumes[touch_positions]))
            if np.isfinite(touch_avg_volume):
                volume_score = min(1.0, touch_avg_volume / overall_avg_volume)

        # --- reaction: average favourable move after each touch ---
        reaction_score = _level_reaction_score(
            str(lvl.get("kind", "support")), price, highs, lows, touch_positions, reaction_bars
        )

        # --- flipped: has price closed on both sides of the level? ---
        closed_above = bool((closes > hi_band).any())
        closed_below = bool((closes < lo_band).any())
        flipped = str(lvl.get("kind")) == "both" or (closed_above and closed_below)

        components = {
            "touches": round(touch_score, 3),
            "recency": round(recency_score, 3),
            "proximity": round(proximity_score, 3),
            "volume": round(volume_score, 3),
            "reaction": round(reaction_score, 3),
        }
        relevance = sum(_RELEVANCE_WEIGHTS[name] * value for name, value in components.items())

        enriched = dict(lvl)
        enriched.update(
            {
                "relevance": round(float(relevance), 3),
                "components": components,
                "last_touch_bars_ago": last_touch_bars_ago,
                "distance_pct": round(float(distance_pct), 2),
                "flipped": flipped,
            }
        )
        scored.append(enriched)

    scored.sort(key=lambda d: d["relevance"], reverse=True)
    return scored


def _level_reaction_score(
    kind: str,
    price: float,
    highs: np.ndarray,
    lows: np.ndarray,
    touch_positions: np.ndarray,
    reaction_bars: int,
) -> float:
    """Average normalized bounce/rejection after a level's touches (0..1).

    For a support we reward upward reactions (price rallying away); for a
    resistance, downward reactions; for "both" we credit whichever was stronger.
    A ~5% average reaction maps to 1.0 (capped) so the score stays in [0, 1].
    """
    if not touch_positions.size or price <= 0:
        return 0.0
    n = len(highs)
    window = max(1, int(reaction_bars))
    reactions: list[float] = []
    for pos in touch_positions:
        end = min(n, int(pos) + 1 + window)
        if end <= int(pos) + 1:
            continue  # the touch was on the very last bar; no "after" to measure
        future_high = float(np.nanmax(highs[int(pos) + 1 : end]))
        future_low = float(np.nanmin(lows[int(pos) + 1 : end]))
        up_move = (future_high - price) / price * 100.0
        down_move = (price - future_low) / price * 100.0
        if kind == "support":
            reactions.append(max(0.0, up_move))
        elif kind == "resistance":
            reactions.append(max(0.0, down_move))
        else:  # "both" — credit whichever reaction was stronger
            reactions.append(max(0.0, up_move, down_move))
    if not reactions:
        return 0.0
    avg_reaction_pct = float(np.mean(reactions))
    return min(1.0, avg_reaction_pct / 5.0)


def resample_to_weekly(frame: pd.DataFrame) -> pd.DataFrame:
    """Aggregate a daily OHLC(V) frame into weekly candles (week ending Friday).

    Why (beginner note)
    -------------------
    The app only stores *daily* candles, but a higher-timeframe (weekly) view is
    invaluable context: a daily bounce *with* the weekly trend is far stronger
    than one against it. Rather than fetch new data, we build the weekly series by
    aggregating the daily candles we already have:

    - open   = first daily open of the week
    - high   = max daily high
    - low    = min daily low
    - close  = last daily close
    - volume = sum of daily volume (when present)

    Weeks are labelled by their Friday (``W-FRI``), the equity-market convention.
    Returns a fresh frame with a ``timestamp`` column (oldest first), or an empty
    frame when there are no dated candles to aggregate.
    """
    if frame is None or frame.empty or "timestamp" not in frame.columns:
        # Without dates we cannot bucket candles into weeks. Return an empty frame
        # with the standard columns so callers can chain `.iloc[-1]` etc. safely.
        return pd.DataFrame(columns=["timestamp", "open", "high", "low", "close", "volume"])

    work = frame.copy()
    work["timestamp"] = pd.to_datetime(work["timestamp"], errors="coerce")
    work = work.dropna(subset=["timestamp"]).sort_values("timestamp").set_index("timestamp")

    aggregation = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in work.columns:
        aggregation["volume"] = "sum"

    weekly = work.resample("W-FRI").agg(aggregation)
    # Drop weeks that had no trading days (holidays) — they resample to all-NaN.
    weekly = weekly.dropna(subset=["open", "high", "low", "close"])
    return weekly.reset_index()


# ---------------------------------------------------------------------------
# Moving averages (EMA / SMA): TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def ema(series: pd.Series, period: int) -> pd.Series:
    """Exponential moving average: recent candles get more weight.

    Uses `talib.EMA` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            values = talib.EMA(np.asarray(series, dtype="float64"), timeperiod=int(period))
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            # Any library hiccup (bad dtype, etc.) drops to the pandas fallback.
            _log_optional_backend_fallback("TA-Lib", "EMA")
    return _ema_fallback(series, period)


def _ema_fallback(series: pd.Series, period: int) -> pd.Series:
    """Pure-pandas EMA used when TA-Lib is unavailable."""
    return series.ewm(span=int(period), adjust=False, min_periods=int(period)).mean()


def sma(series: pd.Series, period: int) -> pd.Series:
    """Simple moving average: every candle in the window gets equal weight.

    Uses `talib.SMA` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            values = talib.SMA(np.asarray(series, dtype="float64"), timeperiod=int(period))
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            _log_optional_backend_fallback("TA-Lib", "SMA")
    return _sma_fallback(series, period)


def _sma_fallback(series: pd.Series, period: int) -> pd.Series:
    """Pure-pandas SMA used when TA-Lib is unavailable."""
    return series.rolling(window=int(period), min_periods=int(period)).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Relative Strength Index, a momentum oscillator between 0 and 100.

    Uses `talib.RSI` when TA-Lib is installed, otherwise a pandas fallback.
    """
    period = max(1, int(period))
    if talib is not None:
        try:
            # TA-Lib is the standard library implementation. Convert to a
            # float64 numpy array because TA-Lib expects plain numeric arrays,
            # not pandas objects or strings from CSV/API data.
            values = talib.RSI(np.asarray(series, dtype="float64"), timeperiod=period)
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            # A bad dtype or unexpected library issue should not break the app;
            # the pandas fallback below keeps the screener usable.
            _log_optional_backend_fallback("TA-Lib", "RSI")
    return _rsi_fallback(series, period)


def _rsi_fallback(series: pd.Series, period: int = 14) -> pd.Series:
    """Pure-pandas RSI used when TA-Lib is unavailable."""
    period = max(1, int(period))
    # `diff()` tells us how much the close changed from the previous candle.
    close_numeric = pd.to_numeric(series, errors="coerce")
    delta = close_numeric.diff()
    # Positive changes are gains; negative changes are losses. The clipping
    # keeps each side separate so average gain and average loss are independent.
    gains = delta.clip(lower=0)
    losses = -delta.clip(upper=0)
    # Wilder-style RSI uses smoothed averages. ewm(alpha=1/period) gives that
    # smooth rolling behavior without needing a manual loop.
    average_gain = gains.ewm(alpha=1 / int(period), adjust=False, min_periods=int(period)).mean()
    average_loss = losses.ewm(alpha=1 / int(period), adjust=False, min_periods=int(period)).mean()
    relative_strength = average_gain / average_loss.replace(0, pd.NA)
    return 100 - (100 / (1 + relative_strength))


def momentum(series: pd.Series, period: int = 20) -> pd.Series:
    """Momentum oscillator: today's close minus the close `period` bars ago.

    Knoxville Divergence uses this to ask whether downside momentum is weakening
    while price is still making a lower low.
    """
    period = max(1, int(period))
    if talib is not None:
        try:
            # TA-Lib MOM is simply close[today] - close[N bars ago]. We wrap it
            # so screeners can use one stable helper regardless of whether
            # TA-Lib is installed on the user's machine.
            values = talib.MOM(np.asarray(series, dtype="float64"), timeperiod=period)
            return pd.Series(values, index=series.index, name=getattr(series, "name", None))
        except Exception:
            _log_optional_backend_fallback("TA-Lib", "Momentum")
    return _momentum_fallback(series, period)


def _momentum_fallback(series: pd.Series, period: int = 20) -> pd.Series:
    """Pure-pandas Momentum used when TA-Lib is unavailable."""
    period = max(1, int(period))
    # The first `period` rows are NaN because there is no candle far enough back
    # to compare against yet. That warm-up behavior matches TA-Lib's MOM output.
    return pd.to_numeric(series, errors="coerce").diff(periods=period)


def volume_average(volume: pd.Series, period: int = 20) -> pd.Series:
    """Rolling average volume, useful for volume spike/liquidity filters."""
    return volume.rolling(window=int(period), min_periods=int(period)).mean()


# ---------------------------------------------------------------------------
# Stochastic oscillator: TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def stochastic(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int = 5,
    k_smoothing: int = 4,
    d_smoothing: int = 3,
) -> pd.DataFrame:
    """
    Slow Stochastic oscillator.

    Beginner note:
    - %K measures where the close sits inside the recent high-low range.
    - %D is a smoothed (averaged) version of %K, used as the signal line.
    Returns a DataFrame with two columns: `stoch_k` and `stoch_d`, both 0-100.

    Uses `talib.STOCH` when TA-Lib is installed, otherwise a pandas fallback.
    """
    if talib is not None:
        try:
            stoch_k, stoch_d = talib.STOCH(
                np.asarray(high, dtype="float64"),
                np.asarray(low, dtype="float64"),
                np.asarray(close, dtype="float64"),
                fastk_period=int(k_period),
                slowk_period=int(k_smoothing),
                slowk_matype=0,  # 0 = simple moving average smoothing
                slowd_period=int(d_smoothing),
                slowd_matype=0,
            )
            return pd.DataFrame(
                {"stoch_k": stoch_k, "stoch_d": stoch_d},
                index=close.index,
            )
        except Exception:
            _log_optional_backend_fallback("TA-Lib", "Stochastic")
    return _stochastic_fallback(high, low, close, k_period, k_smoothing, d_smoothing)


def _stochastic_fallback(
    high: pd.Series,
    low: pd.Series,
    close: pd.Series,
    k_period: int,
    k_smoothing: int,
    d_smoothing: int,
) -> pd.DataFrame:
    """Pure-pandas Stochastic used when TA-Lib is unavailable.

    Step 1: Fast %K = where the close sits in the recent high-low range.
    Step 2: Slow %K = smoothed Fast %K.
    Step 3: Slow %D = smoothed Slow %K.
    """
    k_period = max(1, int(k_period))
    k_smoothing = max(1, int(k_smoothing))
    d_smoothing = max(1, int(d_smoothing))

    lowest_low = low.rolling(window=k_period, min_periods=k_period).min()
    highest_high = high.rolling(window=k_period, min_periods=k_period).max()
    # Replacing a zero range with NaN avoids a divide-by-zero on flat candles.
    range_size = (highest_high - lowest_low).replace(0, np.nan)
    fast_k = 100.0 * (close - lowest_low) / range_size
    slow_k = fast_k.rolling(window=k_smoothing, min_periods=k_smoothing).mean()
    slow_d = slow_k.rolling(window=d_smoothing, min_periods=d_smoothing).mean()
    return pd.DataFrame({"stoch_k": slow_k, "stoch_d": slow_d}, index=close.index)


# ---------------------------------------------------------------------------
# Bollinger Bands: TA-Lib primary, pandas fallback
# ---------------------------------------------------------------------------


def bollinger_bands(close: pd.Series, period: int = 20, std_multiplier: float = 2.0) -> pd.DataFrame:
    """
    Bollinger Bands: a moving-average middle band with volatility bands above
    and below it.

    Returns a DataFrame with `bb_middle`, `bb_upper`, `bb_lower`.
    Uses `talib.BBANDS` when TA-Lib is installed, otherwise a pandas fallback.
    Both paths use population standard deviation (ddof=0).
    """
    if talib is not None:
        try:
            upper, middle, lower = talib.BBANDS(
                np.asarray(close, dtype="float64"),
                timeperiod=max(1, int(period)),
                nbdevup=float(std_multiplier),
                nbdevdn=float(std_multiplier),
                matype=0,  # 0 = simple moving average for the middle band
            )
            return pd.DataFrame(
                {"bb_middle": middle, "bb_upper": upper, "bb_lower": lower},
                index=close.index,
            )
        except Exception:
            _log_optional_backend_fallback("TA-Lib", "Bollinger Bands")
    return _bollinger_bands_fallback(close, period, std_multiplier)


def _bollinger_bands_fallback(
    close: pd.Series, period: int = 20, std_multiplier: float = 2.0
) -> pd.DataFrame:
    """Pure-pandas Bollinger Bands used when TA-Lib is unavailable."""
    period = max(1, int(period))
    multiplier = float(std_multiplier)
    # Convert to numeric because close prices can come from APIs, CSV files, or
    # tests. Invalid values become NaN and naturally produce NaN bands.
    close_numeric = pd.to_numeric(close, errors="coerce")
    # `min_periods=period` means the first period-1 rows are warm-up rows. A
    # screener should wait for a complete window before reading a Bollinger Band.
    middle = close_numeric.rolling(window=period, min_periods=period).mean()
    # Population standard deviation (ddof=0) is intentional. Pandas defaults to
    # sample standard deviation, which would give slightly wider bands.
    rolling_std = close_numeric.rolling(window=period, min_periods=period).std(ddof=0)
    return pd.DataFrame(
        {
            "bb_middle": middle,
            "bb_upper": middle + multiplier * rolling_std,
            "bb_lower": middle - multiplier * rolling_std,
        },
        index=close.index,
    )


# ---------------------------------------------------------------------------
# Envelope bands: moving-average basis with fixed-percent bands
# ---------------------------------------------------------------------------


def envelope(
    close: pd.Series,
    period: int = 200,
    percent: float = 14.0,
    exponential: bool = True,
) -> pd.DataFrame:
    """
    Envelope bands: a moving-average basis with fixed-percent bands around it.

    Mirrors the TradingView "Envelope" indicator:
        basis = EMA(close, period)   (or SMA when exponential is False)
        upper = basis * (1 + percent / 100)
        lower = basis * (1 - percent / 100)

    Returns a DataFrame with `env_basis`, `env_upper`, `env_lower`.

    The moving-average basis is delegated to the library-backed `ema` / `sma`
    helpers, which route through TA-Lib when it is installed (pure-pandas
    fallback otherwise). TA-Lib / pandas_ta have no standalone "envelope"
    indicator, so only the fixed ±percent offset is computed here.
    """
    basis = ema(close, period) if exponential else sma(close, period)
    fraction = float(percent) / 100.0
    return pd.DataFrame(
        {
            "env_basis": basis,
            "env_upper": basis * (1.0 + fraction),
            "env_lower": basis * (1.0 - fraction),
        },
        index=close.index,
    )


# ---------------------------------------------------------------------------
# Knoxville Divergence (bullish): pivots + RSI/Momentum disagreement
# ---------------------------------------------------------------------------


def bullish_knoxville_divergence(
    frame: pd.DataFrame,
    *,
    rsi_period: int = 14,
    momentum_period: int = 20,
    bars_back: int = 20,
    recency: int = 10,
    pivot_left: int = 2,
    pivot_right: int = 2,
    oversold: float = 30.0,
) -> pd.Series | None:
    """
    Return the most recent confirmed *bullish* Knoxville Divergence bar, or None.

    A bullish Knoxville Divergence is the classic "selling pressure is fading"
    setup: price prints a LOWER pivot low while a momentum oscillator prints a
    HIGHER low, with RSI in oversold territory at the latest pivot.

    Parameter names follow the Rob Booker Knoxville indicator: `bars_back` is
    how far back to look for the earlier pivot to compare against ("Bars Back"),
    and `rsi_period` is the RSI length. RSI and Momentum come from the
    TA-Lib-backed `rsi` / `momentum` helpers; confirmed pivots come from
    `pivot_lows` (which has no TA-Lib / pandas_ta equivalent).

    The returned pivot row carries `rsi`, `momentum`, and `timestamp` (when
    present) so callers can report the divergence date and oscillator readings.
    Pass a frame whose rows are ordered oldest→newest (e.g. the output of
    `prepare_ohlc`); the index is reset internally so positional lookbacks work.
    """
    if frame is None or frame.empty:
        return None

    # Reset to a 0..n-1 index so `latest_index - bars_back` and `.loc[...]`
    # behave positionally regardless of the caller's index.
    enriched = frame.reset_index(drop=True).copy()
    enriched["rsi"] = rsi(enriched["close"], period=rsi_period)
    enriched["momentum"] = momentum(enriched["close"], period=momentum_period)

    # `pivot_lows` returns True on confirmed pivot rows and False on the last
    # `pivot_right` candles (no future bars to confirm against yet).
    pivot_mask = pivot_lows(enriched["low"], left=pivot_left, right=pivot_right)
    pivot_rows = enriched.loc[pivot_mask].dropna(subset=["low", "rsi", "momentum"])
    if len(pivot_rows) < 2:
        return None

    # Use the most recent confirmed pivot low, not the latest candle. Pivot
    # detection needs `pivot_right` future bars, so a valid divergence can
    # naturally be a few candles old by the time the scanner runs.
    latest_index = int(pivot_rows.index[-1])
    if (len(enriched) - 1 - latest_index) > int(recency):
        return None

    latest = enriched.loc[latest_index]
    if float(latest["rsi"]) > float(oversold):
        return None

    # Look back up to `bars_back` for an earlier pivot where price made a higher
    # low than today but momentum made a lower low — the bullish disagreement.
    earliest_index = max(0, latest_index - int(bars_back))
    prior_pivots = pivot_rows.loc[
        (pivot_rows.index >= earliest_index) & (pivot_rows.index < latest_index)
    ]
    # Iterate from the most recent prior pivot backward; the first match is the
    # most relevant (closest-in-time) divergence pair.
    for prior_index in reversed(prior_pivots.index.tolist()):
        prior = enriched.loc[prior_index]
        price_made_lower_low = float(latest["low"]) < float(prior["low"])
        momentum_made_higher_low = float(latest["momentum"]) > float(prior["momentum"])
        if price_made_lower_low and momentum_made_higher_low:
            return latest
    return None


def bullish_knoxville_divergences(
    frame: pd.DataFrame,
    *,
    rsi_period: int = 14,
    momentum_period: int = 20,
    bars_back: int = 20,
    pivot_left: int = 2,
    pivot_right: int = 2,
    oversold: float = 30.0,
) -> list[pd.Series]:
    """Return every confirmed bullish Knoxville Divergence pivot, oldest first.

    A bullish Knoxville Divergence is checked on pivot lows, not every candle:
    price must make a lower pivot low while momentum makes a higher pivot low,
    and the latest pivot's RSI must be oversold. Returning all matches is useful
    for charts and for "old divergence retest" rules, while the older
    `bullish_knoxville_divergence(...)` wrapper still answers the narrower
    question: "is there a recent qualifying divergence?"
    """
    if frame is None or frame.empty:
        return []

    enriched = frame.reset_index(drop=True).copy()
    enriched["rsi"] = rsi(enriched["close"], period=rsi_period)
    enriched["momentum"] = momentum(enriched["close"], period=momentum_period)

    pivot_mask = pivot_lows(enriched["low"], left=pivot_left, right=pivot_right)
    pivot_rows = enriched.loc[pivot_mask].dropna(subset=["low", "rsi", "momentum"])
    if len(pivot_rows) < 2:
        return []

    matches: list[pd.Series] = []
    bars_back = max(1, int(bars_back))
    for latest_index in pivot_rows.index[1:]:
        latest_index = int(latest_index)
        latest = enriched.loc[latest_index]
        if float(latest["rsi"]) > float(oversold):
            continue

        earliest_index = max(0, latest_index - bars_back)
        prior_pivots = pivot_rows.loc[
            (pivot_rows.index >= earliest_index) & (pivot_rows.index < latest_index)
        ]
        # Iterate backward so the stored comparison pair is the nearest prior
        # pivot, matching the legacy single-divergence function's choice.
        for prior_index in reversed(prior_pivots.index.tolist()):
            prior = enriched.loc[int(prior_index)]
            price_made_lower_low = float(latest["low"]) < float(prior["low"])
            momentum_made_higher_low = float(latest["momentum"]) > float(prior["momentum"])
            if price_made_lower_low and momentum_made_higher_low:
                enriched_latest = latest.copy()
                # Store the comparison pivot too. The screener currently displays
                # the latest pivot low, but these fields make future explanations
                # and chart annotations possible without recomputing the pair.
                enriched_latest["prior_pivot_timestamp"] = prior.get("timestamp", "")
                enriched_latest["prior_pivot_low"] = float(prior["low"])
                enriched_latest["prior_pivot_momentum"] = float(prior["momentum"])
                matches.append(enriched_latest)
                break

    return matches


# ---------------------------------------------------------------------------
# Heikin Ashi candles: pandas_ta primary, pandas fallback
# ---------------------------------------------------------------------------


def build_heikin_ashi(ohlc: pd.DataFrame) -> pd.DataFrame:
    """
    Convert normal candles into Heikin Ashi candles.

    Beginner note:
    Heikin Ashi candles are built from normal OHLC candles, but they smooth the
    open/close values so trends are visually easier to read.

    Returns the cleaned OHLC frame plus four extra columns: `ha_open`,
    `ha_high`, `ha_low`, `ha_close`. Uses `pandas_ta.ha` when pandas_ta is
    installed, otherwise a pure-pandas fallback.
    """
    if pandas_ta is not None:
        try:
            return _build_heikin_ashi_pandas_ta(ohlc)
        except Exception:
            _log_optional_backend_fallback("pandas_ta", "Heikin Ashi")
    return _build_heikin_ashi_fallback(ohlc)


def _build_heikin_ashi_pandas_ta(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Heikin Ashi via pandas_ta, normalized to this app's `ha_*` column names."""
    frame = _prepare_ohlc(ohlc)
    if frame.empty:
        return frame
    # pandas_ta returns columns named HA_open/HA_high/HA_low/HA_close. We attach
    # them to our frame under lower-case names via `.to_numpy()` so pandas does
    # not try to align on a possibly-different index.
    ha = pandas_ta.ha(frame["open"], frame["high"], frame["low"], frame["close"])
    result = frame.copy()
    result["ha_open"] = ha["HA_open"].to_numpy()
    result["ha_high"] = ha["HA_high"].to_numpy()
    result["ha_low"] = ha["HA_low"].to_numpy()
    result["ha_close"] = ha["HA_close"].to_numpy()
    return result.reset_index(drop=True)


def _build_heikin_ashi_fallback(ohlc: pd.DataFrame) -> pd.DataFrame:
    """Pure-pandas Heikin Ashi used when pandas_ta is unavailable."""
    frame = _prepare_ohlc(ohlc)
    if frame.empty:
        return frame

    result = frame.copy()
    # HA close is the average price of the normal candle. It blends open, high,
    # low, and close into one smoother closing value.
    ha_close = (frame["open"] + frame["high"] + frame["low"] + frame["close"]) / 4.0
    ha_open = [0.0] * len(frame)
    # The first HA open has no previous HA candle, so the standard seed is the
    # midpoint of the first normal candle's open and close.
    ha_open[0] = (float(frame.iloc[0]["open"]) + float(frame.iloc[0]["close"])) / 2.0

    # HA open depends on the previous HA candle, so this part must walk forward
    # one candle at a time.
    for index in range(1, len(frame)):
        ha_open[index] = (ha_open[index - 1] + float(ha_close.iloc[index - 1])) / 2.0

    result["ha_open"] = ha_open
    result["ha_close"] = ha_close
    # HA high/low keep the full candle range visible by taking the highest/lowest
    # value among the normal candle range and the smoothed HA open/close.
    result["ha_high"] = pd.concat([frame["high"], result["ha_open"], result["ha_close"]], axis=1).max(axis=1)
    result["ha_low"] = pd.concat([frame["low"], result["ha_open"], result["ha_close"]], axis=1).min(axis=1)
    return result.reset_index(drop=True)


# ---------------------------------------------------------------------------
# SuperTrend: pandas_ta primary, pandas fallback
# ---------------------------------------------------------------------------


def _true_range(high: pd.Series, low: pd.Series, close: pd.Series) -> pd.Series:
    """
    True Range measures the largest price movement available for each candle.

    It includes gaps from the previous close, which is why ATR uses True Range
    instead of only using high-low.
    """
    previous_close = close.shift(1)
    ranges = pd.concat(
        [
            (high - low).abs(),
            (high - previous_close).abs(),
            (low - previous_close).abs(),
        ],
        axis=1,
    )
    return ranges.max(axis=1)


def _wilder_atr(high: pd.Series, low: pd.Series, close: pd.Series, period: int) -> pd.Series:
    """Average True Range using Wilder-style smoothing."""
    period = max(1, int(period))
    true_range = _true_range(high, low, close)
    # Wilder smoothing is equivalent to an exponential moving average with
    # alpha=1/period. `min_periods=period` leaves early warm-up candles as NaN
    # until there is enough history to trust the ATR.
    return true_range.ewm(alpha=1.0 / period, adjust=False, min_periods=period).mean()


def supertrend(ohlc: pd.DataFrame, atr_period: int = 10, multiplier: float = 2.0) -> pd.DataFrame:
    """
    Calculate SuperTrend on the OHLC columns passed to this function.

    Important for the Heikin Ashi screener:
    if the caller passes HA open/high/low/close renamed to open/high/low/close,
    this function calculates SuperTrend on HA candles, not normal candles.

    Returns the cleaned OHLC frame plus `atr`, `supertrend`,
    `supertrend_direction`, and `supertrend_color`. Uses `pandas_ta.supertrend`
    when pandas_ta is installed, otherwise a pure-pandas fallback.
    """
    if float(multiplier) <= 0:
        raise ValueError("SuperTrend multiplier must be positive.")
    if pandas_ta is not None:
        try:
            return _supertrend_pandas_ta(ohlc, atr_period, multiplier)
        except Exception:
            _log_optional_backend_fallback("pandas_ta", "SuperTrend")
    return _supertrend_fallback(ohlc, atr_period, multiplier)


def _supertrend_pandas_ta(ohlc: pd.DataFrame, atr_period: int, multiplier: float) -> pd.DataFrame:
    """SuperTrend via pandas_ta, normalized to this app's column schema."""
    result = _prepare_ohlc(ohlc)
    if result.empty:
        return result

    st_frame = pandas_ta.supertrend(
        result["high"], result["low"], result["close"],
        length=max(1, int(atr_period)),
        multiplier=float(multiplier),
    )
    if st_frame is None or st_frame.empty:
        raise ValueError("pandas_ta.supertrend returned no data")

    # pandas_ta names columns like `SUPERT_10_2.0` (the line) and
    # `SUPERTd_10_2.0` (the direction). We match by prefix so we do not depend
    # on the exact period/multiplier suffix.
    line_column = next(column for column in st_frame.columns if column.startswith("SUPERT_"))
    direction_column = next(column for column in st_frame.columns if column.startswith("SUPERTd_"))
    direction = st_frame[direction_column].to_numpy()

    # ATR is not part of pandas_ta's supertrend output, so we compute it with
    # the shared helper to keep the column schema identical to the fallback.
    result["atr"] = _wilder_atr(result["high"], result["low"], result["close"], atr_period).to_numpy()
    result["supertrend"] = st_frame[line_column].to_numpy()
    result["supertrend_direction"] = direction
    # A text color label is easier to inspect in Streamlit/tests than raw 1/-1.
    result["supertrend_color"] = np.where(
        direction == 1, "green", np.where(direction == -1, "red", "warmup")
    )
    return result.reset_index(drop=True)


def _supertrend_fallback(ohlc: pd.DataFrame, atr_period: int = 10, multiplier: float = 2.0) -> pd.DataFrame:
    """Pure-pandas SuperTrend used when pandas_ta is unavailable."""
    period = max(1, int(atr_period))
    factor = float(multiplier)

    # The function is candle-type agnostic. Normal candles, Heikin Ashi candles,
    # or any future transformed candle can be used as long as the columns are
    # named open/high/low/close before calling this helper.
    result = _prepare_ohlc(ohlc)
    if result.empty:
        return result

    high = result["high"]
    low = result["low"]
    close = result["close"]
    atr = _wilder_atr(high, low, close, period)

    # SuperTrend starts with two basic bands around the candle midpoint. ATR
    # decides how far the bands sit from price, and the multiplier widens or
    # tightens that distance.
    hl2 = (high + low) / 2.0
    basic_upper = hl2 + factor * atr
    basic_lower = hl2 - factor * atr

    # These arrays are filled row-by-row because SuperTrend is stateful: today's
    # final band and direction depend on yesterday's final band and direction.
    final_upper = np.full(len(result), np.nan, dtype=float)
    final_lower = np.full(len(result), np.nan, dtype=float)
    direction = np.zeros(len(result), dtype=int)
    line = np.full(len(result), np.nan, dtype=float)

    for index in range(len(result)):
        if not np.isfinite(float(atr.iloc[index])):
            # ATR is NaN during warm-up, so the SuperTrend line is also left NaN.
            # Screeners skip these early rows instead of treating them as signals.
            continue

        if index == 0 or not np.isfinite(final_upper[index - 1]) or not np.isfinite(final_lower[index - 1]):
            # The first valid ATR candle seeds both final bands and starts in an
            # uptrend by convention. Later candles can then update that state.
            final_upper[index] = float(basic_upper.iloc[index])
            final_lower[index] = float(basic_lower.iloc[index])
            direction[index] = 1
            line[index] = final_lower[index]
            continue

        previous_close = float(close.iloc[index - 1])
        # A final upper band can move lower in a downtrend, but it should not
        # keep rising against price unless price has already closed above it.
        final_upper[index] = (
            float(basic_upper.iloc[index])
            if float(basic_upper.iloc[index]) < final_upper[index - 1] or previous_close > final_upper[index - 1]
            else final_upper[index - 1]
        )
        # A final lower band can move higher in an uptrend, but it should not
        # keep falling away from price unless price has already closed below it.
        final_lower[index] = (
            float(basic_lower.iloc[index])
            if float(basic_lower.iloc[index]) > final_lower[index - 1] or previous_close < final_lower[index - 1]
            else final_lower[index - 1]
        )

        # Direction flips only when close crosses the opposite final band. The
        # line shown to screeners is the lower band in an uptrend and the upper
        # band in a downtrend.
        if direction[index - 1] == -1 and float(close.iloc[index]) > final_upper[index - 1]:
            direction[index] = 1
        elif direction[index - 1] == 1 and float(close.iloc[index]) < final_lower[index - 1]:
            direction[index] = -1
        else:
            direction[index] = direction[index - 1]

        line[index] = final_lower[index] if direction[index] == 1 else final_upper[index]

    result["atr"] = atr
    result["supertrend"] = line
    result["supertrend_direction"] = direction
    # A text color label is easier to inspect in Streamlit/tests than raw 1/-1
    # direction numbers, while the numeric direction remains available above.
    result["supertrend_color"] = np.where(direction == 1, "green", np.where(direction == -1, "red", "warmup"))
    return result.reset_index(drop=True)
