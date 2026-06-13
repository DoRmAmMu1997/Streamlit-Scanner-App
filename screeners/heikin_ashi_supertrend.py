"""F&O daily Heikin Ashi SuperTrend crossover screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for every F&O stock selected by the loader.
2. Convert those normal candles into Heikin Ashi candles.
3. Calculate SuperTrend on the Heikin Ashi OHLC values, not the normal OHLC.
4. Shortlist only the latest candle when HA close crosses the SuperTrend line.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from backend.charts import add_supertrend_overlay, candlestick_with_volume
from backend.indicators import build_heikin_ashi, supertrend
from backend.scanner_base import BaseScanner


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


class HeikinAshiSupertrend(BaseScanner):
    """BUY/SELL on a fresh Heikin Ashi close vs. SuperTrend crossover."""

    # The scanner always processes every mapped row in the configured universe;
    # there is no per-run cap on the number of symbols scanned.
    SCREENER: ClassVar[dict] = {
        "key": "heikin_ashi_supertrend",
        "name": "Heikin Ashi SuperTrend",
        "description": "Shortlists F&O stocks when daily Heikin Ashi close crosses SuperTrend(10, 2).",
        "universe": "fno",
        "timeframe": "daily",
        "lookback_days": 120,
        "default_params": {"atr_period": 10, "multiplier": 2.0},
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "ha_open",
        "ha_high",
        "ha_low",
        "ha_close",
        "supertrend",
        "previous_ha_close",
        "previous_supertrend",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY/SELL row for a symbol, or None when there is no signal."""
        if candles.empty:
            # Empty frames usually mean the loader could not fetch usable data for
            # this symbol. The loader records failures separately, so the screener
            # can simply skip this stock.
            return None

        atr_period = self.coerce_param(params, "atr_period", int)
        multiplier = self.coerce_param(params, "multiplier", float)

        # The strategy rule is based on Heikin Ashi candles, so conversion happens
        # before any indicator calculation.
        ha = build_heikin_ashi(candles)
        if ha.empty:
            return None

        # SuperTrend needs columns named open/high/low/close. We pass HA values under
        # those names so the shared helper calculates the line from smoothed candles.
        st_frame = supertrend(
            _ha_ohlc_for_supertrend(ha),
            atr_period=atr_period,
            multiplier=multiplier,
        )
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
            triggered_rules = ["ha_close_crossed_above_supertrend"]
            reason = (
                f"Daily Heikin Ashi close ({latest_ha_close:.2f}) crossed above "
                f"SuperTrend({atr_period}, {multiplier:g}) at {latest_supertrend:.2f}."
            )
        # SELL is the mirror image: previous HA close at/above the line, latest HA
        # close below the line.
        elif previous_ha_close >= previous_supertrend and latest_ha_close < latest_supertrend:
            rating = "SELL"
            triggered_rules = ["ha_close_crossed_below_supertrend"]
            reason = (
                f"Daily Heikin Ashi close ({latest_ha_close:.2f}) crossed below "
                f"SuperTrend({atr_period}, {multiplier:g}) at {latest_supertrend:.2f}."
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
            "provenance": self.build_provenance(
                triggered_rules=triggered_rules,
                indicator_values={
                    "close": float(latest_ha["close"]),
                    "ha_close": latest_ha_close,
                    "supertrend": latest_supertrend,
                    "previous_ha_close": previous_ha_close,
                    "previous_supertrend": previous_supertrend,
                },
            ),
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render a Heikin Ashi chart with the SuperTrend line overlaid.

        The screener decides which candle type the user sees: this strategy is
        Heikin Ashi based, so the chart shows HA candles (not regular OHLC). The
        SuperTrend line is calculated on the HA OHLC for consistency with the
        `compute_signal(...)` logic above.
        """
        atr_period = self.coerce_param(params, "atr_period", int)
        multiplier = self.coerce_param(params, "multiplier", float)

        fig = candlestick_with_volume(
            candles,
            title=f"Heikin Ashi candles + SuperTrend({atr_period}, {multiplier:g})",
            ha=True,
        )

        # SuperTrend reads OHLC columns. Convert HA values into normal column names
        # via the local helper so the indicator math is identical to compute_signal.
        ha = build_heikin_ashi(candles)
        if not ha.empty:
            add_supertrend_overlay(
                fig,
                _ha_ohlc_for_supertrend(ha),
                atr_period=atr_period,
                multiplier=multiplier,
            )
        return fig


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = HeikinAshiSupertrend()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
