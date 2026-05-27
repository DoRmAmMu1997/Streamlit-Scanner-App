from __future__ import annotations

"""F&O daily Bollinger Band reversal screener.

Flow in plain English:
1. Fetch normal daily OHLC candles for each selected F&O stock.
2. Build Bollinger Bands from normal close prices only.
3. Inspect only the latest valid candle.
4. Shortlist a BUY/SELL when the candle rejects the outer band and closes with
   the required color.
"""

import pandas as pd

from backend.charts import add_bollinger_overlay, candlestick_with_volume
from backend.indicators import bollinger_bands
from backend.scanner_base import BaseScanner


class BollingerBandReversal(BaseScanner):
    """BUY/SELL when a candle rejects the outer Bollinger Band."""

    # Metadata picked up by the registry. The scanner runs across every mapped
    # symbol in the configured universe; there is no per-run cap on the number
    # of symbols scanned.
    SCREENER = {
        "key": "bollinger_band_reversal",
        "name": "Bollinger Band Reversal",
        "description": "Shortlists F&O stocks with daily Bollinger Band(20, 2) rejection candles.",
        "universe": "fno",
        "timeframe": "daily",
        "lookback_days": 80,
        "default_params": {"period": 20, "std_multiplier": 2.0},
    }

    # These extras are appended to the common schema in BaseScanner so the
    # output DataFrame has consistent leading columns across all screeners.
    EXTRA_RESULT_COLUMNS = [
        "open",
        "high",
        "low",
        "bb_middle",
        "bb_upper",
        "bb_lower",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return one BUY/SELL row for a symbol, or None when there is no signal."""
        frame = self.prepare_candles(candles)
        if frame.empty:
            # Empty data is not an error for the screener. The data loader tracks API
            # failures separately, and this module only decides whether candles signal.
            return None

        period = self.coerce_param(params, "period", int)
        std_multiplier = self.coerce_param(params, "std_multiplier", float)

        # This screener uses normal candles only. Unlike the SuperTrend screener, no
        # Heikin Ashi conversion is done before calculating the indicator.
        bands = bollinger_bands(frame["close"], period=period, std_multiplier=std_multiplier)
        frame = pd.concat([frame, bands], axis=1)
        # Bollinger Bands need a full rolling window, so the first period-1 rows have
        # NaN bands. Dropping them leaves only candles where all three bands exist.
        valid = frame.dropna(subset=["bb_middle", "bb_upper", "bb_lower"]).copy()
        if valid.empty:
            return None

        # The strategy is a latest-candle scanner. Historical signals are ignored
        # because the Streamlit table is meant to show today's actionable shortlist.
        latest = valid.iloc[-1]
        open_price = float(latest["open"])
        high_price = float(latest["high"])
        low_price = float(latest["low"])
        close_price = float(latest["close"])
        upper_band = float(latest["bb_upper"])
        lower_band = float(latest["bb_lower"])

        rating = ""
        reason = ""
        # BUY setup: price pierced below the lower band intraday, but buyers pushed
        # the candle to a green close. Green means close is greater than open.
        if low_price < lower_band and close_price > open_price:
            rating = "BUY"
            reason = (
                f"Daily candle traded below the lower Bollinger Band "
                f"({lower_band:.2f}) and closed green at {close_price:.2f}."
            )
        # SELL setup: price pierced above the upper band intraday, but sellers forced
        # a red close. Red means close is less than open.
        elif high_price > upper_band and close_price < open_price:
            rating = "SELL"
            reason = (
                f"Daily candle traded above the upper Bollinger Band "
                f"({upper_band:.2f}) and closed red at {close_price:.2f}."
            )
        else:
            # No fresh reversal signal, so omit the symbol from the shortlist.
            return None

        return {
            "symbol": symbol,
            "rating": rating,
            "signal_date": latest.get("timestamp", ""),
            "open": open_price,
            "high": high_price,
            "low": low_price,
            "close": close_price,
            "bb_middle": float(latest["bb_middle"]),
            "bb_upper": upper_band,
            "bb_lower": lower_band,
            "reason": reason,
        }

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Render a regular candlestick chart with Bollinger Bands overlaid."""
        period = self.coerce_param(params, "period", int)
        std_multiplier = self.coerce_param(params, "std_multiplier", float)

        fig = candlestick_with_volume(
            candles,
            title=f"Daily candles + Bollinger Bands({period}, {std_multiplier:g})",
            ha=False,
        )
        add_bollinger_overlay(fig, candles, period=period, std_multiplier=std_multiplier)
        return fig


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
#
# Some tests and external callers still import this module directly and call
# `bollinger_band_reversal.run(...)`. The aliases below preserve that path
# without forcing a class instance call.
# ---------------------------------------------------------------------------

_scanner = BollingerBandReversal()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
