from __future__ import annotations

"""Hemant Super 45 — 14% Below 200 EMA BUY screener.

Flow in plain English:
1. Fetch daily candles for every Hemant Super 45 stock.
2. Compute the 200-period EMA on the close price.
3. Shortlist the stock when the latest close is at least `discount_pct`
   below the EMA200. With the default 14%, that means the close must sit at
   or below `0.86 * EMA200`.

The idea: a quality large-cap that has fallen well below its slow trend
line is often a candidate for mean reversion. This screener does not
guarantee a bounce — it just narrows the universe to the cheap-looking
stocks for further analysis.
"""

import pandas as pd

from backend.charts import add_line_overlay, candlestick_with_volume
from backend.indicators import ema
from backend.scanner_base import BaseScanner


class Ema200FourteenPctBelow(BaseScanner):
    """BUY when latest close is at least `discount_pct` below the 200-period EMA."""

    SCREENER = {
        "key": "ema200_14percent_below",
        "name": "14% Below 200 EMA",
        "description": (
            "Hemant Super 45 stocks trading at least 14% below their 200-period "
            "EMA on the latest close."
        ),
        "universe": "hemant_super_45",
        "timeframe": "daily",
        # Slightly more than the EMA period to leave the early candles room to
        # warm up the EMA value before the comparison.
        "lookback_days": 260,
        "default_params": {
            "ema_period": 200,
            # 0.14 == 14%. The user can lower this to 0.05 to see "mildly
            # cheap" stocks, or raise it to 0.20 for deeply discounted ones.
            "discount_pct": 0.14,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "ema200",
        "actual_discount_pct",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when the close is sufficiently below the EMA."""
        frame = self.prepare_candles(candles)
        ema_period = self.coerce_param(params, "ema_period", int)
        discount_pct = self.coerce_param(params, "discount_pct", float)

        # An EMA only starts producing values after roughly `ema_period`
        # warm-up rows, so a too-short history cannot answer the question.
        if frame.empty or len(frame) < ema_period:
            return None

        ema_values = ema(frame["close"], ema_period)
        latest_close = float(frame.iloc[-1]["close"])
        latest_ema = ema_values.iloc[-1]

        # NaN handling: if EMA hasn't formed yet, skip the symbol rather than
        # error out. This is the same warm-up handling used in every other
        # screener in this app.
        if pd.isna(latest_ema) or float(latest_ema) <= 0:
            return None

        latest_ema = float(latest_ema)
        # actual_discount = how far the close is BELOW the EMA, as a fraction
        # of the EMA. A value of 0.15 means the stock is 15% below its 200 EMA.
        actual_discount = (latest_ema - latest_close) / latest_ema

        if actual_discount < discount_pct:
            # Stock is not cheap enough relative to its trend line. Skip.
            return None

        # A clear, self-documenting reason for the table.
        reason = (
            f"Close {latest_close:.2f} is {actual_discount * 100:.2f}% below the "
            f"{ema_period}-period EMA ({latest_ema:.2f})."
        )

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": frame.iloc[-1].get("timestamp", ""),
            "close": latest_close,
            "ema200": latest_ema,
            "actual_discount_pct": actual_discount,
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Price chart with the EMA200 overlaid."""
        ema_period = self.coerce_param(params, "ema_period", int)

        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(
            frame,
            title=f"Daily candles + EMA {ema_period}",
            ha=False,
        )
        if frame.empty:
            return spec
        add_line_overlay(
            spec, frame["timestamp"], ema(frame["close"], ema_period),
            name=f"EMA {ema_period}", color="#ff9800", pane=0,
        )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases.
# ---------------------------------------------------------------------------

_scanner = Ema200FourteenPctBelow()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
