"""Tests for the technical-indicator helpers.

Two layers are tested:
1. The pure-pandas `_<name>_fallback` functions — pinned with exact values so
   the maths is verified deterministically regardless of which optional
   library (TA-Lib / pandas_ta) happens to be installed.
2. The public dispatchers — checked for the correct output schema, plus an
   agreement check that the library-backed path matches the fallback where the
   maths is expected to be identical (Bollinger Bands).
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from backend.indicators import (
    _bollinger_bands_fallback,
    _build_heikin_ashi_fallback,
    _momentum_fallback,
    _stochastic_fallback,
    _supertrend_fallback,
    bollinger_bands,
    build_heikin_ashi,
    bullish_knoxville_divergence,
    bullish_knoxville_divergences,
    ema,
    major_levels,
    momentum,
    pivot_highs,
    pivot_lows,
    prepare_ohlc,
    rank_levels,
    resample_to_weekly,
    rsi,
    sma,
    stochastic,
    supertrend,
    yearly_cpr,
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


def test_momentum_fallback_is_close_difference_over_period():
    close = pd.Series([100.0, 102.0, 99.0, 108.0, 111.0])

    result = _momentum_fallback(close, period=3)

    assert result.iloc[:3].isna().all()
    assert result.iloc[3] == pytest.approx(8.0)
    assert result.iloc[4] == pytest.approx(9.0)


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
    assert len(rsi(frame["close"], 14)) == len(frame)
    assert len(momentum(frame["close"], 20)) == len(frame)


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


# ---------------------------------------------------------------------------
# prepare_ohlc — the public name of the boundary cleaner used by BaseScanner
# ---------------------------------------------------------------------------


def test_prepare_ohlc_sorts_and_drops_duplicate_timestamps():
    # Out-of-order rows + one duplicate timestamp. After prep the frame should
    # be sorted oldest-to-newest with each day appearing exactly once.
    raw = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-03", "2026-01-01", "2026-01-02", "2026-01-02"]),
        "open": [10.0, 9.0, 11.0, 11.5],
        "high": [11.0, 10.0, 12.0, 12.5],
        "low": [9.0, 8.0, 10.0, 10.5],
        "close": [10.5, 9.5, 11.5, 11.7],
    })
    prepared = prepare_ohlc(raw)
    assert list(prepared["timestamp"].dt.day) == [1, 2, 3]


def test_prepare_ohlc_coerces_string_prices_to_numeric():
    # API/CSV data sometimes lands as strings. The helper must turn them into
    # numbers so indicator math does not fail with a TypeError. (pd.to_numeric
    # may return either int or float depending on the input; either is fine.)
    raw = pd.DataFrame({
        "timestamp": pd.to_datetime(["2026-01-01", "2026-01-02"]),
        "open": ["10", "11"],
        "high": ["12", "13"],
        "low": ["9", "10"],
        "close": ["11", "12"],
    })
    prepared = prepare_ohlc(raw)
    assert pd.api.types.is_numeric_dtype(prepared["close"])
    assert prepared["close"].tolist() == [11, 12]


def test_prepare_ohlc_returns_empty_frame_when_input_is_empty():
    empty = pd.DataFrame({"open": [], "high": [], "low": [], "close": []})
    prepared = prepare_ohlc(empty)
    assert prepared.empty


# ---------------------------------------------------------------------------
# pivot_lows — vectorized confirmed-pivot detection
# ---------------------------------------------------------------------------


def test_pivot_lows_flags_only_confirmed_lows():
    # A V-shaped dip at index 3 should be detected as a pivot low.
    lows = pd.Series([5.0, 4.0, 3.0, 1.0, 2.5, 3.5, 4.5])
    mask = pivot_lows(lows, left=2, right=2)
    # Index 3 is lower than both its 2-bar neighbors on each side.
    assert mask.tolist() == [False, False, False, True, False, False, False]


def test_pivot_lows_does_not_mark_last_right_candles():
    # The last `right` candles cannot be confirmed pivots because they have no
    # future bars yet. Even a clear-looking minimum at the end stays False.
    lows = pd.Series([5.0, 4.0, 3.0, 2.0, 1.0])
    mask = pivot_lows(lows, left=1, right=1)
    # Index 4 is the global minimum but has no future bar → not confirmed.
    assert mask.iloc[-1] is False or bool(mask.iloc[-1]) is False


def test_pivot_lows_handles_nan_inputs_without_raising():
    # Real candle data sometimes has NaN lows (warm-up rows, parser issues).
    # The helper must accept them, mark them False, and not crash.
    lows = pd.Series([float("nan"), 5.0, 3.0, 5.0, float("nan")])
    mask = pivot_lows(lows, left=1, right=1)
    assert mask.iloc[0] is False or bool(mask.iloc[0]) is False
    # The interior candle at index 2 has lower neighbors on both sides.
    assert bool(mask.iloc[2]) is True


# ---------------------------------------------------------------------------
# pivot_highs — mirror image of pivot_lows
# ---------------------------------------------------------------------------


def test_pivot_highs_flags_only_confirmed_highs():
    # An inverted-V peak at index 3 should be detected as a pivot high.
    highs = pd.Series([1.0, 2.0, 3.0, 5.0, 3.5, 2.5, 1.5])
    mask = pivot_highs(highs, left=2, right=2)
    # Index 3 is higher than both its 2-bar neighbors on each side.
    assert mask.tolist() == [False, False, False, True, False, False, False]


def test_pivot_highs_does_not_mark_last_right_candles():
    # The last `right` candles cannot be confirmed (no future bars), even the
    # global maximum at the very end.
    highs = pd.Series([1.0, 2.0, 3.0, 4.0, 5.0])
    mask = pivot_highs(highs, left=1, right=1)
    assert bool(mask.iloc[-1]) is False


def test_pivot_highs_handles_nan_inputs_without_raising():
    highs = pd.Series([float("nan"), 1.0, 3.0, 1.0, float("nan")])
    mask = pivot_highs(highs, left=1, right=1)
    assert bool(mask.iloc[0]) is False
    # Interior peak at index 2 sits above both neighbors → confirmed.
    assert bool(mask.iloc[2]) is True


# ---------------------------------------------------------------------------
# Knoxville Divergence — all matches plus the legacy recent-wrapper behavior
# ---------------------------------------------------------------------------


def _knoxville_candles(close_values: list[float]) -> pd.DataFrame:
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


def test_bullish_knoxville_divergences_returns_all_matches_and_wrapper_keeps_recency():
    frame = _knoxville_candles(
        [
            100.0, 100.0, 100.0, 100.0, 96.0,
            92.0, 96.0, 100.0, 95.0, 90.0,
            93.0, 110.0, 105.0, 100.0, 95.0,
            98.0, 101.0, 96.0, 93.0, 96.0,
            100.0,
        ]
    )

    all_divergences = bullish_knoxville_divergences(
        frame,
        rsi_period=3,
        momentum_period=3,
        bars_back=10,
        pivot_left=1,
        pivot_right=1,
        oversold=100.0,
    )

    assert [row["timestamp"].strftime("%Y-%m-%d") for row in all_divergences] == [
        "2026-01-10",
        "2026-01-19",
    ]
    assert [float(row["low"]) for row in all_divergences] == pytest.approx([89.5, 92.5])

    recent = bullish_knoxville_divergence(
        frame,
        rsi_period=3,
        momentum_period=3,
        bars_back=10,
        recency=3,
        pivot_left=1,
        pivot_right=1,
        oversold=100.0,
    )
    assert recent is not None
    assert recent["timestamp"].strftime("%Y-%m-%d") == "2026-01-19"

    stale = bullish_knoxville_divergence(
        frame,
        rsi_period=3,
        momentum_period=3,
        bars_back=10,
        recency=1,
        pivot_left=1,
        pivot_right=1,
        oversold=100.0,
    )
    assert stale is None


# ---------------------------------------------------------------------------
# major_levels — clustered multi-touch support/resistance
# ---------------------------------------------------------------------------


def _level_frame(lows: list[float], highs: list[float]) -> pd.DataFrame:
    """Build an OHLC frame from explicit low/high paths (open/close = midpoint)."""
    mid = [(lo + hi) / 2.0 for lo, hi in zip(lows, highs, strict=True)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2020-01-01", periods=len(lows), freq="D"),
            "open": mid,
            "high": highs,
            "low": lows,
            "close": mid,
            "volume": [1000.0] * len(lows),
        }
    )


def test_major_levels_clusters_repeated_touches_and_drops_one_offs():
    # Three V-dips at ~50 (a repeated support zone) plus a single isolated dip
    # at 80. With min_touches=3, only the ~50 cluster should survive.
    lows = [
        60.0, 55.0, 50.0, 55.0, 60.0,   # support pivot at idx 2 (~50)
        65.0, 55.0, 50.5, 55.0, 65.0,   # support pivot at idx 7 (~50.5)
        70.0, 60.0, 49.5, 60.0, 70.0,   # support pivot at idx 12 (~49.5)
        85.0, 80.0, 90.0,               # one-off dip at idx 16 (~80)
    ]
    highs = [v + 5.0 for v in lows]
    frame = _level_frame(lows, highs)

    levels = major_levels(frame, left=2, right=2, cluster_pct=3.0, min_touches=3)

    support_levels = [lvl for lvl in levels if lvl["kind"] in ("support", "both")]
    assert len(support_levels) == 1
    level = support_levels[0]
    # The surviving level sits in the ~50 zone and counts all three touches.
    assert 49.0 <= level["price"] <= 51.0
    assert level["touches"] == 3
    # The isolated 80 dip never reached min_touches, so it is absent.
    assert all(not (78.0 <= lvl["price"] <= 82.0) for lvl in levels)


def test_major_levels_returns_empty_when_too_short_to_confirm():
    # Three candles cannot confirm a left=2/right=2 pivot, so there are no levels.
    frame = _level_frame([10.0, 9.0, 10.0], [12.0, 11.0, 12.0])
    assert major_levels(frame, left=2, right=2, cluster_pct=2.0, min_touches=2) == []


# ---------------------------------------------------------------------------
# rank_levels — relevance scoring of support/resistance
# ---------------------------------------------------------------------------


def test_rank_levels_prefers_recent_near_price_level():
    # Price drifts 100 → 150 over 60 bars (last close ≈ 150). The level at 150 is
    # near price and freshly tested; the level at 100 was only touched long ago
    # and is now far away. Despite fewer "touches", 150 must rank first because
    # proximity + recency dominate.
    close = np.linspace(100.0, 150.0, 60)
    frame = pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=60, freq="D"),
            "open": close,
            "high": close + 1.0,
            "low": close - 1.0,
            "close": close,
            "volume": [1000.0] * 60,
        }
    )
    levels = [
        {"price": 150.0, "touches": 3, "kind": "support"},
        {"price": 100.0, "touches": 5, "kind": "support"},
    ]

    scored = rank_levels(frame, levels)

    assert len(scored) == 2
    # Sorted by relevance descending, the near/recent 150 level wins.
    assert scored[0]["price"] == 150.0
    assert scored[0]["relevance"] >= scored[1]["relevance"]
    # Every enrichment field is present and well-formed.
    for field in ("relevance", "components", "last_touch_bars_ago", "distance_pct", "flipped"):
        assert field in scored[0]
    assert 0.0 <= scored[0]["relevance"] <= 1.0


def test_rank_levels_empty_inputs_are_safe():
    frame = _level_frame([10.0, 9.0, 10.0], [12.0, 11.0, 12.0])
    assert rank_levels(frame, []) == []
    assert rank_levels(pd.DataFrame(), [{"price": 10.0, "touches": 2, "kind": "support"}]) == []


# ---------------------------------------------------------------------------
# resample_to_weekly — daily → weekly aggregation
# ---------------------------------------------------------------------------


def test_resample_to_weekly_aggregates_ohlcv_per_week():
    # Two trading weeks (Mon–Fri). Each weekly candle takes the first open, the
    # max high, the min low, the last close, and the summed volume.
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [
                    "2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04", "2024-01-05",
                    "2024-01-08", "2024-01-09", "2024-01-10", "2024-01-11", "2024-01-12",
                ]
            ),
            "open": [100, 104, 107, 102, 105, 108, 111, 113, 110, 112],
            "high": [105, 108, 110, 106, 109, 112, 115, 116, 114, 118],
            "low": [99, 103, 101, 100, 104, 107, 110, 109, 108, 111],
            "close": [104, 107, 102, 105, 108, 111, 113, 110, 112, 117],
            "volume": [10, 20, 30, 40, 50, 11, 22, 33, 44, 55],
        }
    )

    weekly = resample_to_weekly(frame)

    assert len(weekly) == 2
    week1 = weekly.iloc[0]
    assert week1["open"] == 100
    assert week1["high"] == 110
    assert week1["low"] == 99
    assert week1["close"] == 108
    assert week1["volume"] == 150
    week2 = weekly.iloc[1]
    assert week2["open"] == 108
    assert week2["high"] == 118
    assert week2["low"] == 107
    assert week2["close"] == 117
    assert week2["volume"] == 165


def test_resample_to_weekly_empty_frame_is_safe():
    assert resample_to_weekly(pd.DataFrame()).empty


def _year_candles(year: int, high: float, low: float, close: float) -> pd.DataFrame:
    """Three daily candles for one year whose max/min/last give high/low/close."""
    midpoint = (high + low) / 2.0
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(
                [f"{year}-02-01", f"{year}-07-01", f"{year}-12-15"]
            ),
            "open": [midpoint, close, close],
            "high": [high, close + 1.0, close + 1.0],
            "low": [low, close - 1.0, close - 1.0],
            # The chronologically last candle sets the year's close.
            "close": [midpoint, close, close],
            "volume": [1000.0, 1000.0, 1000.0],
        }
    )


def test_yearly_cpr_derives_each_years_levels_from_the_previous_year():
    # 2022 High/Low/Close = 320/280/290 (close is NOT the H/L midpoint, so TC != BC).
    frame = pd.concat(
        [_year_candles(2022, 320.0, 280.0, 290.0), _year_candles(2023, 210.0, 190.0, 200.0)],
        ignore_index=True,
    )

    cpr = yearly_cpr(frame)

    # Only 2023 has a preceding year in the data, so exactly one row is emitted,
    # and its levels come from 2022's High/Low/Close.
    assert list(cpr["year"]) == [2023]
    row = cpr.iloc[0]
    assert row["pivot"] == pytest.approx((320.0 + 280.0 + 290.0) / 3.0)
    assert row["bc"] == pytest.approx((320.0 + 280.0) / 2.0)
    assert row["tc"] == pytest.approx(2.0 * ((320.0 + 280.0 + 290.0) / 3.0) - (320.0 + 280.0) / 2.0)
    assert row["prev_year_high"] == pytest.approx(320.0)
    assert row["prev_year_low"] == pytest.approx(280.0)


def test_yearly_cpr_skips_a_year_whose_predecessor_is_missing():
    # 2021 is absent, so 2022 must NOT inherit its CPR from 2020 two years back.
    frame = pd.concat(
        [_year_candles(2020, 300.0, 280.0, 290.0), _year_candles(2022, 210.0, 190.0, 200.0)],
        ignore_index=True,
    )

    assert yearly_cpr(frame).empty


def test_yearly_cpr_short_history_returns_the_standard_empty_shape():
    result = yearly_cpr(_year_candles(2024, 110.0, 90.0, 100.0))

    assert result.empty
    assert list(result.columns) == ["year", "pivot", "tc", "bc", "prev_year_high", "prev_year_low"]
