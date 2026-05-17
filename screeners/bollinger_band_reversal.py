from __future__ import annotations

"""F&O daily Bollinger Band reversal screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each selected F&O stock.
2. Build Bollinger Bands from normal close prices only.
3. Inspect only the latest valid candle.
4. Shortlist a BUY/SELL when the candle rejects the outer band and closes with
   the required color.
"""

import pandas as pd

from backend.charts import add_bollinger_overlay, candlestick_with_volume
from backend.indicators import bollinger_bands


# This metadata is how the Streamlit app discovers and displays the screener.
# The scanner uses the F&O universe and runs across every mapped symbol on each
# invocation; there is no per-run cap on the number of symbols scanned.
SCREENER = {
    "key": "bollinger_band_reversal",
    "name": "Bollinger Band Reversal",
    "description": "Shortlists F&O stocks with daily Bollinger Band(20, 2) rejection candles.",
    "universe": "fno",
    "timeframe": "daily",
    "lookback_days": 80,
    "default_params": {"period": 20, "std_multiplier": 2.0},
}

# Fixed output columns make the Streamlit result table predictable, including the
# common case where today's scan finds no matching symbols.
RESULT_COLUMNS = [
    "symbol",
    "rating",
    "signal_date",
    "open",
    "high",
    "low",
    "close",
    "bb_middle",
    "bb_upper",
    "bb_lower",
    "reason",
]


def _prepare_candles(candles: pd.DataFrame) -> pd.DataFrame:
    """Clean and sort normal daily candles before applying Bollinger Bands."""
    if candles.empty:
        # Empty data is not an error for the screener. The data loader tracks API
        # failures separately, and this module only decides whether candles signal.
        return candles

    # Work on a copy so cleaning timestamps/numeric columns does not mutate the
    # cached frame owned by the loader.
    frame = candles.copy()
    if "timestamp" in frame.columns:
        # Indicators must read candles oldest-to-newest. If duplicate dates slip
        # in from an API/cache refresh, keep one row per timestamp.
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], errors="coerce")
        frame = frame.sort_values("timestamp").drop_duplicates("timestamp")

    for column in ("open", "high", "low", "close", "volume"):
        if column in frame.columns:
            # Dhan/cache/test data may contain strings. Numeric conversion keeps
            # the later candle-color and band comparisons reliable.
            frame[column] = pd.to_numeric(frame[column], errors="coerce")

    # A candle missing any OHLC value cannot produce a meaningful band rejection,
    # so it is removed before the latest-candle check.
    return frame.dropna(subset=["open", "high", "low", "close"]).reset_index(drop=True)


def _signal_from_history(symbol: str, candles: pd.DataFrame, period: int, std_multiplier: float) -> dict | None:
    """Return one BUY/SELL row for a symbol, or None when there is no signal."""
    frame = _prepare_candles(candles)
    if frame.empty:
        return None

    # This screener uses normal candles only. Unlike the SuperTrend screener, no
    # Heikin Ashi conversion is done before calculating the indicator.
    bands = bollinger_bands(frame["close"], period=period, std_multiplier=std_multiplier)
    frame = pd.concat([frame, bands], axis=1)
    # Bollinger Bands need a full rolling window, so the first period-1 rows have
    # NaN bands. Dropping them leaves only candles where all three bands exist.
    valid = frame.dropna(subset=["bb_middle", "bb_upper", "bb_lower"]).copy()
    if valid.empty:
        return None

    # The strategy is a latest-candle scanner. Historical signals are ignored
    # because the Streamlit table is meant to show today's actionable shortlist.
    latest = valid.iloc[-1]
    open_price = float(latest["open"])
    high_price = float(latest["high"])
    low_price = float(latest["low"])
    close_price = float(latest["close"])
    upper_band = float(latest["bb_upper"])
    lower_band = float(latest["bb_lower"])

    rating = ""
    reason = ""
    # BUY setup: price pierced below the lower band intraday, but buyers pushed
    # the candle to a green close. Green means close is greater than open.
    if low_price < lower_band and close_price > open_price:
        rating = "BUY"
        reason = "Daily candle traded below the lower Bollinger Band and closed green."
    # SELL setup: price pierced above the upper band intraday, but sellers forced
    # a red close. Red means close is less than open.
    elif high_price > upper_band and close_price < open_price:
        rating = "SELL"
        reason = "Daily candle traded above the upper Bollinger Band and closed red."
    else:
        # No fresh reversal signal, so omit the symbol from the shortlist.
        return None

    return {
        "symbol": symbol,
        "rating": rating,
        "signal_date": latest.get("timestamp", ""),
        "open": open_price,
        "high": high_price,
        "low": low_price,
        "close": close_price,
        "bb_middle": float(latest["bb_middle"]),
        "bb_upper": upper_band,
        "bb_lower": lower_band,
        "reason": reason,
    }


def run(universe_df, data_loader, params) -> pd.DataFrame:
    """
    Scan the configured F&O universe for daily Bollinger reversal candles.

    The scanner looks only at the latest valid Bollinger candle per stock, so
    older signals do not appear in today's shortlist.
    """
    period = int(params.get("period", SCREENER["default_params"]["period"]))
    std_multiplier = float(params.get("std_multiplier", SCREENER["default_params"]["std_multiplier"]))

    # The loader owns API calls and local cache behavior. This screener stays
    # focused on the Bollinger rule and receives already loaded candle frames.
    # The UI passes a `progress_callback` so the user sees per-symbol progress.
    batch = data_loader.load_universe_history(
        universe_df=universe_df,
        start_date=params["start_date"],
        end_date=params["end_date"],
        force_refresh=bool(params.get("force_refresh", False)),
        progress_callback=params.get("progress_callback"),
    )

    rows = []
    for symbol, candles in batch.frames.items():
        # Only BUY/SELL dictionaries are added. Empty/short/no-signal symbols
        # return None and stay out of the result table.
        signal = _signal_from_history(symbol, candles, period=period, std_multiplier=std_multiplier)
        if signal is not None:
            rows.append(signal)

    # The explicit column list preserves order and makes an empty result useful
    # to the UI instead of becoming a DataFrame with no columns.
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def build_chart(candles: pd.DataFrame, params: dict) -> dict:
    """Render a regular candlestick chart with Bollinger Bands overlaid."""
    period = int(params.get("period", SCREENER["default_params"]["period"]))
    std_multiplier = float(params.get("std_multiplier", SCREENER["default_params"]["std_multiplier"]))

    fig = candlestick_with_volume(
        candles,
        title=f"Daily candles + Bollinger Bands({period}, {std_multiplier:g})",
        ha=False,
    )
    add_bollinger_overlay(fig, candles, period=period, std_multiplier=std_multiplier)
    return fig
