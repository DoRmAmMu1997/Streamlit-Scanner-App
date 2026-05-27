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

Beginner glossary:
- **Pivot low**: a candle whose `low` is lower than the surrounding candles.
  We need a few "future" candles before we can confirm one — that delay is
  intentional and prevents acting on a low that tomorrow's bar could undercut.
- **Knoxville Divergence**: a setup where price prints a lower low but
  momentum prints a higher low. The price says "down", the momentum says
  "not as down as last time" — a classic signal that selling pressure is
  fading.
"""

import pandas as pd

from backend.charts import add_bollinger_overlay, candlestick_with_volume
from backend.indicators import bollinger_bands, momentum, pivot_lows, rsi
from backend.scanner_base import BaseScanner


class BollingerKnoxvilleBuy(BaseScanner):
    """BUY-only Knoxville Divergence near the lower Bollinger Band."""

    SCREENER = {
        "key": "bollinger_knoxville_buy",
        "name": "Bollinger Knoxville Buy",
        "description": (
            "Shortlists Hemant Super 45 stocks near the daily lower Bollinger Band"
            " (200, 2.5) with a recent bullish Knoxville Divergence."
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

    EXTRA_RESULT_COLUMNS = [
        "bb_lower",
        "bb_middle",
        "bb_upper",
        "bb_distance_pct",
        "divergence_date",
        "rsi",
        "momentum",
    ]

    # ------------------------------------------------------------------
    # Indicator enrichment
    # ------------------------------------------------------------------

    def _enrich(self, candles: pd.DataFrame, params: dict) -> pd.DataFrame:
        """Add Bollinger Bands, RSI, and Momentum columns used by the rule."""
        bb_period = self.coerce_param(params, "bb_period", int)
        bb_std = self.coerce_param(params, "bb_std", float)
        rsi_period = self.coerce_param(params, "rsi_period", int)
        momentum_period = self.coerce_param(params, "momentum_period", int)

        frame = candles.copy()
        bands = bollinger_bands(frame["close"], period=bb_period, std_multiplier=bb_std)
        frame = pd.concat([frame, bands], axis=1)
        frame["rsi"] = rsi(frame["close"], period=rsi_period)
        frame["momentum"] = momentum(frame["close"], period=momentum_period)
        return frame

    # ------------------------------------------------------------------
    # Divergence detection
    # ------------------------------------------------------------------

    def _latest_bullish_knoxville(self, enriched: pd.DataFrame, params: dict) -> pd.Series | None:
        """Return the latest bullish Knoxville Divergence bar, if present."""
        left = self.coerce_param(params, "pivot_left", int)
        right = self.coerce_param(params, "pivot_right", int)
        bars_back = self.coerce_param(params, "divergence_bars_back", int)
        recency = self.coerce_param(params, "signal_recency_bars", int)
        oversold = self.coerce_param(params, "oversold", float)

        # `pivot_lows` is the vectorized helper now living in
        # backend.indicators. It returns True on confirmed pivot rows and
        # False on the last `right` candles (they cannot have future bars
        # to confirm against).
        pivot_mask = pivot_lows(enriched["low"], left=left, right=right)
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
        # Iterate from the most recent prior pivot backward. The first match
        # (closest to the current pivot in time) is the most relevant divergence
        # pair — using it avoids reporting a stale older pair when a more recent
        # one already explains today's setup.
        for prior_index in reversed(prior_pivots.index.tolist()):
            prior = enriched.loc[prior_index]
            price_made_lower_low = float(latest["low"]) < float(prior["low"])
            momentum_made_higher_low = float(latest["momentum"]) > float(prior["momentum"])
            if price_made_lower_low and momentum_made_higher_low:
                return latest
        return None

    # ------------------------------------------------------------------
    # Strategy hook
    # ------------------------------------------------------------------

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY row for a symbol, or None when either filter fails."""
        frame = self.prepare_candles(candles)
        bb_period = self.coerce_param(params, "bb_period", int)
        if frame.empty or len(frame) < bb_period:
            return None

        enriched = self._enrich(frame, params)
        latest = enriched.iloc[-1]
        needed = latest[["close", "bb_lower", "bb_middle", "bb_upper"]]
        if needed.isna().any():
            return None

        close = float(latest["close"])
        lower_band = float(latest["bb_lower"])
        proximity_pct = self.coerce_param(params, "bb_proximity_pct", float)
        # "Close to or beneath" means the close may be below the lower band, on it,
        # or up to `bb_proximity_pct` above it. Spelled out as an inequality:
        #     close <= lower_band * (1 + proximity_pct)
        # With the default 0.01, a lower band at 100 allows closes up to 101.
        if close > lower_band * (1.0 + proximity_pct):
            return None

        # The Bollinger filter says price is stretched down; the Knoxville filter
        # says downside momentum may be weakening. Both are required for a BUY row.
        divergence = self._latest_bullish_knoxville(enriched, params)
        if divergence is None:
            return None

        if lower_band == 0:
            bb_distance_pct = 0.0
        else:
            # Positive means close is above the lower band; negative means it closed
            # beneath the lower band. This is easier to compare than raw rupees.
            bb_distance_pct = (close - lower_band) / lower_band

        rsi_value = float(divergence["rsi"])
        momentum_value = float(divergence["momentum"])
        # A dynamic reason interpolates the actual indicator values into the
        # message. This makes the Streamlit results table self-explanatory and
        # keeps the wording in sync if the strategy parameters change later.
        reason = (
            f"Close {close:.2f} is {bb_distance_pct * 100:.2f}% above lower Bollinger "
            f"({lower_band:.2f}). Bullish Knoxville: price made a lower low while "
            f"momentum made a higher low ({momentum_value:.2f}) with RSI "
            f"oversold ({rsi_value:.1f})."
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
            "divergence_date": divergence.get("timestamp", ""),
            "rsi": rsi_value,
            "momentum": momentum_value,
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Chart
    # ------------------------------------------------------------------

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

_scanner = BollingerKnoxvilleBuy()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
