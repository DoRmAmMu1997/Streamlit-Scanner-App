"""Hemant Super 45 — 52 Week High/Low (Ceyhun) BUY screener.

Flow in plain English:
1. Fetch daily candles for every Hemant Super 45 stock.
2. Compute the rolling 52-week (252 trading day) low and high.
3. Inside the most recent N trading days (default 10), find the day where
   the closing price was closest to the rolling 52-week low.
4. Shortlist the stock when that closest distance is within `proximity_pct`
   of the rolling 52-week low (default 2%).

The Ceyhun indicator on TradingView highlights stocks that come back to
their yearly extremes. This screener is the BUY half of that idea: stocks
that recently revisited the 52-week low are the watchlist candidates for a
mean-reversion or value-buy thesis.

Beginner note: this is a *shortlist*, not a buy signal on its own. The user
still needs to look at the chart and decide whether the stock is making a
sustainable base or just falling further.
"""

from __future__ import annotations

import pandas as pd

from backend.charts import add_line_overlay, candlestick_with_volume
from backend.scanner_base import BaseScanner


class Week52LowCeyhun(BaseScanner):
    """BUY when close was within `proximity_pct` of the 52-week low in recent days."""

    SCREENER = {
        "key": "week52_low_ceyhun",
        "name": "52 Week High/Low (Ceyhun)",
        "description": (
            "Hemant Super 45 stocks whose closing price came within a small "
            "tolerance (default 2%) of the trailing 52-week low on any of the "
            "last 10 trading days."
        ),
        "universe": "hemant_super_45",
        "timeframe": "daily",
        # 252 trading days for the rolling window + a comfortable buffer so the
        # very first rolling value sits well before today's candles.
        "lookback_days": 300,
        "default_params": {
            # 52 trading weeks ≈ 252 daily candles.
            "window_bars": 252,
            # "Within the last N trading days" — N is exposed as a parameter so
            # users can tighten it for fresher signals or widen it for a slower
            # watchlist refresh.
            "recent_window_bars": 10,
            # 2% tolerance. The user picked "close within tolerance" of the
            # 52-week low. Lower = stricter, higher = looser.
            "proximity_pct": 0.02,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "week52_low",
        "week52_high",
        "proximity_pct_at_signal",
        "days_since_signal",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when the close was near the 52-week low recently."""
        frame = self.prepare_candles(candles)
        window_bars = self.coerce_param(params, "window_bars", int)
        recent_window_bars = self.coerce_param(params, "recent_window_bars", int)
        proximity_pct = self.coerce_param(params, "proximity_pct", float)

        # We need at least one full 52-week window of history. Without it the
        # rolling min/max would be NaN everywhere and the comparison is moot.
        if frame.empty or len(frame) < window_bars:
            return None

        # `rolling(...).min()` returns NaN until `window_bars` candles have
        # accumulated. min_periods is set to window_bars on purpose so we never
        # compare against a half-formed 52-week window.
        rolling_low = frame["low"].rolling(window=window_bars, min_periods=window_bars).min()
        rolling_high = frame["high"].rolling(window=window_bars, min_periods=window_bars).max()

        # Look only at the last `recent_window_bars` candles. The strategy
        # is "did the stock come back to its 52-week low recently?", not a
        # general historical scan.
        recent = frame.tail(recent_window_bars).copy()
        recent_low_window = rolling_low.tail(recent_window_bars)
        recent_high_window = rolling_high.tail(recent_window_bars)

        # If the rolling values are still NaN anywhere in the recent slice, we
        # don't have a clean 52-week reference yet — skip the symbol.
        if recent_low_window.isna().any() or recent_high_window.isna().any():
            return None

        # Per-day proximity: how far the close is above the 52w low, as a
        # fraction. A value of 0 means the close equals the 52w low; negative
        # values mean the close is actually BELOW the rolling 52w low (a fresh
        # new low). Both qualify.
        proximity = (recent["close"].to_numpy() - recent_low_window.to_numpy()) / recent_low_window.to_numpy()

        # We want the day where the close was closest to the 52w low. That's
        # the minimum of `proximity`. If that minimum is within `proximity_pct`,
        # the signal fires for the latest candle (Streamlit shows TODAY, but
        # the proximity_pct_at_signal column points to which day was tightest).
        if len(proximity) == 0:
            return None
        tightest_idx_in_recent = int(proximity.argmin())
        tightest_proximity = float(proximity[tightest_idx_in_recent])

        if tightest_proximity > proximity_pct:
            # The stock did not get close enough to the 52-week low in the
            # recent window. Skip it.
            return None

        # Map the position back into the full frame so we can read its
        # timestamp for the "days_since_signal" diagnostic.
        global_signal_idx = len(frame) - len(recent) + tightest_idx_in_recent
        signal_row = frame.iloc[global_signal_idx]
        latest = frame.iloc[-1]

        days_since_signal = int(len(recent) - 1 - tightest_idx_in_recent)

        # The reason interpolates the actual values so the Streamlit table is
        # self-documenting. A reader can tell at a glance how close to the low
        # the stock got and how many days ago.
        reason = (
            f"Close {float(signal_row['close']):.2f} came within "
            f"{tightest_proximity * 100:.2f}% of the 52-week low "
            f"{float(rolling_low.iloc[global_signal_idx]):.2f} "
            f"({days_since_signal} day(s) ago)."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": latest.get("timestamp", ""),
            "close": float(latest["close"]),
            "week52_low": float(rolling_low.iloc[-1]),
            "week52_high": float(rolling_high.iloc[-1]),
            "proximity_pct_at_signal": tightest_proximity,
            "days_since_signal": days_since_signal,
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Price chart with the current 52-week low and high as horizontal lines."""
        window_bars = self.coerce_param(params, "window_bars", int)

        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(
            frame,
            title=f"Daily candles + 52-week ({window_bars} bar) high/low",
            ha=False,
        )
        if frame.empty or len(frame) < window_bars:
            return spec

        rolling_low = frame["low"].rolling(window=window_bars, min_periods=window_bars).min()
        rolling_high = frame["high"].rolling(window=window_bars, min_periods=window_bars).max()
        add_line_overlay(
            spec, frame["timestamp"], rolling_low,
            name=f"{window_bars}-bar low", color="#ef5350", pane=0,
        )
        add_line_overlay(
            spec, frame["timestamp"], rolling_high,
            name=f"{window_bars}-bar high", color="#26a69a", pane=0,
        )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases (kept for tests that import the module).
# ---------------------------------------------------------------------------

_scanner = Week52LowCeyhun()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
