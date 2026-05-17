from __future__ import annotations

"""NIFTY 500 daily Stochastic swing-entry screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for every NIFTY 500 stock.
2. Add three indicators to each stock: a 200-period SMA (the long-term trend
   filter), a 5-period EMA (the fast trend line), and the Stochastic oscillator.
3. Shortlist a stock when a fresh swing-trade ENTRY sets up:
   - BUY  (long setup): price is above the 200 SMA, the Stochastic %K just
     crossed above %D out of the oversold zone, and the 5 EMA crossed above the
     200 SMA within the last 7 days.
   - SELL (short setup): the mirror image below the 200 SMA.

This is an entry SCREENER, not a position manager. The original strategy also
has stop / target / oscillator exit rules for trades that are already open;
those manage an existing position and are not part of a "what should I look at
today" scan, so they are intentionally not implemented here. The informational
`stop` and `target` columns still show where the strategy's fixed 3% stop and
5% target would sit for the flagged entry.
"""

import pandas as pd
import plotly.graph_objects as go

from backend.charts import add_line_overlay, add_stochastic_overlay, candlestick_volume_oscillator
from backend.indicators import ema, sma, stochastic


# The app discovers this dictionary automatically through `screener_registry.py`.
# The scanner runs across every mapped symbol in the NIFTY 500 universe.
SCREENER = {
    "key": "stochastic_swing",
    "name": "Stochastic Swing",
    "description": (
        "Shortlists NIFTY 500 stocks with a fresh Stochastic swing entry: "
        "%K crossing %D out of the oversold/overbought zone, in agreement with "
        "the 200 SMA trend and a recent 5 EMA / 200 SMA crossover."
    ),
    "universe": "nifty_500",
    "timeframe": "daily",
    # SMA200 needs 200 prior candles; the app already feeds ~10 years of cached
    # candles, so this value mainly drives the "Lookback" number in the sidebar.
    "lookback_days": 300,
    "default_params": {
        "stoch_k": 5,
        "stoch_k_smoothing": 4,
        "stoch_d_smoothing": 3,
        "ema_period": 5,
        "sma_period": 200,
        "oversold": 20.0,
        "overbought": 80.0,
    },
}

# Fixed risk rules from the original strategy brief. They are not UI knobs, so
# they live here as module constants rather than in `default_params`.
STOP_LOSS_PCT = 0.03
TARGET_PCT = 0.05
# A 5 EMA / 200 SMA crossover only "confirms" an entry while it is this fresh.
MAX_CONFIRMATION_AGE = pd.Timedelta(days=7)

# Returning a fixed set of columns keeps Streamlit stable even when a scan finds
# no matches. An empty DataFrame with these columns can still render/download.
RESULT_COLUMNS = [
    "symbol",
    "rating",
    "signal_date",
    "close",
    "sma200",
    "ema5",
    "stoch_k",
    "stoch_d",
    "previous_stoch_k",
    "previous_stoch_d",
    "stop",
    "target",
    "reason",
]


def _prepare_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Return time-sorted, numeric candles with a clean integer index.

    Indicators are sequential (today depends on every candle before it), so the
    candles must be oldest-to-newest with one row per day before any maths.
    """
    if candles.empty:
        return candles

    frame = candles.copy()
    if "timestamp" in frame.columns:
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp")

    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            # API/cache/test data may contain strings; coerce so the indicator
            # maths never silently operates on text.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    return frame.dropna(subset=["high", "low", "close"]).reset_index(drop=True)


def _crossed_above(left: pd.Series, right: pd.Series) -> pd.Series:
    """True only on the bar where `left` moves from at-or-below to above `right`."""
    return (left > right) & (left.shift(1) <= right.shift(1))


def _crossed_below(left: pd.Series, right: pd.Series) -> pd.Series:
    """True only on the bar where `left` moves from at-or-above to below `right`."""
    return (left < right) & (left.shift(1) >= right.shift(1))


def _recent_cross_confirmation(
    timestamp: pd.Series,
    cross_mask: pd.Series,
    direction_mask: pd.Series,
) -> pd.Series:
    """Mark bars where a qualifying EMA/SMA crossover is still "fresh".

    Steps:
    1. Remember the timestamp of the most recent crossover.
    2. Measure how old that crossover is on each later bar.
    3. Keep the confirmation true only while the trend side still matches and
       the crossover is no older than `MAX_CONFIRMATION_AGE` (7 days).
    """
    last_cross_timestamp = timestamp.where(cross_mask).ffill()
    age = timestamp - last_cross_timestamp
    return (
        direction_mask
        & last_cross_timestamp.notna()
        & age.ge(pd.Timedelta(0))
        & age.le(MAX_CONFIRMATION_AGE)
    )


def _enrich(candles: pd.DataFrame, params: dict) -> pd.DataFrame:
    """Add SMA200, EMA5, Stochastic, and the EMA/SMA confirmation flags.

    The indicators come from `backend.indicators`, which routes through TA-Lib
    when available and falls back to pure-pandas maths otherwise.
    """
    defaults = SCREENER["default_params"]
    sma_period = int(params.get("sma_period", defaults["sma_period"]))
    ema_period = int(params.get("ema_period", defaults["ema_period"]))
    k_period = int(params.get("stoch_k", defaults["stoch_k"]))
    k_smoothing = int(params.get("stoch_k_smoothing", defaults["stoch_k_smoothing"]))
    d_smoothing = int(params.get("stoch_d_smoothing", defaults["stoch_d_smoothing"]))

    frame = candles.copy()
    frame["sma200"] = sma(frame["close"], sma_period)
    frame["ema5"] = ema(frame["close"], ema_period)

    stoch = stochastic(
        frame["high"],
        frame["low"],
        frame["close"],
        k_period=k_period,
        k_smoothing=k_smoothing,
        d_smoothing=d_smoothing,
    )
    frame["stoch_k"] = stoch["stoch_k"]
    frame["stoch_d"] = stoch["stoch_d"]

    # The confirmation rule says the 5 EMA / 200 SMA crossover must be recent
    # AND the EMA must still sit on the correct side of the SMA.
    bullish_ema_cross = _crossed_above(frame["ema5"], frame["sma200"])
    bearish_ema_cross = _crossed_below(frame["ema5"], frame["sma200"])
    frame["bullish_confirmation"] = _recent_cross_confirmation(
        frame["timestamp"], bullish_ema_cross, frame["ema5"] > frame["sma200"]
    )
    frame["bearish_confirmation"] = _recent_cross_confirmation(
        frame["timestamp"], bearish_ema_cross, frame["ema5"] < frame["sma200"]
    )
    return frame


def _signal_from_history(symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
    """Return one BUY/SELL row for a symbol, or None when there is no entry."""
    frame = _prepare_candles(candles)
    defaults = SCREENER["default_params"]
    sma_period = int(params.get("sma_period", defaults["sma_period"]))

    # The strategy needs a real SMA200 value plus one previous candle for the
    # crossover comparison. Too-short histories are skipped, not errors.
    if frame.empty or len(frame) <= sma_period:
        return None

    oversold = float(params.get("oversold", defaults["oversold"]))
    overbought = float(params.get("overbought", defaults["overbought"]))

    enriched = _enrich(frame, params)
    current = enriched.iloc[-1]
    previous = enriched.iloc[-2]

    # If any key value is still NaN (warm-up, missing data) stay flat rather
    # than risk a false signal from a half-formed indicator.
    needed = pd.Series(
        [
            current["close"],
            current["sma200"],
            current["stoch_k"],
            current["stoch_d"],
            previous["stoch_k"],
            previous["stoch_d"],
        ]
    )
    if needed.isna().any():
        return None

    close = float(current["close"])
    sma200 = float(current["sma200"])
    ema5 = float(current["ema5"])
    latest_k = float(current["stoch_k"])
    latest_d = float(current["stoch_d"])
    prev_k = float(previous["stoch_k"])
    prev_d = float(previous["stoch_d"])

    # The Stochastic crossover must happen from the oversold/overbought zone,
    # not from the middle of the 0-100 range.
    bullish_stoch_cross = bool(
        _crossed_above(enriched["stoch_k"], enriched["stoch_d"]).iloc[-1]
        and prev_k < oversold
        and prev_d < oversold
    )
    bearish_stoch_cross = bool(
        _crossed_below(enriched["stoch_k"], enriched["stoch_d"]).iloc[-1]
        and prev_k > overbought
        and prev_d > overbought
    )

    # Full entry filters: trend side (SMA200) + fresh Stochastic cross + a
    # still-fresh EMA/SMA confirmation.
    long_setup = close > sma200 and bullish_stoch_cross and bool(current["bullish_confirmation"])
    short_setup = close < sma200 and bearish_stoch_cross and bool(current["bearish_confirmation"])

    if long_setup:
        rating = "BUY"
        stop = close * (1.0 - STOP_LOSS_PCT)
        target = close * (1.0 + TARGET_PCT)
        reason = (
            "Stochastic %K crossed above %D from oversold; price above SMA200 "
            "with a fresh 5 EMA / 200 SMA bullish crossover."
        )
    elif short_setup:
        rating = "SELL"
        stop = close * (1.0 + STOP_LOSS_PCT)
        target = close * (1.0 - TARGET_PCT)
        reason = (
            "Stochastic %K crossed below %D from overbought; price below SMA200 "
            "with a fresh 5 EMA / 200 SMA bearish crossover."
        )
    else:
        # The scanner is a shortlist, so stocks without a fresh entry are
        # intentionally omitted instead of being shown as HOLD.
        return None

    return {
        "symbol": symbol,
        "rating": rating,
        "signal_date": current.get("timestamp", ""),
        "close": close,
        "sma200": sma200,
        "ema5": ema5,
        "stoch_k": latest_k,
        "stoch_d": latest_d,
        "previous_stoch_k": prev_k,
        "previous_stoch_d": prev_d,
        "stop": stop,
        "target": target,
        "reason": reason,
    }


def run(universe_df, data_loader, params) -> pd.DataFrame:
    """
    Scan the NIFTY 500 universe for fresh Stochastic swing entries.

    Data fetching/caching stays inside `data_loader`; this function only applies
    the entry rule to each successfully loaded candle DataFrame.
    """
    # Fetching/caching is centralized in the data loader. The screener asks for
    # the full mapped universe and applies its rule to each symbol. The optional
    # `progress_callback` lets the Streamlit UI render a live progress bar.
    batch = data_loader.load_universe_history(
        universe_df=universe_df,
        start_date=params["start_date"],
        end_date=params["end_date"],
        force_refresh=bool(params.get("force_refresh", False)),
        progress_callback=params.get("progress_callback"),
    )

    rows = []
    for symbol, candles in batch.frames.items():
        # `_signal_from_history` returns None for empty/short/no-signal symbols,
        # so only actual BUY/SELL candidates are appended to the results table.
        signal = _signal_from_history(symbol, candles, params)
        if signal is not None:
            rows.append(signal)

    # Supplying RESULT_COLUMNS preserves column order and gives Streamlit a
    # useful empty-table shape when no symbols are shortlisted.
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def build_chart(candles: pd.DataFrame, params: dict) -> go.Figure:
    """Render a 3-panel chart: price + SMA200 + EMA5 / volume / Stochastic.

    The Stochastic is an oscillator bounded 0-100, so it gets its own panel
    rather than being overlaid on price. SMA200 and EMA5 are price overlays.
    """
    defaults = SCREENER["default_params"]
    sma_period = int(params.get("sma_period", defaults["sma_period"]))
    ema_period = int(params.get("ema_period", defaults["ema_period"]))
    k_period = int(params.get("stoch_k", defaults["stoch_k"]))
    k_smoothing = int(params.get("stoch_k_smoothing", defaults["stoch_k_smoothing"]))
    d_smoothing = int(params.get("stoch_d_smoothing", defaults["stoch_d_smoothing"]))
    oversold = float(params.get("oversold", defaults["oversold"]))
    overbought = float(params.get("overbought", defaults["overbought"]))

    frame = _prepare_candles(candles)
    fig = candlestick_volume_oscillator(
        frame,
        title=f"Daily candles + SMA{sma_period}/EMA{ema_period} + Stochastic",
        ha=False,
    )
    if frame.empty:
        # `candlestick_volume_oscillator` already returned a placeholder figure.
        return fig

    # Price-panel overlays: the slow trend (SMA200) and the fast line (EMA5).
    add_line_overlay(
        fig, frame["timestamp"], sma(frame["close"], sma_period),
        name=f"SMA {sma_period}", color="#ab47bc", row=1,
    )
    add_line_overlay(
        fig, frame["timestamp"], ema(frame["close"], ema_period),
        name=f"EMA {ema_period}", color="#ffca28", row=1,
    )
    # Oscillator panel: %K, %D, and the 20/80 guide lines.
    add_stochastic_overlay(
        fig, frame,
        k_period=k_period, k_smoothing=k_smoothing, d_smoothing=d_smoothing,
        oversold=oversold, overbought=overbought, row=3,
    )
    return fig
