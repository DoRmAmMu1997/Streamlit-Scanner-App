from __future__ import annotations

"""Hemant Super 45 Bollinger + Knoxville BUY screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each mapped Hemant Super 45 stock.
2. Build Bollinger Bands with the long 200-candle / 2.5 standard-deviation
   settings from the strategy brief.
3. Look for a recent bullish Knoxville Divergence: price makes a lower pivot
   low, but momentum makes a higher pivot low while RSI is oversold.
4. Shortlist only BUY candidates whose latest close is at, below, or within a
   small buffer above the lower Bollinger Band.

This is a screener, not a trade manager. It answers "which stocks should I look
at today?" and intentionally returns no SELL/HOLD rows.
"""

import pandas as pd

from backend.charts import add_bollinger_overlay, candlestick_with_volume
from backend.indicators import bollinger_bands, momentum, rsi


SCREENER = {
    "key": "bollinger_knoxville_buy",
    "name": "Bollinger Knoxville Buy",
    "description": (
        "Shortlists Hemant Super 45 stocks near the daily lower Bollinger Band"
        "(200, 2.5) with a recent bullish Knoxville Divergence."
    ),
    "universe": "hemant_super_45",
    "timeframe": "daily",
    # The app prefetches roughly 10 years anyway. This value tells the UI that
    # the screener needs enough candles for BB200 plus divergence lookback.
    "lookback_days": 430,
    # These are strategy defaults, not hardcoded magic inside the functions.
    # Tests can override them with tiny values so we can prove the rule using a
    # compact synthetic candle set instead of hundreds of rows.
    "default_params": {
        "bb_period": 200,
        "bb_std": 2.5,
        "bb_proximity_pct": 0.01,
        "rsi_period": 21,
        "momentum_period": 20,
        "divergence_bars_back": 150,
        "signal_recency_bars": 10,
        "pivot_left": 2,
        "pivot_right": 2,
        "oversold": 30.0,
    },
}


RESULT_COLUMNS = [
    # A fixed schema matters because Streamlit still needs to render an empty
    # result table cleanly when no Hemant Super 45 stock matches today's scan.
    "symbol",
    "rating",
    "signal_date",
    "close",
    "bb_lower",
    "bb_middle",
    "bb_upper",
    "bb_distance_pct",
    "divergence_date",
    "rsi",
    "momentum",
    "reason",
]


def _prepare_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Return sorted, numeric daily candles ready for indicator maths."""
    if candles.empty:
        return candles

    frame = candles.copy()
    if "timestamp" in frame.columns:
        # Indicator values only make sense when the rows are oldest-to-newest.
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp")

    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            # Dhan/cache/test frames may carry numbers as text. Coerce once at
            # the boundary so all comparisons below are numeric.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def _pivot_low_mask(frame: pd.DataFrame, left: int, right: int) -> pd.Series:
    """Mark confirmed pivot lows using `left` bars before and `right` bars after."""
    left = max(1, int(left))
    right = max(1, int(right))
    lows = frame["low"]
    mask = pd.Series(False, index=frame.index)

    # A pivot low is only confirmed after `right` future bars. That keeps the
    # screener from acting on a low that might be invalidated by tomorrow's bar.
    for index in range(left, len(frame) - right):
        current_low = lows.iloc[index]
        previous_lows = lows.iloc[index - left:index]
        next_lows = lows.iloc[index + 1:index + right + 1]
        if (
            pd.notna(current_low)
            and current_low < previous_lows.min()
            and current_low < next_lows.min()
        ):
            mask.iloc[index] = True
    return mask


def _latest_bullish_knoxville(enriched: pd.DataFrame, params: dict) -> pd.Series | None:
    """Return the latest bullish Knoxville Divergence bar, if present."""
    defaults = SCREENER["default_params"]
    left = int(params.get("pivot_left", defaults["pivot_left"]))
    right = int(params.get("pivot_right", defaults["pivot_right"]))
    bars_back = int(params.get("divergence_bars_back", defaults["divergence_bars_back"]))
    recency = int(params.get("signal_recency_bars", defaults["signal_recency_bars"]))
    oversold = float(params.get("oversold", defaults["oversold"]))

    pivot_mask = _pivot_low_mask(enriched, left=left, right=right)
    # Drop warm-up rows where RSI or Momentum is not ready yet. A pivot without
    # both oscillator values cannot prove Knoxville Divergence.
    pivot_rows = enriched.loc[pivot_mask].dropna(subset=["low", "rsi", "momentum"])
    if len(pivot_rows) < 2:
        return None

    # Use the most recent confirmed pivot low, not the latest candle. Pivot
    # detection needs `right` future bars, so a valid divergence can naturally
    # be a few candles old by the time the scanner runs.
    latest_index = int(pivot_rows.index[-1])
    bars_since_latest_pivot = len(enriched) - 1 - latest_index
    if bars_since_latest_pivot > recency:
        return None

    latest = enriched.loc[latest_index]
    if float(latest["rsi"]) > oversold:
        return None

    # Knoxville's bullish idea: price pushes to a lower low, but the momentum
    # oscillator refuses to make a lower low. That disagreement suggests selling
    # pressure may be tiring.
    earliest_index = max(0, latest_index - bars_back)
    prior_pivots = pivot_rows.loc[
        (pivot_rows.index >= earliest_index) & (pivot_rows.index < latest_index)
    ]
    # Compare against prior pivots from newest to oldest. The first match is the
    # closest useful divergence pair and avoids reporting a stale older pair
    # when a more recent one explains the current setup.
    for prior_index in reversed(prior_pivots.index.tolist()):
        prior = enriched.loc[prior_index]
        price_made_lower_low = float(latest["low"]) < float(prior["low"])
        momentum_made_higher_low = float(latest["momentum"]) > float(prior["momentum"])
        if price_made_lower_low and momentum_made_higher_low:
            return latest
    return None


def _enrich(candles: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Add Bollinger Bands, RSI, and Momentum columns used by the rule."""
    defaults = SCREENER["default_params"]
    bb_period = int(params.get("bb_period", defaults["bb_period"]))
    bb_std = float(params.get("bb_std", defaults["bb_std"]))
    rsi_period = int(params.get("rsi_period", defaults["rsi_period"]))
    momentum_period = int(params.get("momentum_period", defaults["momentum_period"]))

    frame = candles.copy()
    bands = bollinger_bands(frame["close"], period=bb_period, std_multiplier=bb_std)
    frame = pd.concat([frame, bands], axis=1)
    frame["rsi"] = rsi(frame["close"], period=rsi_period)
    frame["momentum"] = momentum(frame["close"], period=momentum_period)
    return frame


def _signal_from_history(symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
    """Return one BUY row for a symbol, or None when either filter fails."""
    frame = _prepare_candles(candles)
    defaults = SCREENER["default_params"]
    bb_period = int(params.get("bb_period", defaults["bb_period"]))
    if frame.empty or len(frame) < bb_period:
        return None

    enriched = _enrich(frame, params)
    latest = enriched.iloc[-1]
    needed = latest[["close", "bb_lower", "bb_middle", "bb_upper"]]
    if needed.isna().any():
        return None

    close = float(latest["close"])
    lower_band = float(latest["bb_lower"])
    proximity_pct = float(params.get("bb_proximity_pct", defaults["bb_proximity_pct"]))
    # "Close to or beneath" means the close may be below the lower band, on it,
    # or up to `bb_proximity_pct` above it. With the default 0.01, a lower band
    # at 100 allows closes up to 101.
    if close > lower_band * (1.0 + proximity_pct):
        return None

    # The Bollinger filter says price is stretched down; the Knoxville filter
    # says downside momentum may be weakening. Both are required for a BUY row.
    divergence = _latest_bullish_knoxville(enriched, params)
    if divergence is None:
        return None

    divergence_bar = divergence
    if lower_band == 0:
        bb_distance_pct = 0.0
    else:
        # Positive means close is above the lower band; negative means it closed
        # beneath the lower band. This is easier to compare than raw rupees.
        bb_distance_pct = (close - lower_band) / lower_band

    return {
        "symbol": symbol,
        "rating": "BUY",
        "signal_date": latest.get("timestamp", ""),
        "close": close,
        "bb_lower": lower_band,
        "bb_middle": float(latest["bb_middle"]),
        "bb_upper": float(latest["bb_upper"]),
        "bb_distance_pct": bb_distance_pct,
        "divergence_date": divergence_bar.get("timestamp", ""),
        "rsi": float(divergence_bar["rsi"]),
        "momentum": float(divergence_bar["momentum"]),
        "reason": (
            "Close is at/near the lower Bollinger Band and a recent bullish "
            "Knoxville Divergence shows price making a lower low while momentum "
            "makes a higher low with RSI oversold."
        ),
    }


def run(universe_df, data_loader, params) -> pd.DataFrame:
    """Scan Hemant Super 45 for BUY-only Bollinger + Knoxville setups."""
    # Data fetching and caching stay centralized in DailyDataLoader. The
    # screener receives candle frames and only decides whether each symbol
    # satisfies the strategy rule.
    batch = data_loader.load_universe_history(
        universe_df=universe_df,
        start_date=params["start_date"],
        end_date=params["end_date"],
        force_refresh=bool(params.get("force_refresh", False)),
        progress_callback=params.get("progress_callback"),
    )

    rows = []
    for symbol, candles in batch.frames.items():
        signal = _signal_from_history(symbol, candles, params)
        if signal is not None:
            rows.append(signal)

    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def build_chart(candles: pd.DataFrame, params: dict) -> dict:
    """Render daily candles with the screener's Bollinger Bands overlaid."""
    defaults = SCREENER["default_params"]
    bb_period = int(params.get("bb_period", defaults["bb_period"]))
    bb_std = float(params.get("bb_std", defaults["bb_std"]))

    spec = candlestick_with_volume(
        candles,
        title=f"Daily candles + Bollinger Bands({bb_period}, {bb_std:g})",
        ha=False,
    )
    add_bollinger_overlay(spec, candles, period=bb_period, std_multiplier=bb_std)
    return spec
