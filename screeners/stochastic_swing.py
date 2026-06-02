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

from __future__ import annotations

import pandas as pd

from backend.charts import add_line_overlay, add_stochastic_overlay, candlestick_volume_oscillator
from backend.indicators import ema, sma, stochastic
from backend.scanner_base import BaseScanner


# Fixed risk rules from the original strategy brief. They are not UI knobs, so
# they live here as module constants rather than in `default_params`.
STOP_LOSS_PCT = 0.03
TARGET_PCT = 0.05
# A 5 EMA / 200 SMA crossover only "confirms" an entry while it is this fresh.
MAX_CONFIRMATION_AGE = pd.Timedelta(days=7)


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


class StochasticSwing(BaseScanner):
    """BUY/SELL when a Stochastic %K/%D cross aligns with SMA200 trend + EMA cross."""

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

    EXTRA_RESULT_COLUMNS = [
        "sma200",
        "ema5",
        "stoch_k",
        "stoch_d",
        "previous_stoch_k",
        "previous_stoch_d",
        "stop",
        "target",
    ]

    def _enrich(self, candles: pd.DataFrame, params: dict) -> pd.DataFrame:
        """Add SMA200, EMA5, Stochastic, and the EMA/SMA confirmation flags.

        The indicators come from `backend.indicators`, which routes through TA-Lib
        when available and falls back to pure-pandas maths otherwise.
        """
        sma_period = self.coerce_param(params, "sma_period", int)
        ema_period = self.coerce_param(params, "ema_period", int)
        k_period = self.coerce_param(params, "stoch_k", int)
        k_smoothing = self.coerce_param(params, "stoch_k_smoothing", int)
        d_smoothing = self.coerce_param(params, "stoch_d_smoothing", int)

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

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY/SELL row for a symbol, or None when there is no entry."""
        frame = self.prepare_candles(candles)
        sma_period = self.coerce_param(params, "sma_period", int)

        # The strategy needs a real SMA200 value plus one previous candle for the
        # crossover comparison. Too-short histories are skipped, not errors.
        if frame.empty or len(frame) <= sma_period:
            return None

        oversold = self.coerce_param(params, "oversold", float)
        overbought = self.coerce_param(params, "overbought", float)

        enriched = self._enrich(frame, params)
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
                f"Stochastic %K ({latest_k:.1f}) crossed above %D ({latest_d:.1f}) "
                f"from oversold; price {close:.2f} above SMA200 {sma200:.2f} with a "
                f"fresh 5 EMA / 200 SMA bullish crossover."
            )
        elif short_setup:
            rating = "SELL"
            stop = close * (1.0 + STOP_LOSS_PCT)
            target = close * (1.0 - TARGET_PCT)
            reason = (
                f"Stochastic %K ({latest_k:.1f}) crossed below %D ({latest_d:.1f}) "
                f"from overbought; price {close:.2f} below SMA200 {sma200:.2f} with a "
                f"fresh 5 EMA / 200 SMA bearish crossover."
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

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render a 3-panel chart: price + SMA200 + EMA5 / volume / Stochastic.

        The Stochastic is an oscillator bounded 0-100, so it gets its own panel
        rather than being overlaid on price. SMA200 and EMA5 are price overlays.
        """
        sma_period = self.coerce_param(params, "sma_period", int)
        ema_period = self.coerce_param(params, "ema_period", int)
        k_period = self.coerce_param(params, "stoch_k", int)
        k_smoothing = self.coerce_param(params, "stoch_k_smoothing", int)
        d_smoothing = self.coerce_param(params, "stoch_d_smoothing", int)
        oversold = self.coerce_param(params, "oversold", float)
        overbought = self.coerce_param(params, "overbought", float)

        frame = self.prepare_candles(candles)
        spec = candlestick_volume_oscillator(
            frame,
            title=f"Daily candles + SMA{sma_period}/EMA{ema_period} + Stochastic",
            ha=False,
        )
        if frame.empty:
            # `candlestick_volume_oscillator` already returned a placeholder spec.
            return spec

        # Price-pane overlays (pane 0): the slow trend (SMA200) and fast line (EMA5).
        add_line_overlay(
            spec, frame["timestamp"], sma(frame["close"], sma_period),
            name=f"SMA {sma_period}", color="#ab47bc", pane=0,
        )
        add_line_overlay(
            spec, frame["timestamp"], ema(frame["close"], ema_period),
            name=f"EMA {ema_period}", color="#ffca28", pane=0,
        )
        # Oscillator pane (pane 2): %K, %D, and the 20/80 guide lines.
        add_stochastic_overlay(
            spec, frame,
            k_period=k_period, k_smoothing=k_smoothing, d_smoothing=d_smoothing,
            oversold=oversold, overbought=overbought, pane=2,
        )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = StochasticSwing()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
