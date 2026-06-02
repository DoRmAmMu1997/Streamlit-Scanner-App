"""Hemant Super 45 ∪ Good 45 — "20% up with continuous green candles" screener.

Port of Lovevanshi's "20% up with all continuously green candle" TradingView
indicator.

Flow in plain English:
1. Fetch daily candles for every stock in the Hemant Super 45 ∪ Good 45 union.
2. Look at the LATEST candle. If it is green, count how many consecutive green
   candles end on it (this candle plus its green predecessors), capped at
   `max_run` (20).
3. Over that run, measure the move from the lowest low to the highest high:
   `gain = (max(high) - min(low)) / min(low)`.
4. Shortlist the stock when that gain is more than `gain_threshold_pct` (20%).

A "green" candle is `close > open`, or a doji (`close == open`) that opened
above the previous candle's low — matching the original PineScript's
`greenCandle` definition.

The original indicator plots a marker on the qualifying bar; this screener is
the "is the run qualifying *right now*?" shortlist, so it only evaluates the
most recent candle.
"""

from __future__ import annotations

import pandas as pd

from backend.charts import candlestick_with_volume
from backend.scanner_base import BaseScanner


def _green_mask(frame: pd.DataFrame) -> pd.Series:
    """Boolean Series: True where a candle is 'green' per the PineScript rule.

    green = close > open, OR a doji (close == open) whose open is above the
    PREVIOUS candle's low. The first row has no previous low, so a doji there
    is treated as not-green (NaN comparison is False), which is the safe choice.
    """
    open_ = frame["open"]
    close = frame["close"]
    prev_low = frame["low"].shift(1)
    return (close > open_) | ((close == open_) & (open_ > prev_low))


def _latest_green_run(frame: pd.DataFrame, max_run: int) -> dict | None:
    """Describe the consecutive green run ending on the latest candle.

    Returns a dict with `length`, `run_low`, `run_high`, `gain` (fraction), or
    None when the latest candle is not green or the data is unusable.
    """
    if frame.empty:
        return None
    green = _green_mask(frame).to_numpy()
    last = len(green) - 1
    if not bool(green[last]):
        # The run must be in progress on the most recent bar.
        return None

    # Count consecutive greens backward from the latest bar, capped at max_run.
    length = 0
    index = last
    while index >= 0 and length < max_run and bool(green[index]):
        length += 1
        index -= 1

    window = frame.iloc[last - length + 1 : last + 1]
    run_low = float(window["low"].min())
    run_high = float(window["high"].max())
    if run_low <= 0:
        return None
    return {
        "length": length,
        "run_low": run_low,
        "run_high": run_high,
        "gain": (run_high - run_low) / run_low,
    }


class GreenCandles20PctUp(BaseScanner):
    """BUY when a fresh run of green candles has moved >= gain_threshold_pct."""

    SCREENER = {
        "key": "green_candles_20pct_up",
        "name": "20% Up Green Candles (Lovevanshi)",
        "description": (
            "Hemant Super 45 ∪ Good 45 stocks whose latest candle caps a run of "
            "consecutive green candles (up to 20) that moved more than 20% from "
            "the run's lowest low to its highest high."
        ),
        "universe": "hemant_super_good_union",
        "timeframe": "daily",
        # The run is at most `max_run` bars; a small buffer covers the previous
        # low needed by the green-doji rule.
        "lookback_days": 120,
        "default_params": {
            # Cap on the consecutive-green run length (PineScript checks up to 20).
            "max_run": 20,
            # Minimum high-vs-low move across the run, in percent.
            "gain_threshold_pct": 20.0,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "run_length",
        "run_gain_pct",
        "run_low",
        "run_high",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when the latest green run cleared the gain threshold."""
        frame = self.prepare_candles(candles)
        if frame.empty:
            return None

        max_run = self.coerce_param(params, "max_run", int)
        gain_threshold = self.coerce_param(params, "gain_threshold_pct", float) / 100.0

        run = _latest_green_run(frame, max_run)
        if run is None:
            return None
        # Strict ">" mirrors the PineScript `if gain > 0.2`.
        if run["gain"] <= gain_threshold:
            return None

        latest = frame.iloc[-1]
        reason = (
            f"{run['length']} consecutive green candle(s) moved "
            f"{run['gain'] * 100:.2f}% from low {run['run_low']:.2f} to high "
            f"{run['run_high']:.2f}."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": latest.get("timestamp", ""),
            "close": float(latest["close"]),
            "run_length": int(run["length"]),
            "run_gain_pct": float(run["gain"]),
            "run_low": run["run_low"],
            "run_high": run["run_high"],
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Daily candles, with the qualifying run's low/high as guide lines.

        The chart layer has no triangle-marker series (only candles, lines, and
        histograms), so the run is marked with two horizontal price lines rather
        than the PineScript's up-triangle.
        """
        max_run = self.coerce_param(params, "max_run", int)
        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(frame, title="Daily candles + 20% green run", ha=False)
        if frame.empty:
            return spec

        run = _latest_green_run(frame, max_run)
        panes = spec.get("panes", [])
        if run is not None and panes:
            # Mark the run extremes on the price pane (pane 0).
            panes[0].setdefault("price_lines", []).extend([
                {"price": run["run_low"], "color": "#26a69a", "title": "run low"},
                {"price": run["run_high"], "color": "#26a69a", "title": "run high"},
            ])
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = GreenCandles20PctUp()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
