from __future__ import annotations

"""F&O daily Heikin Ashi SuperTrend crossover screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for every F&O stock selected by the loader.
2. Convert those normal candles into Heikin Ashi candles.
3. Calculate SuperTrend on the Heikin Ashi OHLC values, not the normal OHLC.
4. Shortlist only the latest candle when HA close crosses the SuperTrend line.
"""

import pandas as pd
import plotly.graph_objects as go

from backend.charts import add_supertrend_overlay, candlestick_with_volume
from backend.indicators import build_heikin_ashi, supertrend


# The app discovers this dictionary automatically through `screener_registry.py`.
# Keeping these settings beside the screener logic makes it obvious which
# universe and default parameters this module expects. The scanner always
# processes every mapped row in the configured universe; there is no per-run
# cap on the number of symbols scanned.
SCREENER = {
    "key": "heikin_ashi_supertrend",
    "name": "Heikin Ashi SuperTrend",
    "description": "Shortlists F&O stocks when daily Heikin Ashi close crosses SuperTrend(10, 2).",
    "universe": "fno",
    "timeframe": "daily",
    "lookback_days": 120,
    "default_params": {"atr_period": 10, "multiplier": 2.0},
}

# Returning a fixed set of columns keeps Streamlit stable even when a scan finds
# no matches. An empty DataFrame with these columns can still render/download.
RESULT_COLUMNS = [
    "symbol",
    "rating",
    "signal_date",
    "close",
    "ha_open",
    "ha_high",
    "ha_low",
    "ha_close",
    "supertrend",
    "previous_ha_close",
    "previous_supertrend",
    "reason",
]


def _ha_ohlc_for_supertrend(ha_frame: pd.DataFrame) -> pd.DataFrame:
    """
    Build the OHLC input that SuperTrend should see.

    The user specifically wanted SuperTrend calculated on Heikin Ashi candles,
    so HA open/high/low/close are renamed to the normal OHLC column names before
    calling the shared `supertrend(...)` helper.
    """
    columns = {
        "ha_open": "open",
        "ha_high": "high",
        "ha_low": "low",
        "ha_close": "close",
    }
    # Keep timestamp so the output signal can still point to the exact daily
    # candle that produced the BUY/SELL rating.
    selected = ha_frame[["timestamp", "ha_open", "ha_high", "ha_low", "ha_close"]].copy()
    return selected.rename(columns=columns)


def _signal_from_history(symbol: str, candles: pd.DataFrame, atr_period: int, multiplier: float) -> dict | None:
    """Return one BUY/SELL row for a symbol, or None when there is no signal."""
    if candles.empty:
        # Empty frames usually mean the loader could not fetch usable data for
        # this symbol. The loader records failures separately, so the screener
        # can simply skip this stock.
        return None

    # The strategy rule is based on Heikin Ashi candles, so conversion happens
    # before any indicator calculation.
    ha = build_heikin_ashi(candles)
    if ha.empty:
        return None

    # SuperTrend needs columns named open/high/low/close. We pass HA values under
    # those names so the shared helper calculates the line from smoothed candles.
    st_frame = supertrend(_ha_ohlc_for_supertrend(ha), atr_period=atr_period, multiplier=multiplier)
    # Early SuperTrend rows are NaN while ATR warms up. Dropping them ensures the
    # crossover check compares two fully formed SuperTrend values.
    valid = st_frame.dropna(subset=["supertrend"]).copy()
    if len(valid) < 2:
        # A crossover needs a previous candle and a latest candle. With fewer
        # than two valid SuperTrend rows there is nothing reliable to compare.
        return None

    previous = valid.iloc[-2]
    latest = valid.iloc[-1]
    # `valid` preserves the original integer index, so this points back to the
    # matching row in the Heikin Ashi DataFrame for output fields like ha_open.
    latest_index = int(latest.name)
    latest_ha = ha.iloc[latest_index]

    previous_ha_close = float(previous["close"])
    previous_supertrend = float(previous["supertrend"])
    latest_ha_close = float(latest["close"])
    latest_supertrend = float(latest["supertrend"])

    rating = ""
    reason = ""
    # BUY means the previous valid HA close was at/below the SuperTrend line and
    # the latest HA close finished above it. This is a raw close-vs-line cross,
    # not a separate SuperTrend direction-flip rule.
    if previous_ha_close <= previous_supertrend and latest_ha_close > latest_supertrend:
        rating = "BUY"
        reason = (
            f"Daily Heikin Ashi close crossed and closed above "
            f"SuperTrend({atr_period}, {multiplier:g})."
        )
    # SELL is the mirror image: previous HA close at/above the line, latest HA
    # close below the line.
    elif previous_ha_close >= previous_supertrend and latest_ha_close < latest_supertrend:
        rating = "SELL"
        reason = (
            f"Daily Heikin Ashi close crossed and closed below "
            f"SuperTrend({atr_period}, {multiplier:g})."
        )
    else:
        # The scanner is a shortlist, so stocks without a fresh BUY/SELL signal
        # are intentionally omitted instead of being shown as HOLD.
        return None

    return {
        "symbol": symbol,
        "rating": rating,
        "signal_date": latest_ha.get("timestamp", latest.get("timestamp")),
        "close": float(latest_ha["close"]),
        "ha_open": float(latest_ha["ha_open"]),
        "ha_high": float(latest_ha["ha_high"]),
        "ha_low": float(latest_ha["ha_low"]),
        "ha_close": latest_ha_close,
        "supertrend": latest_supertrend,
        "previous_ha_close": previous_ha_close,
        "previous_supertrend": previous_supertrend,
        "reason": reason,
    }


def run(universe_df, data_loader, params) -> pd.DataFrame:
    """
    Scan the configured F&O universe for daily HA SuperTrend crossovers.

    Data fetching/caching stays inside `data_loader`; this function only applies
    the screener rule to each successfully loaded candle DataFrame.
    """
    atr_period = int(params.get("atr_period", SCREENER["default_params"]["atr_period"]))
    multiplier = float(params.get("multiplier", SCREENER["default_params"]["multiplier"]))

    # Fetching/caching is centralized in the data loader. The screener asks the
    # loader for the full mapped universe and then applies its rule per symbol.
    # `progress_callback` (if present in params) lets the UI render a live bar.
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
        signal = _signal_from_history(symbol, candles, atr_period=atr_period, multiplier=multiplier)
        if signal is not None:
            rows.append(signal)

    # Supplying RESULT_COLUMNS preserves column order and also gives Streamlit a
    # useful empty table shape when no symbols are shortlisted.
    return pd.DataFrame(rows, columns=RESULT_COLUMNS)


def build_chart(candles: pd.DataFrame, params: dict) -> go.Figure:
    """Render a Heikin Ashi chart with the SuperTrend line overlaid.

    The screener decides which candle type the user sees: this strategy is
    Heikin Ashi based, so the chart shows HA candles (not regular OHLC). The
    SuperTrend line is calculated on the HA OHLC for consistency with the
    `run(...)` logic above.
    """
    atr_period = int(params.get("atr_period", SCREENER["default_params"]["atr_period"]))
    multiplier = float(params.get("multiplier", SCREENER["default_params"]["multiplier"]))

    fig = candlestick_with_volume(
        candles,
        title=f"Heikin Ashi candles + SuperTrend({atr_period}, {multiplier:g})",
        ha=True,
    )

    # SuperTrend reads OHLC columns. Convert HA values into normal column names
    # via the local helper so the indicator math is identical to `run(...)`.
    ha = build_heikin_ashi(candles)
    if not ha.empty:
        add_supertrend_overlay(
            fig,
            _ha_ohlc_for_supertrend(ha),
            atr_period=atr_period,
            multiplier=multiplier,
        )
    return fig
