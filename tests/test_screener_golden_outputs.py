"""Golden-file (snapshot) regression tests for the deterministic screeners (TEST-001).

What a golden test is
---------------------
Each case runs one screener over a tiny, fixed set of synthetic candles and compares its
*entire* normalized output against a checked-in JSON snapshot under
``tests/golden/screeners/``. If a code change alters a screener's output — a rating, a
``reason`` string, or any indicator column — the snapshot no longer matches and the test
fails. That is the whole point: catch unintended **output drift**, including drift in the
underlying indicator math (Bollinger / EMA / RSI / SuperTrend), before it ships.

Important properties
--------------------
- These tests pin *current* behavior, not correctness. A golden test freezes whatever the
  screener produces today; it does not independently prove the signal is "right".
- Fully offline and deterministic: a ``FakeDataLoader`` supplies fixed candle frames (no
  Dhan / Streamlit / LLM / network), and floats are rounded to 6 decimals. Determinism
  relies on the numpy/pandas versions pinned in ``constraints.txt``.

Regenerating the snapshots
--------------------------
When you change a screener *on purpose*, its golden file must be refreshed. Run::

    UPDATE_GOLDEN=1 python -m pytest tests/test_screener_golden_outputs.py

That rewrites every snapshot from the current output (and skips the assertions). **Review
the resulting JSON diff** to confirm the change is intentional, commit it, then run the
suite again without the flag to verify it passes.
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime
from numbers import Integral, Real
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from screeners import (
    bollinger_band_reversal,
    bollinger_lower_band,
    cpr_yearly,
    envelope,
    envelope_knoxville_buy,
    green_candles_20pct_up,
    heikin_ashi_supertrend,
    stochastic_swing,
    week52_low_ceyhun,
)

GOLDEN_DIR = Path(__file__).parent / "golden" / "screeners"


class FakeDataLoader:
    """Small offline replacement for the real Dhan-backed daily data loader.

    Golden tests should prove screener behavior, not network behavior. This fake
    returns the exact candle DataFrames supplied by the test case and exposes the
    same summary attributes that the production screeners expect from a loader.
    """

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.last_failures = []
        self.last_cache_hits = 0
        self.last_cache_misses = len(frames)

    def load_universe_history(
        self,
        universe_df,
        start_date,
        end_date,
        max_symbols=None,
        force_refresh=False,
        progress_callback=None,
    ):
        """Return deterministic candle frames using the real loader's shape."""
        selected = dict(self.frames)
        if max_symbols is not None:
            # Keep the fake faithful to the real loader: max_symbols trims the
            # ordered scan set before the screener sees any candles.
            selected = dict(list(selected.items())[: int(max_symbols)])

        if progress_callback is not None:
            # Exercise progress callbacks without depending on API calls, cache
            # files, or timing. The callback receives the same simple counters as
            # the real loader.
            total = len(selected)
            for index, symbol in enumerate(selected, start=1):
                progress_callback(index, total, symbol)

        return SimpleNamespace(
            frames=selected,
            failures=[],
            cache_hits=0,
            cache_misses=len(selected),
        )


@dataclass(frozen=True)
class GoldenCase:
    """Everything needed to run one screener and compare its exact output."""

    key: str
    run: Callable[[pd.DataFrame, FakeDataLoader, dict], pd.DataFrame]
    universe_symbols: list[str]
    frames: dict[str, pd.DataFrame]
    params: dict


def _universe_for(symbols: list[str]) -> pd.DataFrame:
    """Build the tiny universe table required by the scanner/loader contract."""
    return pd.DataFrame(
        {
            "symbol": symbols,
            "security_id": [str(index) for index, _symbol in enumerate(symbols, start=1)],
            "exchange_segment": ["NSE_EQ"] * len(symbols),
            "instrument_type": ["EQUITY"] * len(symbols),
            "mapping_status": ["mapped"] * len(symbols),
        }
    )


def _flat_candles(close_values: list[float]) -> pd.DataFrame:
    """Create easy-to-read OHLC candles where open and close are identical."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": close_values,
            "high": [value + 1.0 for value in close_values],
            "low": [value - 1.0 for value in close_values],
            "close": close_values,
            "volume": [1000.0] * len(close_values),
        }
    )


def _bollinger_candles(
    open_values: list[float],
    high_values: list[float],
    low_values: list[float],
    close_values: list[float],
) -> pd.DataFrame:
    """Create Bollinger fixtures where candle color and band pierces are explicit."""
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": open_values,
            "high": high_values,
            "low": low_values,
            "close": close_values,
            "volume": [1000.0] * len(close_values),
        }
    )


def _env_knox_candles(close_values: list[float]) -> pd.DataFrame:
    """Create compact Envelope + Knoxville candles from only a close path.

    The Envelope/Knoxville rule mostly reads close, low, RSI, and momentum. A
    fixed ``low = close - 0.5`` keeps the bullish-divergence pivot prices easy to
    audit by hand when a golden diff appears.
    """
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": [value - 0.25 for value in close_values],
            "high": [value + 1.0 for value in close_values],
            "low": [value - 0.5 for value in close_values],
            "close": close_values,
            "volume": [1000.0] * len(close_values),
        }
    )


def _env_knox_params() -> dict:
    """Use small indicator windows so the golden fixture stays readable."""
    params = dict(envelope_knoxville_buy.SCREENER["default_params"])
    params.update(
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 13),
            "ema_period": 5,
            "percent": 10.0,
            "exponential": False,
            "env_proximity_pct": 0.01,
            "rsi_period": 3,
            "momentum_period": 3,
            "divergence_bars_back": 10,
            "signal_recency_bars": 10,
            "pivot_left": 1,
            "pivot_right": 1,
            # This test is about output drift, so the RSI threshold is relaxed
            # enough that the price/momentum divergence is the interesting gate.
            "oversold": 95.0,
        }
    )
    return params


def _wick_candles(
    close_values: list[float],
    *,
    high_offsets: list[float] | None = None,
    low_values: list[float] | None = None,
) -> pd.DataFrame:
    """Create candles from a close path with optional per-bar highs/lows.

    Two TEST-004 fixtures need wicks that differ from the uniform close±1
    shape: the 52-week-low case pins exact rolling lows, and the Stochastic
    case inflates highs so the oscillator reads "oversold" (close near the
    bottom of the high-low range) while the close path — which feeds the
    EMA/SMA trend filters — barely moves.
    """
    highs = (
        [value + offset for value, offset in zip(close_values, high_offsets, strict=True)]
        if high_offsets is not None
        else [value + 1.0 for value in close_values]
    )
    lows = low_values if low_values is not None else [value - 1.0 for value in close_values]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": close_values,
            "high": highs,
            "low": lows,
            "close": close_values,
            "volume": [1000.0] * len(close_values),
        }
    )


def _stochastic_buy_candles() -> pd.DataFrame:
    """Engineer a Stochastic swing BUY: a pullback inside a fresh uptrend.

    The screener wants three things to line up on the *final* bar, which pull
    in different directions on synthetic data:
    1. close above the SMA(10) with a fresh (<= 7 day) EMA(5)/SMA(10) bullish
       cross — needs the close path to have recovered recently;
    2. slow %K crossing above %D on the final bar — needs %K depressed right
       up to the end;
    3. both previous-bar %K/%D below the oversold line — needs several
       consecutive low-oscillator bars while the trend stays intact.
    The shape: a long flat prefix (which pins EMA and SMA to exactly 100.0 on
    both the TA-Lib and pure-pandas backends), a dip to 85, a two-bar
    recovery, then a seven-bar drift with tall upper wicks (+6). The wicks
    push the rolling high-low range up so %K/%D drain low while the closes
    keep the EMA above the SMA, and the final pop to 104 fires the cross.
    """
    prefix = [100.0] * 30
    dip = [97.0, 94.0, 91.0, 88.0, 86.0, 85.0]
    recovery = [90.0, 97.0]
    plateau = [97.5, 98.0, 98.5, 99.0, 99.5, 100.0, 100.5]
    pop = [104.0]
    closes = prefix + dip + recovery + plateau + pop
    offsets = [1.0] * (len(prefix) + len(dip) + len(recovery))
    offsets += [6.0] * len(plateau) + [1.0] * len(pop)
    return _wick_candles(closes, high_offsets=offsets)


def _stochastic_sell_candles() -> pd.DataFrame:
    """Engineer the SELL mirror: a bounce inside a fresh downtrend.

    Exact price mirror of `_stochastic_buy_candles` around 100 (Codex review
    on TEST-004 asked for the short side to be pinned too — its stop/target
    math, triggered rules, and reason text are a separate production branch).
    The plateau candles carry tall LOWER wicks (-6) so the close sits near the
    top of the high-low range and %K/%D drain HIGH (above the mirrored
    overbought line at 55) while the closes keep the EMA below the SMA; the
    final drop to 96 fires the %K-below-%D cross.
    """
    prefix = [100.0] * 30
    rise = [103.0, 106.0, 109.0, 112.0, 114.0, 115.0]
    drop = [110.0, 103.0]
    plateau = [102.5, 102.0, 101.5, 101.0, 100.5, 100.0, 99.5]
    final_drop = [96.0]
    closes = prefix + rise + drop + plateau + final_drop
    low_offsets = [1.0] * (len(prefix) + len(rise) + len(drop))
    low_offsets += [6.0] * len(plateau) + [1.0] * len(final_drop)
    lows = [value - offset for value, offset in zip(closes, low_offsets, strict=True)]
    return _wick_candles(closes, low_values=lows)


def _cpr_year_candles(year: int, high: float, low: float, close: float) -> pd.DataFrame:
    """Three daily candles for one complete year giving the target H/L/C."""
    midpoint = (high + low) / 2.0
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime([f"{year}-02-07", f"{year}-06-06", f"{year}-12-12"]),
            "open": [midpoint, close, close],
            "high": [high, close + 1.0, close + 1.0],
            "low": [low, close - 1.0, close - 1.0],
            "close": [midpoint, close, close],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )


def _cpr_frame(
    yearly: dict[int, tuple[float, float, float]], weekly_closes: list[float]
) -> pd.DataFrame:
    """Complete prior years + 2025 weekly-spaced candles for the reclaim check."""
    parts = [_cpr_year_candles(year, high, low, close) for year, (high, low, close) in yearly.items()]
    fridays = pd.date_range("2025-01-10", periods=len(weekly_closes), freq="W-FRI")
    parts.append(
        pd.DataFrame(
            {
                "timestamp": fridays,
                "open": weekly_closes,
                "high": [value + 1.0 for value in weekly_closes],
                "low": [value - 1.0 for value in weekly_closes],
                "close": weekly_closes,
                "volume": [1000.0] * len(weekly_closes),
            }
        )
    )
    return pd.concat(parts, ignore_index=True)


def _golden_cases() -> list[GoldenCase]:
    """Define the deterministic screener snapshots (TEST-001 + TEST-004).

    TEST-001 pinned the four P0 screeners; TEST-004 extends coverage to every
    remaining *deterministic* screener. The two AI-assisted screeners are
    deliberately excluded from golden coverage: ``technical_analysis`` and
    ``sixty_seven_ka_funda`` produce rows only after a Claude-agent verdict,
    so their output is not a pure function of candles. Their deterministic
    pre-gates and agent plumbing are already regression-tested by
    ``tests/test_technical_analysis_agent.py`` and
    ``tests/test_sixty_seven_agent.py`` with faked agents.
    """
    # Heikin-Ashi + SuperTrend fixtures. Each symbol is engineered to a known outcome:
    #   BUY  -> flat at 10, then a 5->15 jump flips the HA close above SuperTrend (cross up).
    #   SELL -> flat at 20, then a 30->10 plunge flips the HA close below SuperTrend (cross down).
    #   HOLD -> perfectly flat: no cross, so no row (proves non-signals stay out of the golden).
    heikin_frames = {
        "BUY": _flat_candles([10.0] * 10 + [5.0, 15.0]),
        "SELL": _flat_candles([20.0] * 10 + [30.0, 10.0]),
        "HOLD": _flat_candles([10.0] * 12),
    }
    # Bollinger reversal fixtures (period=3). The final candle of each symbol is the test:
    #   BUY  -> dips below the lower band (low 8) and closes GREEN (11->12) = bullish reversal.
    #   SELL -> pierces above the upper band (high 22) and closes RED (19->18) = bearish reversal.
    #   HOLD -> flat candles never pierce a band -> no row.
    bollinger_frames = {
        "BUY": _bollinger_candles(
            open_values=[10.0, 10.0, 11.0],
            high_values=[11.0, 11.0, 12.0],
            low_values=[9.0, 9.0, 8.0],
            close_values=[10.0, 10.0, 12.0],
        ),
        "SELL": _bollinger_candles(
            open_values=[20.0, 20.0, 19.0],
            high_values=[21.0, 21.0, 22.0],
            low_values=[19.0, 19.0, 17.0],
            close_values=[20.0, 20.0, 18.0],
        ),
        "HOLD": _bollinger_candles(
            open_values=[10.0, 10.0, 10.0],
            high_values=[11.0, 11.0, 11.0],
            low_values=[9.0, 9.0, 9.0],
            close_values=[10.0, 10.0, 10.0],
        ),
    }
    # Envelope + Knoxville fixtures (small windows via _env_knox_params). Outcomes:
    #   BUY    -> dips, prints a bullish Knoxville divergence, then returns to the lower band.
    #   NO_KD  -> a plain straight drop: no momentum divergence -> no Knoxville -> no row.
    #   NO_ENV -> diverges but rebounds away from the lower band -> no row.
    envelope_frames = {
        "BUY": _env_knox_candles(
            [
                100.0,
                100.0,
                100.0,
                100.0,
                96.0,
                92.0,
                96.0,
                100.0,
                95.0,
                90.0,
                93.0,
                85.0,
                78.0,
            ]
        ),
        "NO_KD": _env_knox_candles(
            [100.0] * 11 + [90.0, 80.0]
        ),
        "NO_ENV": _env_knox_candles(
            [
                100.0,
                100.0,
                100.0,
                100.0,
                96.0,
                92.0,
                96.0,
                100.0,
                95.0,
                90.0,
                93.0,
                97.0,
                100.0,
            ]
        ),
    }

    return [
        GoldenCase(
            key="bollinger_band_reversal",
            run=bollinger_band_reversal.run,
            universe_symbols=["BUY", "SELL", "HOLD"],
            frames=bollinger_frames,
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 3),
                "max_symbols": 10,
                "force_refresh": False,
                "period": 3,
                "std_multiplier": 2.0,
            },
        ),
        GoldenCase(
            key="heikin_ashi_supertrend",
            run=heikin_ashi_supertrend.run,
            universe_symbols=["BUY", "SELL", "HOLD"],
            frames=heikin_frames,
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 12),
                "max_symbols": 10,
                "force_refresh": False,
                "atr_period": 3,
                "multiplier": 1.0,
            },
        ),
        GoldenCase(
            key="envelope_knoxville_buy",
            run=envelope_knoxville_buy.run,
            universe_symbols=["BUY", "NO_KD", "NO_ENV"],
            frames=envelope_frames,
            params=_env_knox_params(),
        ),
        # CPR Yearly Reversal fixtures. Outcomes:
        #   HIT  -> yearly pivots step down 300>200>100 and the 2025 weekly close
        #           reclaims 2024's high (110) -> one BUY row.
        #   MISS -> ascending pivots 100<200<300 (uptrend) -> no row (proves
        #           non-signals stay out of the golden snapshot).
        GoldenCase(
            key="cpr_yearly",
            run=cpr_yearly.run,
            universe_symbols=["HIT", "MISS"],
            frames={
                "HIT": _cpr_frame(
                    {
                        2022: (310.0, 290.0, 300.0),
                        2023: (210.0, 190.0, 200.0),
                        2024: (110.0, 90.0, 100.0),
                    },
                    [95.0, 98.0, 100.0, 104.0, 108.0, 120.0],
                ),
                "MISS": _cpr_frame(
                    {
                        2022: (110.0, 90.0, 100.0),
                        2023: (210.0, 190.0, 200.0),
                        2024: (310.0, 290.0, 300.0),
                    },
                    [295.0, 298.0, 300.0, 305.0, 312.0, 320.0],
                ),
            },
            params={
                "start_date": date(2022, 1, 1),
                "end_date": date(2025, 3, 1),
                "max_symbols": 10,
                "force_refresh": False,
                "recent_cross_weeks": 4,
            },
        ),
        # Bollinger Lower Band fixtures (period=3, std=2.0, 1% proximity). Outcomes:
        #   ON_BAND -> a perfectly flat tape collapses the bands onto the price, so
        #              the close sits ON the lower band -> BUY at distance 0.
        #   NEAR    -> the close eases to 9.9, within 1% of the lower band -> BUY.
        #   ABOVE   -> the close pops to 12, far above the lower band -> no row.
        GoldenCase(
            key="bollinger_lower_band",
            run=bollinger_lower_band.run,
            universe_symbols=["ON_BAND", "NEAR", "ABOVE"],
            frames={
                "ON_BAND": _flat_candles([10.0, 10.0, 10.0]),
                "NEAR": _flat_candles([10.0, 10.0, 9.9]),
                "ABOVE": _flat_candles([10.0, 10.0, 12.0]),
            },
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 3),
                "max_symbols": 10,
                "force_refresh": False,
                "bb_period": 3,
                "bb_std": 2.0,
                "bb_proximity_pct": 0.01,
            },
        ),
        # Envelope fixtures (5-period SMA basis, 10% bands — SMA instead of the
        # default EMA so the basis math is identical on both indicator backends
        # and auditable by hand). Outcomes:
        #   BUY   -> SMA5 = 97, lower band = 87.3, close 85 is below it -> BUY.
        #   HOLD  -> SMA5 = 99, lower band = 89.1, close 95 stays above -> no row.
        #   SHORT -> fewer rows than the basis period -> warm-up skip, no row.
        GoldenCase(
            key="envelope",
            run=envelope.run,
            universe_symbols=["BUY", "HOLD", "SHORT"],
            frames={
                "BUY": _flat_candles([100.0, 100.0, 100.0, 100.0, 85.0]),
                "HOLD": _flat_candles([100.0, 100.0, 100.0, 100.0, 95.0]),
                "SHORT": _flat_candles([100.0, 100.0, 100.0]),
            },
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 5),
                "max_symbols": 10,
                "force_refresh": False,
                "ema_period": 5,
                "percent": 10.0,
                "exponential": False,
            },
        ),
        # Green-candles run fixtures (pure pandas, default params). Outcomes:
        #   BUY       -> a red candle ends the lookback, then three greens whose
        #                low->high span (99 -> 122) is +23.2% -> BUY, run length 3.
        #   SMALL_RUN -> three greens but only a ~4% span -> below 20% -> no row.
        #   RED_LAST  -> a strong two-green run capped by a red candle -> no row
        #                (the run must be alive on the latest bar).
        # `_bollinger_candles` is a generic OHLC builder despite its name; the
        # green/red candle colors need explicit opens, which it provides.
        GoldenCase(
            key="green_candles_20pct_up",
            run=green_candles_20pct_up.run,
            universe_symbols=["BUY", "SMALL_RUN", "RED_LAST"],
            frames={
                "BUY": _bollinger_candles(
                    open_values=[101.0, 100.0, 105.0, 112.0],
                    high_values=[102.0, 106.0, 113.0, 122.0],
                    low_values=[99.5, 99.0, 104.0, 111.0],
                    close_values=[100.0, 105.0, 112.0, 121.0],
                ),
                "SMALL_RUN": _bollinger_candles(
                    open_values=[100.0, 101.0, 102.0],
                    high_values=[101.5, 102.5, 104.0],
                    low_values=[99.8, 100.8, 101.8],
                    close_values=[101.0, 102.0, 103.5],
                ),
                "RED_LAST": _bollinger_candles(
                    open_values=[100.0, 110.0, 125.0],
                    high_values=[111.0, 126.0, 126.0],
                    low_values=[99.0, 109.0, 118.0],
                    close_values=[110.0, 125.0, 120.0],
                ),
            },
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 4),
                "max_symbols": 10,
                "force_refresh": False,
                "max_run": 20,
                "gain_threshold_pct": 20.0,
            },
        ),
        # 52-week-low fixtures (window shrunk to 5 bars, recent window 3, 2%
        # tolerance). Outcomes:
        #   BUY   -> lows grind down to 96, then a recent close (97.9) comes
        #            within 2% of the rolling low -> BUY, tightest 2 days ago.
        #   FAR   -> same descent but the recent closes rebound >2% above the
        #            rolling low -> no row.
        #   SHORT -> fewer rows than the rolling window -> warm-up skip.
        GoldenCase(
            key="week52_low_ceyhun",
            run=week52_low_ceyhun.run,
            universe_symbols=["BUY", "FAR", "SHORT"],
            frames={
                "BUY": _wick_candles(
                    [100.0, 99.0, 98.0, 97.0, 97.0, 97.9, 99.0, 100.0],
                    low_values=[99.0, 98.0, 97.0, 96.0, 96.5, 97.0, 98.0, 99.0],
                ),
                "FAR": _wick_candles(
                    [100.0, 99.0, 98.0, 97.0, 100.0, 101.0, 102.0, 103.0],
                    low_values=[99.0, 98.0, 97.0, 96.0, 99.0, 100.0, 101.0, 102.0],
                ),
                "SHORT": _flat_candles([100.0, 99.0, 98.0]),
            },
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 1, 8),
                "max_symbols": 10,
                "force_refresh": False,
                "window_bars": 5,
                "recent_window_bars": 3,
                "proximity_pct": 0.02,
            },
        ),
        # Stochastic swing fixtures (SMA shrunk to 10 bars; oversold/overbought
        # relaxed to the symmetric 45/55 pair so the compact fixtures stay
        # readable — the fresh-cross + trend alignment is the interesting gate
        # here, mirroring how the Envelope + Knoxville case relaxes its RSI
        # threshold). Outcomes:
        #   BUY      -> the engineered pullback-in-fresh-uptrend described in
        #               `_stochastic_buy_candles` -> one BUY row.
        #   SELL     -> the exact price mirror (`_stochastic_sell_candles`):
        #               a bounce in a fresh downtrend -> one SELL row, pinning
        #               the short side's stop/target math and rule names.
        #   NO_ENTRY -> the same dip but price just flatlines at the bottom: no
        #               recovery, no fresh EMA/SMA cross -> no row.
        GoldenCase(
            key="stochastic_swing",
            run=stochastic_swing.run,
            universe_symbols=["BUY", "SELL", "NO_ENTRY"],
            frames={
                "BUY": _stochastic_buy_candles(),
                "SELL": _stochastic_sell_candles(),
                "NO_ENTRY": _flat_candles(
                    [100.0] * 30 + [97.0, 94.0, 91.0, 88.0, 86.0, 85.0] + [85.0] * 9
                ),
            },
            params={
                "start_date": date(2026, 1, 1),
                "end_date": date(2026, 2, 15),
                "max_symbols": 10,
                "force_refresh": False,
                "stoch_k": 5,
                "stoch_k_smoothing": 4,
                "stoch_d_smoothing": 3,
                "ema_period": 5,
                "sma_period": 10,
                "oversold": 45.0,
                "overbought": 55.0,
            },
        ),
    ]


def _normalize_value(value):
    """Convert pandas/numpy values into stable JSON-friendly Python values."""
    if value is None:
        return None
    if isinstance(value, pd.Timestamp):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Integral) and not isinstance(value, bool):
        return int(value)
    if isinstance(value, Real) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        return round(number, 6)
    # PROV-002 stores a ``provenance`` dict cell whose nested numbers need the same
    # rounding/NaN handling as flat columns, otherwise raw NumPy floats would vary
    # in the last decimals across platforms and break the snapshot non-portably.
    if isinstance(value, dict):
        return {str(key): _normalize_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_normalize_value(item) for item in value]
    return value


def _normalize_records(frame: pd.DataFrame) -> list[dict]:
    """Return exact, ordered records that can be compared with a JSON file."""
    return [
        {column: _normalize_value(value) for column, value in row.items()}
        for row in frame.to_dict("records")
    ]


def test_normalize_value_recurses_into_dict_and_list_cells():
    """PROV-002 adds a ``provenance`` dict cell, whose floats need the same rounding.

    Without recursion a dict/list cell would be returned verbatim, leaving raw
    NumPy floats that ``json.dump`` cannot serialize and that vary in the last
    decimals across platforms. Recursion makes nested values as stable as the
    flat columns the snapshots already pin.
    """
    cell = {
        "indicator_values": {"pct": np.float64(0.142857142857)},
        "triggered_rules": ["rule_a"],
        "nested_list": [np.int64(3), np.nan],
    }
    normalized = _normalize_value(cell)

    assert normalized["indicator_values"]["pct"] == round(0.142857142857, 6)
    assert isinstance(normalized["indicator_values"]["pct"], float)
    assert normalized["triggered_rules"] == ["rule_a"]
    assert normalized["nested_list"] == [3, None]
    json.dumps(normalized, allow_nan=False)


def _load_golden_records(key: str) -> list[dict]:
    """Load the checked-in snapshot for one screener."""
    path = GOLDEN_DIR / f"{key}.json"
    if not path.exists():
        pytest.fail(f"Golden snapshot is missing: {path}")
    with path.open(encoding="utf-8") as file:
        return json.load(file)


def _write_golden_records(key: str, records: list[dict]) -> None:
    """Overwrite one screener's snapshot. Used ONLY by the ``UPDATE_GOLDEN`` workflow.

    Writes pretty-printed JSON so the regenerated snapshot is easy to read in a diff.
    This never runs during a normal test (it is gated on the env var below), so a
    stray import or CI run can't silently rewrite the goldens.
    """
    path = GOLDEN_DIR / f"{key}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        json.dump(records, file, indent=2)
        file.write("\n")


@pytest.mark.parametrize("case", _golden_cases(), ids=lambda case: case.key)
def test_screener_output_matches_golden_snapshot(case: GoldenCase):
    """Important screeners should fail tests when their full output drifts.

    Set ``UPDATE_GOLDEN=1`` to rewrite the snapshots instead of asserting (see the
    module docstring) — use it after an *intentional* screener change, then review the diff.
    """
    result = case.run(
        _universe_for(case.universe_symbols),
        FakeDataLoader(case.frames),
        case.params,
    )
    records = _normalize_records(result)

    if os.environ.get("UPDATE_GOLDEN"):
        _write_golden_records(case.key, records)
        pytest.skip(
            f"Rewrote golden snapshot for {case.key}; rerun without UPDATE_GOLDEN to verify."
        )

    assert records == _load_golden_records(case.key)
