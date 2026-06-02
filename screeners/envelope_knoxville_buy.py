"""Hemant Super 45 Envelope + Knoxville BUY screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each mapped Hemant Super 45 stock.
2. Build an Envelope around the 200-period EMA (basis + bands a fixed percent
   away). The lower band at 14% sits at `0.86 * EMA200`.
3. Look for a recent bullish Knoxville Divergence: price makes a lower pivot
   low while momentum makes a higher pivot low and RSI is oversold.
4. Shortlist only BUY candidates whose latest close is at, below, or within a
   small buffer above the lower Envelope band.

This pairs the Envelope lower-band "stretched down" filter with the Knoxville
"selling pressure fading" filter — the same two-filter idea the old combined
Bollinger screener used, but on the Envelope instead of Bollinger Bands.

Beginner glossary:
- **Knoxville Divergence**: price prints a lower low but momentum prints a
  higher low. The price says "down", momentum says "not as down as last time"
  — a classic signal that selling pressure may be fading.
"""

from __future__ import annotations

import pandas as pd

from backend.charts import add_envelope_overlay, candlestick_with_volume
from backend.indicators import bullish_knoxville_divergence, envelope
from backend.scanner_base import BaseScanner


class EnvelopeKnoxvilleBuy(BaseScanner):
    """BUY-only Knoxville Divergence near the lower Envelope band."""

    SCREENER = {
        "key": "envelope_knoxville_buy",
        "name": "Envelope + Knoxville",
        "description": (
            "Shortlists Hemant Super 45 stocks near the daily lower Envelope band"
            " (200-EMA basis, 14% bands) with a recent bullish Knoxville Divergence"
            " (Bars Back 20, RSI 14)."
        ),
        "universe": "hemant_super_45",
        "timeframe": "daily",
        # Enough candles for the 200-EMA basis plus the divergence lookback.
        "lookback_days": 430,
        # Strategy defaults, not hardcoded magic. Tests override them with tiny
        # values so the rule can be proved on a compact synthetic candle set.
        "default_params": {
            "ema_period": 200,
            "percent": 14.0,
            "exponential": True,
            # "Close to or beneath": close may sit up to this fraction above the
            # lower band and still qualify.
            "env_proximity_pct": 0.01,
            # Knoxville settings per the strategy brief: Bars Back 20, RSI 14.
            "rsi_period": 14,
            "momentum_period": 20,
            "divergence_bars_back": 20,
            "signal_recency_bars": 10,
            "pivot_left": 2,
            "pivot_right": 2,
            "oversold": 30.0,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "env_basis",
        "env_lower",
        "env_upper",
        "env_distance_pct",
        "divergence_date",
        "rsi",
        "momentum",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY row for a symbol, or None when either filter fails."""
        frame = self.prepare_candles(candles)
        ema_period = self.coerce_param(params, "ema_period", int)
        if frame.empty or len(frame) < ema_period:
            return None

        percent = self.coerce_param(params, "percent", float)
        # `coerce_param` already casts with `bool`, so this is a real bool.
        exponential = self.coerce_param(params, "exponential", bool)
        bands = envelope(
            frame["close"], period=ema_period, percent=percent, exponential=exponential
        )
        latest = frame.iloc[-1]
        latest_basis = bands["env_basis"].iloc[-1]
        latest_lower = bands["env_lower"].iloc[-1]
        if pd.isna(latest_basis) or float(latest_basis) <= 0 or pd.isna(latest_lower):
            return None

        close = float(latest["close"])
        lower_band = float(latest_lower)
        proximity_pct = self.coerce_param(params, "env_proximity_pct", float)
        # Bollinger-style proximity test, but against the Envelope lower band:
        #     close <= lower_band * (1 + proximity_pct)
        if close > lower_band * (1.0 + proximity_pct):
            return None

        # The Envelope filter says price is stretched down; the Knoxville filter
        # says downside momentum may be weakening. Both are required for a BUY.
        divergence = bullish_knoxville_divergence(
            frame,
            rsi_period=self.coerce_param(params, "rsi_period", int),
            momentum_period=self.coerce_param(params, "momentum_period", int),
            bars_back=self.coerce_param(params, "divergence_bars_back", int),
            recency=self.coerce_param(params, "signal_recency_bars", int),
            pivot_left=self.coerce_param(params, "pivot_left", int),
            pivot_right=self.coerce_param(params, "pivot_right", int),
            oversold=self.coerce_param(params, "oversold", float),
        )
        if divergence is None:
            return None

        if lower_band == 0:
            env_distance_pct = 0.0
        else:
            env_distance_pct = (close - lower_band) / lower_band

        rsi_value = float(divergence["rsi"])
        momentum_value = float(divergence["momentum"])
        reason = (
            f"Close {close:.2f} is {env_distance_pct * 100:.2f}% from the lower "
            f"Envelope band ({lower_band:.2f}). Bullish Knoxville: price made a "
            f"lower low while momentum made a higher low ({momentum_value:.2f}) "
            f"with RSI oversold ({rsi_value:.1f})."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": latest.get("timestamp", ""),
            "close": close,
            "env_basis": float(latest_basis),
            "env_lower": lower_band,
            "env_upper": float(bands["env_upper"].iloc[-1]),
            "env_distance_pct": env_distance_pct,
            "divergence_date": divergence.get("timestamp", ""),
            "rsi": rsi_value,
            "momentum": momentum_value,
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render daily candles with the screener's Envelope overlaid."""
        ema_period = self.coerce_param(params, "ema_period", int)
        percent = self.coerce_param(params, "percent", float)
        exponential = self.coerce_param(params, "exponential", bool)

        spec = candlestick_with_volume(
            candles,
            title=f"Daily candles + Envelope({ema_period}, {percent:g}%)",
            ha=False,
        )
        add_envelope_overlay(
            spec, candles, period=ema_period, percent=percent, exponential=exponential
        )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = EnvelopeKnoxvilleBuy()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
