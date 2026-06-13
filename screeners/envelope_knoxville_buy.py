"""Hemant Super 45 Envelope + Knoxville BUY screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each mapped Hemant Super 45 stock.
2. Build an Envelope around the 200-period EMA (basis + bands a fixed percent
   away). The lower band at 14% sits at `0.86 * EMA200`.
3. Look for a recent bullish Knoxville Divergence: price makes a lower pivot
   low while momentum makes a higher pivot low and RSI is oversold.
4. Shortlist BUY candidates through either of two paths:
   - the original setup: near the lower Envelope plus a recent bullish Knoxville
     Divergence
   - the retest setup: the current close has returned to the latest bullish
     Knoxville pivot low, even if that divergence happened long ago

This pairs the Envelope lower-band "stretched down" filter with the Knoxville
"selling pressure fading" filter — the same two-filter idea the old combined
Bollinger screener used, but on the Envelope instead of Bollinger Bands.

Beginner note:
The retest setup is an OR path. A stock can qualify because it revisits the last
bullish Knoxville pivot low even when the lower Envelope band is not nearby.

Beginner glossary:
- **Knoxville Divergence**: price prints a lower low but momentum prints a
  higher low. The price says "down", momentum says "not as down as last time"
  — a classic signal that selling pressure may be fading.
"""

from __future__ import annotations

import math
from typing import ClassVar

import pandas as pd

from backend.charts import add_envelope_overlay, add_series_markers, candlestick_with_volume
from backend.indicators import bullish_knoxville_divergences, envelope
from backend.scanner_base import BaseScanner


class EnvelopeKnoxvilleBuy(BaseScanner):
    """BUY-only screener for lower-Envelope Knoxville setups and KD retests."""

    SCREENER: ClassVar[dict] = {
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
            # A stock may also qualify when it retests the most recent bullish
            # Knoxville pivot low, even if that divergence is old.
            "kd_retest_proximity_pct": 0.02,
            "pivot_left": 2,
            "pivot_right": 2,
            "oversold": 30.0,
        },
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "env_basis",
        "env_lower",
        "env_upper",
        "env_distance_pct",
        "entry_trigger",
        "divergence_date",
        "divergence_price",
        "divergence_bars_ago",
        "kd_retest_distance_pct",
        "rsi",
        "momentum",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY row for a symbol, or None when no entry path qualifies.

        There are two entry paths:
        1. `recent_envelope_kd`: the classic rule. Price is close to the lower
           Envelope band and a bullish Knoxville Divergence happened recently.
        2. `old_kd_retest`: the new rule. The latest close is at, below, or only
           slightly above the most recent Knoxville pivot low. This path does not
           require the Envelope band to be nearby.
        """
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
        near_envelope = close <= lower_band * (1.0 + proximity_pct)

        # Collect all divergences once. The recent path only needs the latest
        # recent one, but the retest path and chart markers need older pivots too.
        all_divergences = bullish_knoxville_divergences(
            frame,
            rsi_period=self.coerce_param(params, "rsi_period", int),
            momentum_period=self.coerce_param(params, "momentum_period", int),
            bars_back=self.coerce_param(params, "divergence_bars_back", int),
            pivot_left=self.coerce_param(params, "pivot_left", int),
            pivot_right=self.coerce_param(params, "pivot_right", int),
            oversold=self.coerce_param(params, "oversold", float),
        )
        if not all_divergences:
            return None

        env_distance_pct = 0.0 if lower_band == 0 else (close - lower_band) / lower_band

        signal_recency = self.coerce_param(params, "signal_recency_bars", int)
        recent_divergence = None
        for candidate in reversed(all_divergences):
            # `candidate.name` is the row number inside `frame`. Comparing it to
            # the last row tells us how many candles ago the divergence occurred.
            if len(frame) - 1 - int(candidate.name) <= signal_recency:
                recent_divergence = candidate
                break

        last_divergence = all_divergences[-1]
        retest_proximity = self.coerce_param(params, "kd_retest_proximity_pct", float)
        divergence_price = float(last_divergence["low"])
        # Positive means current price is above the pivot low; negative means it
        # has undercut the pivot low. Both are useful to show in the results.
        kd_retest_distance_pct = (
            (close - divergence_price) / divergence_price
            if divergence_price > 0
            else math.inf
        )

        entry_trigger = ""
        divergence = None
        if near_envelope and recent_divergence is not None:
            entry_trigger = "recent_envelope_kd"
            divergence = recent_divergence
        elif close <= divergence_price * (1.0 + retest_proximity):
            # This is an OR condition. It deliberately does not check
            # `near_envelope`, because the rule is about revisiting the Knoxville
            # pivot price itself.
            entry_trigger = "old_kd_retest"
            divergence = last_divergence
        if divergence is None:
            return None

        selected_divergence_price = float(divergence["low"])
        selected_bars_ago = len(frame) - 1 - int(divergence.name)
        rsi_value = float(divergence["rsi"])
        momentum_value = float(divergence["momentum"])
        if entry_trigger == "recent_envelope_kd":
            triggered_rules = [
                "close_near_lower_envelope_band",
                "recent_bullish_knoxville_divergence",
            ]
            reason = (
                f"Close {close:.2f} is {env_distance_pct * 100:.2f}% from the lower "
                f"Envelope band ({lower_band:.2f}). Recent bullish Knoxville: "
                f"price made a lower low while momentum made a higher low "
                f"({momentum_value:.2f}) with RSI oversold ({rsi_value:.1f})."
            )
        else:
            triggered_rules = ["knoxville_pivot_low_retest"]
            reason = (
                f"Close {close:.2f} is {kd_retest_distance_pct * 100:.2f}% from "
                f"the last bullish Knoxville pivot low ({selected_divergence_price:.2f}) "
                f"made {selected_bars_ago} bars ago. This is within the "
                f"{retest_proximity * 100:.2f}% retest buffer; Envelope proximity "
                f"is not required for this trigger."
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
            "entry_trigger": entry_trigger,
            "divergence_date": divergence.get("timestamp", ""),
            "divergence_price": selected_divergence_price,
            "divergence_bars_ago": selected_bars_ago,
            "kd_retest_distance_pct": kd_retest_distance_pct,
            "rsi": rsi_value,
            "momentum": momentum_value,
            "reason": reason,
            "provenance": self.build_provenance(
                triggered_rules=triggered_rules,
                indicator_values={
                    "close": close,
                    "env_basis": float(latest_basis),
                    "env_lower": lower_band,
                    "env_distance_pct": env_distance_pct,
                    "divergence_price": selected_divergence_price,
                    "divergence_bars_ago": selected_bars_ago,
                    "kd_retest_distance_pct": kd_retest_distance_pct,
                    "rsi": rsi_value,
                    "momentum": momentum_value,
                },
            ),
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render daily candles with Envelope bands and bullish KD markers.

        The chart marks every bullish Knoxville Divergence found in the loaded
        history. Older pivots use `KD`; the most recent pivot uses `Latest KD` so
        a user can quickly see the level used by the retest rule.
        """
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
        divergences = bullish_knoxville_divergences(
            self.prepare_candles(candles),
            rsi_period=self.coerce_param(params, "rsi_period", int),
            momentum_period=self.coerce_param(params, "momentum_period", int),
            bars_back=self.coerce_param(params, "divergence_bars_back", int),
            pivot_left=self.coerce_param(params, "pivot_left", int),
            pivot_right=self.coerce_param(params, "pivot_right", int),
            oversold=self.coerce_param(params, "oversold", float),
        )
        markers = []
        for index, divergence in enumerate(divergences):
            is_latest = index == len(divergences) - 1
            # Markers attach to the price candle series, not a separate indicator
            # pane, because the actionable level is the divergence candle's low.
            markers.append(
                {
                    "time": divergence["timestamp"],
                    "position": "belowBar",
                    "shape": "arrowUp",
                    "color": "#ffd54f" if is_latest else "#00c853",
                    "text": "Latest KD" if is_latest else "KD",
                }
            )
        add_series_markers(spec, markers)
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = EnvelopeKnoxvilleBuy()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
