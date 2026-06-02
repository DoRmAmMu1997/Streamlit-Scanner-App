"""Hemant Super 45 — Envelope (lower band) BUY screener.

Flow in plain English:
1. Fetch daily candles for every Hemant Super 45 stock.
2. Build an Envelope around the 200-period EMA: a basis line plus an upper and
   a lower band a fixed `percent` away from it (TradingView's "Envelope").
3. Shortlist the stock when the latest close is at or below the LOWER band.

With the default 200-EMA basis and 14% bands, the lower band sits at
`0.86 * EMA200`, so "close at/below the lower band" is exactly "close at least
14% below the 200 EMA" — the mean-reversion idea the previous "14% Below 200
EMA" screener captured, now expressed (and charted) as a full envelope.

This is a *shortlist*, not a buy signal on its own: a quality large-cap that
has fallen to the bottom of its envelope is a candidate for further analysis.
"""

from __future__ import annotations

import pandas as pd

from backend.charts import add_envelope_overlay, candlestick_with_volume
from backend.indicators import envelope
from backend.scanner_base import BaseScanner


class Envelope(BaseScanner):
    """BUY when the latest close is at or below the lower Envelope band."""

    SCREENER = {
        "key": "envelope",
        "name": "Envelope",
        "description": (
            "Hemant Super 45 stocks whose latest close is at or below the lower "
            "Envelope band (200-EMA basis, 14% bands) — i.e. at least 14% below "
            "the 200 EMA."
        ),
        "universe": "hemant_super_45",
        "timeframe": "daily",
        # Slightly more than the EMA period so the early candles can warm up the
        # EMA basis before the comparison.
        "lookback_days": 260,
        "default_params": {
            "ema_period": 200,
            # Band width in percent. 14.0 -> lower band at 0.86 * basis.
            "percent": 14.0,
            # True -> EMA basis (TradingView default); False -> SMA basis.
            "exponential": True,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "env_basis",
        "env_lower",
        "env_upper",
        "pct_below_basis",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when the close sits at/below the lower band."""
        frame = self.prepare_candles(candles)
        ema_period = self.coerce_param(params, "ema_period", int)
        percent = self.coerce_param(params, "percent", float)
        # `coerce_param` already casts with `bool`, so this is a real bool.
        exponential = self.coerce_param(params, "exponential", bool)

        # The moving-average basis needs roughly `ema_period` warm-up rows
        # before it produces a usable value, so a too-short history cannot
        # answer the question.
        if frame.empty or len(frame) < ema_period:
            return None

        bands = envelope(
            frame["close"], period=ema_period, percent=percent, exponential=exponential
        )
        latest_close = float(frame.iloc[-1]["close"])
        latest_basis = bands["env_basis"].iloc[-1]
        latest_lower = bands["env_lower"].iloc[-1]

        # NaN handling: if the basis hasn't formed yet, skip the symbol rather
        # than error out — the same warm-up handling every screener uses.
        if pd.isna(latest_basis) or float(latest_basis) <= 0 or pd.isna(latest_lower):
            return None

        latest_basis = float(latest_basis)
        latest_lower = float(latest_lower)

        # The trigger: close at or below the lower envelope band.
        if latest_close > latest_lower:
            return None

        # How far the close is below the basis, as a fraction of the basis. With
        # 14% bands this is >= 0.14 for any row that clears the trigger, and it
        # gives a natural "how stretched" ordering for the results table.
        pct_below_basis = (latest_basis - latest_close) / latest_basis

        reason = (
            f"Close {latest_close:.2f} is at/below the lower Envelope band "
            f"({latest_lower:.2f}); {pct_below_basis * 100:.2f}% below the "
            f"{ema_period}-period basis ({latest_basis:.2f})."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": frame.iloc[-1].get("timestamp", ""),
            "close": latest_close,
            "env_basis": latest_basis,
            "env_lower": latest_lower,
            "env_upper": float(bands["env_upper"].iloc[-1]),
            "pct_below_basis": pct_below_basis,
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Price chart with the Envelope basis + upper/lower bands overlaid."""
        ema_period = self.coerce_param(params, "ema_period", int)
        percent = self.coerce_param(params, "percent", float)
        exponential = self.coerce_param(params, "exponential", bool)

        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(
            frame,
            title=f"Daily candles + Envelope({ema_period}, {percent:g}%)",
            ha=False,
        )
        if frame.empty:
            return spec
        add_envelope_overlay(
            spec, frame, period=ema_period, percent=percent, exponential=exponential
        )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases.
# ---------------------------------------------------------------------------

_scanner = Envelope()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
