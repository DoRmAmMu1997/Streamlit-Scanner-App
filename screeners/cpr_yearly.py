"""CPR Yearly Reversal — yearly Central Pivot Range downtrend-reversal screener.

Flow in plain English:
1. Fetch daily candles for every stock in the universe (the loader hands each
   screener ~10 years of history).
2. Build the **yearly** Central Pivot Range (CPR). Exactly like the intraday CPR
   uses the *previous day's* High/Low/Close, the yearly CPR "in effect" during a
   year is computed from the **previous full year's** High/Low/Close. So the CPR
   labelled for 2025 is derived from 2024's H/L/C, and the "previous year high"
   is 2024's High (the ``H`` term of the 2025 CPR).
3. Read three consecutive yearly central pivots — this year (``Y``), last year
   (``Y-1``) and two years ago (``Y-2``). A strictly **descending** pivot ladder
   ``P(Y-2) > P(Y-1) > P(Y)`` marks a multi-year structural downtrend.
4. Resample the daily candles to **weekly** and check whether a weekly close has
   **recently reclaimed the previous year's high** — an early sign the downtrend
   may be reversing.

Beginner note: this is a *shortlist*, not a buy signal on its own. A trader still
opens the chart (weekly candles + the yearly CPR lines this screener draws) and
decides whether the reclaim is holding. The "Check Fundamentals" panel the app
renders under every shortlisted chart is the natural next step.
"""

from __future__ import annotations

from typing import ClassVar

import pandas as pd

from backend.charts import add_cpr_overlay, candlestick_with_volume
from backend.indicators import resample_to_weekly, yearly_cpr
from backend.scanner_base import BaseScanner


class CprYearlyReversal(BaseScanner):
    """BUY when yearly CPR pivots are falling and price just reclaimed last year's high."""

    SCREENER: ClassVar[dict] = {
        "key": "cpr_yearly",
        "name": "CPR Yearly Reversal",
        "description": (
            "Nifty 500 stocks whose yearly Central Pivot Range has stepped down "
            "for three straight years (this year's pivot below last year's below "
            "the year before) and whose weekly close has just reclaimed the "
            "previous year's high — a potential downtrend reversal."
        ),
        "universe": "nifty_500",
        "timeframe": "daily",
        # ~4 calendar years of daily candles are needed (three complete prior years
        # to derive the three yearly pivots, plus the current year's price action).
        # The loader actually serves the full ~10-year history; this is UI context.
        "lookback_days": 1460,
        "default_params": {
            # "Recently reclaimed" — how many of the most recent weekly candles the
            # up-cross above the previous-year high may sit in. Smaller = fresher.
            "recent_cross_weeks": 4,
        },
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "pivot_2y_ago",
        "pivot_prev_year",
        "pivot_this_year",
        "prev_year_high",
        "weeks_since_cross",
    ]

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when pivots descend and a weekly close reclaims PYH."""
        recent_cross_weeks = self.coerce_param(params, "recent_cross_weeks", int)
        daily = self.prepare_candles(candles)
        if daily.empty or "timestamp" not in daily.columns:
            return None

        # Three consecutive yearly CPRs are required: this year, last year and two
        # years ago (each derived from the year before it). ``yearly_cpr`` therefore
        # needs at least four calendar years of history to return three usable rows.
        cpr = yearly_cpr(daily)
        if len(cpr) < 3:
            return None

        latest_year = int(pd.to_datetime(daily["timestamp"].iloc[-1]).year)
        by_year = {int(row["year"]): row for row in cpr.to_dict("records")}
        this_year = by_year.get(latest_year)
        prev_year = by_year.get(latest_year - 1)
        two_years_ago = by_year.get(latest_year - 2)
        if this_year is None or prev_year is None or two_years_ago is None:
            return None

        pivot_this_year = float(this_year["pivot"])
        pivot_prev_year = float(prev_year["pivot"])
        pivot_2y_ago = float(two_years_ago["pivot"])
        # Rule 1: the yearly central pivots must step strictly DOWN over three years.
        if not (pivot_2y_ago > pivot_prev_year > pivot_this_year):
            return None

        # The "previous year high" is the High that feeds THIS year's CPR — i.e. the
        # prior calendar year's High — which the price must reclaim to reverse.
        prev_year_high = float(this_year["prev_year_high"])

        # Rule 2: a weekly close recently up-crossed the previous-year high, and the
        # latest weekly close is still at/above it (the reclaim is holding).
        weekly = resample_to_weekly(daily)
        weekly_closes = weekly["close"].astype(float).to_numpy() if not weekly.empty else []
        if len(weekly_closes) < 2 or float(weekly_closes[-1]) < prev_year_high:
            return None

        weeks_since_cross = self._weeks_since_reclaim(
            weekly_closes, prev_year_high, recent_cross_weeks
        )
        if weeks_since_cross is None:
            return None

        latest = daily.iloc[-1]
        latest_weekly_close = float(weekly_closes[-1])
        reason = (
            f"Yearly CPR pivots descending ({pivot_2y_ago:.2f} > {pivot_prev_year:.2f} > "
            f"{pivot_this_year:.2f}); weekly close {latest_weekly_close:.2f} reclaimed the "
            f"previous-year high {prev_year_high:.2f} {weeks_since_cross} week(s) ago."
        )
        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": latest.get("timestamp", ""),
            "close": float(latest["close"]),
            "pivot_2y_ago": pivot_2y_ago,
            "pivot_prev_year": pivot_prev_year,
            "pivot_this_year": pivot_this_year,
            "prev_year_high": prev_year_high,
            "weeks_since_cross": weeks_since_cross,
            "reason": reason,
            "provenance": self.build_provenance(
                triggered_rules=[
                    "yearly_pivots_descending",
                    "weekly_close_crossed_prev_year_high",
                ],
                indicator_values={
                    "pivot_2y_ago": pivot_2y_ago,
                    "pivot_prev_year": pivot_prev_year,
                    "pivot_this_year": pivot_this_year,
                    "prev_year_high": prev_year_high,
                    "latest_weekly_close": latest_weekly_close,
                    "weeks_since_cross": weeks_since_cross,
                },
            ),
        }

    @staticmethod
    def _weeks_since_reclaim(
        weekly_closes,
        prev_year_high: float,
        recent_cross_weeks: int,
    ) -> int | None:
        """Weeks since the most recent up-cross of PYH, or ``None`` if not recent.

        Scans only the last ``recent_cross_weeks`` weekly candles for a close that
        moved from below the previous-year high to at/above it (``close[i-1] < PYH
        <= close[i]``). Returns 0 when that reclaim is the latest weekly candle,
        1 for the week before, and so on — or ``None`` when no reclaim happened
        inside the window.
        """
        count = len(weekly_closes)
        earliest = max(1, count - recent_cross_weeks)
        for index in range(count - 1, earliest - 1, -1):
            crossed_up = (
                float(weekly_closes[index]) >= prev_year_high
                and float(weekly_closes[index - 1]) < prev_year_high
            )
            if crossed_up:
                return (count - 1) - index
        return None

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Weekly candles with the recent years' yearly CPR + previous-year-high lines."""
        daily = self.prepare_candles(candles)
        weekly = resample_to_weekly(daily)
        spec = candlestick_with_volume(weekly, title="Weekly candles + yearly CPR")
        if weekly.empty or daily.empty or "timestamp" not in daily.columns:
            return spec

        cpr = yearly_cpr(daily)
        if cpr.empty:
            return spec
        # Draw only the most recent few years so the pane stays readable.
        recent_rows = cpr.tail(3).to_dict("records")
        latest_year = int(pd.to_datetime(daily["timestamp"].iloc[-1]).year)
        this_year_rows = [row for row in recent_rows if int(row["year"]) == latest_year]
        prev_year_high = (
            float(this_year_rows[-1]["prev_year_high"]) if this_year_rows else None
        )
        add_cpr_overlay(spec, recent_rows, prev_year_high=prev_year_high, pane=0)
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases (kept for tests that import the module).
# ---------------------------------------------------------------------------

_scanner = CprYearlyReversal()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
