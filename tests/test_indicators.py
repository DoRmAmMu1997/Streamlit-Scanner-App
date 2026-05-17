from __future__ import annotations

"""Tests for the technical-indicator helpers.

Two layers are tested:
1. The pure-pandas `_<name>_fallback` functions — pinned with exact values so
   the maths is verified deterministically regardless of which optional
   library (TA-Lib / pandas_ta) happens to be installed.
2. The public dispatchers — checked for the correct output schema, plus an
   agreement check that the library-backed path matches the fallback where the
   maths is expected to be identical (Bollinger Bands).
"""

import numpy as np
import pandas as pd
import pytest

from backend.indicators import (
    _bollinger_bands_fallback,
    _build_heikin_ashi_fallback,
    _stochastic_fallback,
    _supertrend_fallback,
    bollinger_bands,
    build_heikin_ashi,
    ema,
    sma,
    stochastic,
    supertrend,
)


def _ohlc_frame(periods: int = 60) -> pd.DataFrame:
    """Return a small, well-formed daily OHLC frame for dispatcher schema tests."""
    close = np.linspace(100.0, 130.0, periods) + np.sin(np.arange(periods))
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D"),
            "open": close - 0.5,
            "high": close + 1.5,
            "low": close - 1.5,
            "close": close,
            "volume": [1000.0] * periods,
        }
    )


# ---------------------------------------------------------------------------
# Layer 1: pinned fallback maths
# ---------------------------------------------------------------------------


def test_heikin_ashi_fallback_uses_standard_formulas():
    # Two candles are enough to verify both parts of the HA formula:
    # the first candle seed and the second candle's recursive HA open.
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=2, freq="D"),
            "open": [10.0, 12.0],
            "high": [14.0, 16.0],
            "low": [8.0, 11.0],
            "close": [12.0, 15.0],
        }
    )

    result = _build_heikin_ashi_fallback(candles)

    # First HA close = (10 + 14 + 8 + 12) / 4 = 11; first HA open = (10 + 12) / 2 = 11.
    assert result.loc[0, "ha_close"] == pytest.approx(11.0)
    assert result.loc[0, "ha_open"] == pytest.approx(11.0)
    assert result.loc[0, "ha_high"] == pytest.approx(14.0)
    assert result.loc[0, "ha_low"] == pytest.approx(8.0)
    # Second HA open = (previous HA open 11 + previous HA close 11) / 2 = 11.
    assert result.loc[1, "ha_close"] == pytest.approx(13.5)
    assert result.loc[1, "ha_open"] == pytest.approx(11.0)
    assert result.loc[1, "ha_high"] == pytest.approx(16.0)
    assert result.loc[1, "ha_low"] == pytest.approx(11.0)


def test_supertrend_fallback_outputs_columns_and_detects_raw_crossover():
    # The final two candles are intentionally shaped as a raw cross from below
    # the SuperTrend line to above it. This mirrors the screener's BUY rule.
    close_values = [10.0] * 10 + [5.0, 15.0]
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": close_values,
            "high": [value + 1.0 for value in close_values],
            "low": [value - 1.0 for value in close_values],
            "close": close_values,
        }
    )

    result = _supertrend_fallback(candles, atr_period=3, multiplier=1.0)
    valid = result.dropna(subset=["supertrend"])
    previous = valid.iloc[-2]
    latest = valid.iloc[-1]

    assert {"atr", "supertrend", "supertrend_direction", "supertrend_color"}.issubset(result.columns)
    assert previous["close"] <= previous["supertrend"]
    assert latest["close"] > latest["supertrend"]


def test_bollinger_bands_fallback_uses_population_standard_deviation():
    # The first three closes are flat, so the first complete band has zero
    # width. The fourth close then confirms the ddof=0 population-std maths.
    close = pd.Series([10.0, 10.0, 10.0, 12.0])

    bands = _bollinger_bands_fallback(close, period=3, std_multiplier=2.0)

    assert bands.loc[2, "bb_middle"] == pytest.approx(10.0)
    assert bands.loc[2, "bb_upper"] == pytest.approx(10.0)
    assert bands.loc[2, "bb_lower"] == pytest.approx(10.0)
    assert bands.loc[3, "bb_middle"] == pytest.approx(10.6666666667)
    assert bands.loc[3, "bb_upper"] == pytest.approx(12.5522847498)
    assert bands.loc[3, "bb_lower"] == pytest.approx(8.7810485835)


def test_stochastic_fallback_matches_hand_computed_value():
    # With k_smoothing=1 and d_smoothing=1 the slow %K equals fast %K and
    # %D equals %K, so the final bar is hand-checkable.
    # Window for bar 3 = bars 1-3: lowest low = 10, highest high = 20, close = 15.
    # Fast %K = 100 * (15 - 10) / (20 - 10) = 50.
    high = pd.Series([18.0, 19.0, 20.0, 17.0])
    low = pd.Series([12.0, 11.0, 13.0, 10.0])
    close = pd.Series([15.0, 14.0, 16.0, 15.0])

    result = _stochastic_fallback(high, low, close, k_period=3, k_smoothing=1, d_smoothing=1)

    assert list(result.columns) == ["stoch_k", "stoch_d"]
    assert result.loc[3, "stoch_k"] == pytest.approx(50.0)
    assert result.loc[3, "stoch_d"] == pytest.approx(50.0)


# ---------------------------------------------------------------------------
# Layer 2: public dispatchers — schema + agreement
# ---------------------------------------------------------------------------


def test_public_indicators_return_expected_schema():
    frame = _ohlc_frame()

    ha = build_heikin_ashi(frame)
    assert {"ha_open", "ha_high", "ha_low", "ha_close"}.issubset(ha.columns)

    st_frame = supertrend(frame, atr_period=10, multiplier=2.0)
    assert {"atr", "supertrend", "supertrend_direction", "supertrend_color"}.issubset(st_frame.columns)

    bands = bollinger_bands(frame["close"], period=20, std_multiplier=2.0)
    assert list(bands.columns) == ["bb_middle", "bb_upper", "bb_lower"]

    stoch = stochastic(frame["high"], frame["low"], frame["close"])
    assert list(stoch.columns) == ["stoch_k", "stoch_d"]

    # EMA/SMA return a Series the same length as the input.
    assert len(ema(frame["close"], 5)) == len(frame)
    assert len(sma(frame["close"], 10)) == len(frame)


def test_public_heikin_ashi_close_is_mean_of_ohlc():
    # HA close = (O + H + L + C) / 4 is identical for pandas_ta and the
    # fallback, so this holds whichever backend the dispatcher picks.
    frame = _ohlc_frame(periods=12)
    ha = build_heikin_ashi(frame)
    expected = (frame["open"] + frame["high"] + frame["low"] + frame["close"]) / 4.0
    assert np.allclose(ha["ha_close"].to_numpy(), expected.to_numpy())


def test_public_bollinger_agrees_with_fallback():
    # TA-Lib's BBANDS (SMA middle, population std) should match the pandas
    # fallback within floating-point tolerance.
    close = pd.Series(np.linspace(100.0, 130.0, 80) + np.sin(np.arange(80)))
    public = bollinger_bands(close, period=20, std_multiplier=2.0)
    fallback = _bollinger_bands_fallback(close, period=20, std_multiplier=2.0)
    pd.testing.assert_frame_equal(public.dropna(), fallback.dropna(), rtol=1e-6, atol=1e-6)


def test_public_stochastic_stays_within_zero_to_hundred():
    frame = _ohlc_frame()
    stoch = stochastic(frame["high"], frame["low"], frame["close"])
    valid = stoch.dropna()
    assert not valid.empty
    # The Stochastic oscillator is bounded 0-100 by construction.
    assert (valid["stoch_k"] >= -1e-6).all() and (valid["stoch_k"] <= 100.0 + 1e-6).all()
    assert (valid["stoch_d"] >= -1e-6).all() and (valid["stoch_d"] <= 100.0 + 1e-6).all()
