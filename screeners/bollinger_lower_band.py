"""Hemant Super 45 Bollinger lower-band BUY screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each mapped Hemant Super 45 stock.
2. Build Bollinger Bands with the long 200-candle / 2.5 standard-deviation
   settings from the strategy brief.
3. Shortlist a BUY when the latest close is at, below, or within a small buffer
   above the lower Bollinger Band.

This is the Bollinger half of what used to be the combined "Bollinger Knoxville
Buy" screener: the Knoxville Divergence confirmation now lives in its own
"Envelope + Knoxville" screener, leaving this one a pure lower-band proximity
scan. The name "Bollinger Lower Band" keeps it clearly distinct from the
separate "Bollinger Band Reversal" screener, which scans the F&O universe for
outer-band rejection candles.

This is a screener, not a trade manager. It answers "which stocks should I look
at today?" and intentionally returns no SELL/HOLD rows.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from backend.charts import add_bollinger_overlay, candlestick_with_volume
from backend.indicators import bollinger_bands
from backend.scanner_base import BaseScanner


class BollingerLowerBand(BaseScanner):
    """BUY when the latest close is at/near the lower Bollinger Band."""

    SCREENER: ClassVar[dict] = {
        "key": "bollinger_lower_band",
        "name": "Bollinger Lower Band",
        "description": (
            "Shortlists Hemant Super 45 stocks whose latest close is at, below, "
            "or within a small buffer of the daily lower Bollinger Band (200, 2.5)."
        ),
        "universe": "hemant_super_45",
        "timeframe": "daily",
        # Enough candles for a 200-period Bollinger Band plus warm-up. The app
        # prefetches ~10 years anyway; this drives the sidebar "Lookback".
        "lookback_days": 260,
        "default_params": {
            "bb_period": 200,
            "bb_std": 2.5,
            # "Close to or beneath": the close may sit below the lower band, on
            # it, or up to `bb_proximity_pct` above it.
            "bb_proximity_pct": 0.01,
        },
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "bb_lower",
        "bb_middle",
        "bb_upper",
        "bb_distance_pct",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY row when the close is at/near the lower band."""
        frame = self.prepare_candles(candles)
        bb_period = self.coerce_param(params, "bb_period", int)
        if frame.empty or len(frame) < bb_period:
            return None

        bb_std = self.coerce_param(params, "bb_std", float)
        bands = bollinger_bands(frame["close"], period=bb_period, std_multiplier=bb_std)
        frame = pd.concat([frame, bands], axis=1)

        latest = frame.iloc[-1]
        needed = latest[["close", "bb_lower", "bb_middle", "bb_upper"]]
        if needed.isna().any():
            return None

        close = float(latest["close"])
        lower_band = float(latest["bb_lower"])
        proximity_pct = self.coerce_param(params, "bb_proximity_pct", float)
        # Spelled out as an inequality: close <= lower_band * (1 + proximity_pct).
        # With the default 0.01, a lower band at 100 allows closes up to 101.
        if close > lower_band * (1.0 + proximity_pct):
            return None

        # Positive means the close is above the lower band; negative means it
        # closed beneath it. Easier to compare than raw rupees.
        bb_distance_pct = 0.0 if lower_band == 0 else (close - lower_band) / lower_band

        reason = (
            f"Close {close:.2f} is {bb_distance_pct * 100:.2f}% from the lower "
            f"Bollinger Band ({lower_band:.2f}) on the {bb_period}-period, "
            f"{bb_std:g} std setting."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": latest.get("timestamp", ""),
            "close": close,
            "bb_lower": lower_band,
            "bb_middle": float(latest["bb_middle"]),
            "bb_upper": float(latest["bb_upper"]),
            "bb_distance_pct": bb_distance_pct,
            "reason": reason,
            "provenance": self.build_provenance(
                triggered_rules=["close_within_proximity_of_lower_band"],
                indicator_values={
                    "close": close,
                    "bb_lower": lower_band,
                    "bb_middle": float(latest["bb_middle"]),
                    "bb_upper": float(latest["bb_upper"]),
                    "bb_distance_pct": bb_distance_pct,
                },
            ),
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render daily candles with the screener's Bollinger Bands overlaid."""
        bb_period = self.coerce_param(params, "bb_period", int)
        bb_std = self.coerce_param(params, "bb_std", float)

        spec = candlestick_with_volume(
            candles,
            title=f"Daily candles + Bollinger Bands({bb_period}, {bb_std:g})",
            ha=False,
        )
        add_bollinger_overlay(spec, candles, period=bb_period, std_multiplier=bb_std)
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = BollingerLowerBand()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
