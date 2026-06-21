"""Unit tests for the pure RANK-002 scoring math.

These tests avoid Streamlit, SQLAlchemy, and Dhan entirely. They describe the
small mathematical building blocks first, so the implementation can stay simple:
turn messy inputs into finite 0-100 component scores, or return ``None``/NaN so
the model layer can drop that component and renormalize the remaining weights.
"""

from __future__ import annotations

import math

import numpy as np
import pandas as pd
import pytest

from backend.scoring.components import (
    NEUTRAL_SCORE,
    cross_sectional,
    freshness_score_absolute,
    liquidity_raw,
    risk_score_absolute,
    technical_raw,
)


def test_cross_sectional_min_max_normalizes_present_values():
    scores = cross_sectional(pd.Series([10, 20, 30]))

    assert scores.tolist() == [0.0, 50.0, 100.0]


def test_cross_sectional_degenerate_distribution_is_neutral_for_present_values():
    scores = cross_sectional(pd.Series([7, 7, np.nan]))

    assert scores.iloc[0] == NEUTRAL_SCORE
    assert scores.iloc[1] == NEUTRAL_SCORE
    assert math.isnan(scores.iloc[2])


def test_cross_sectional_treats_bad_and_infinite_inputs_as_missing():
    scores = cross_sectional(pd.Series(["5", "bad", np.inf, -np.inf]))

    assert scores.iloc[0] == NEUTRAL_SCORE
    assert math.isnan(scores.iloc[1])
    assert math.isnan(scores.iloc[2])
    assert math.isnan(scores.iloc[3])


def test_liquidity_raw_uses_trailing_mean_traded_value():
    candles = pd.DataFrame(
        {
            "open": [9, 10, 11],
            "high": [11, 12, 13],
            "low": [8, 9, 10],
            "close": [10, 11, 12],
            "volume": [100, 200, 300],
        }
    )

    assert liquidity_raw(candles, window=2) == pytest.approx(((11 * 200) + (12 * 300)) / 2)


def test_liquidity_raw_returns_none_when_inputs_are_missing_or_too_short():
    candles = pd.DataFrame(
        {
            "open": [9],
            "high": [11],
            "low": [8],
            "close": [10],
            "volume": [100],
        }
    )

    assert liquidity_raw(candles, window=2) is None
    assert liquidity_raw(candles.drop(columns=["volume"]), window=1) is None


def test_risk_score_absolute_uses_trailing_log_return_volatility():
    candles = pd.DataFrame(
        {
            "open": [99, 101, 100, 102],
            "high": [101, 103, 102, 104],
            "low": [98, 100, 99, 101],
            "close": [100, 102, 101, 103],
        }
    )
    returns = np.log(pd.Series([100, 102, 101, 103]) / pd.Series([100, 102, 101, 103]).shift(1)).dropna()
    sigma = float(returns.std(ddof=0))
    expected = 100.0 * max(0.0, min(1.0, 1.0 - sigma / 0.05))

    assert risk_score_absolute(candles, window=4, vol_cap=0.05) == pytest.approx(expected)


def test_risk_score_absolute_returns_none_for_short_or_invalid_inputs():
    candles = pd.DataFrame(
        {
            "open": [99],
            "high": [101],
            "low": [98],
            "close": [100],
        }
    )

    assert risk_score_absolute(candles, window=2, vol_cap=0.05) is None
    assert risk_score_absolute(candles, window=1, vol_cap=0.05) is None
    assert risk_score_absolute(candles, window=2, vol_cap=0) is None


def test_freshness_score_absolute_uses_halflife_and_clamps_future_dates():
    assert freshness_score_absolute(0, halflife_days=5) == 100.0
    assert freshness_score_absolute(5, halflife_days=5) == 50.0
    assert freshness_score_absolute(-3, halflife_days=5) == 100.0
    assert freshness_score_absolute(None, halflife_days=5) is None
    assert freshness_score_absolute(1, halflife_days=0) is None


def test_technical_raw_uses_confidence_first_then_known_strength_fields():
    assert technical_raw({"confidence": "8"}) == 8.0
    assert technical_raw({"confidence": "bad", "pct_below_basis": 0.18}) == 0.18
    assert technical_raw({"bb_distance_pct": -0.03}) == 0.03
    assert technical_raw({"proximity_pct_at_signal": 0.01}) == -0.01
    assert technical_raw({"reason": "no numeric strength"}) is None
