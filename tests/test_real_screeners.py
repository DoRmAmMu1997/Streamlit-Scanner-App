from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from screeners import (
    bollinger_band_reversal,
    bollinger_knoxville_buy,
    ema200_14percent_below,
    heikin_ashi_supertrend,
    stochastic_swing,
    week52_low_ceyhun,
)


class FakeDataLoader:
    """Fake data loader that returns prebuilt candle frames instead of using Dhan."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        # Tests should verify screener logic only. Using a fake loader keeps API
        # credentials, network access, and cache files completely out of the test.
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
        selected = dict(self.frames)
        if max_symbols is not None:
            # Mirror the real loader's optional cap so tests can still exercise
            # the "scan only the first N symbols" path used by CLI callers,
            # even though the Streamlit UI no longer exposes this control.
            selected = dict(list(selected.items())[: int(max_symbols)])
        if progress_callback is not None:
            # Drive the callback once per "symbol" so tests can validate that
            # screeners forward it correctly without depending on the real
            # data loader's bookkeeping.
            total = len(selected)
            for index, symbol in enumerate(selected, start=1):
                progress_callback(index, total, symbol)
        return SimpleNamespace(frames=selected, failures=[], cache_hits=0, cache_misses=len(selected))


def _flat_candles(symbol_values: list[float]) -> pd.DataFrame:
    # Build simple candles where open and close are the same value. This makes it
    # easier to reason about SuperTrend tests because only the price path changes.
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(symbol_values), freq="D"),
            "open": symbol_values,
            "high": [value + 1.0 for value in symbol_values],
            "low": [value - 1.0 for value in symbol_values],
            "close": symbol_values,
            "volume": [1000.0] * len(symbol_values),
        }
    )


def _bollinger_candles(open_values, high_values, low_values, close_values) -> pd.DataFrame:
    # Bollinger tests need separate open/high/low/close values because the signal
    # depends on candle color plus whether high/low pierced a band.
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


def _knoxville_candles(*, with_divergence: bool = True, near_lower_band: bool = True) -> pd.DataFrame:
    """Build compact candles for the BUY-only Bollinger + Knoxville screener."""
    # These values are intentionally tiny compared with the real 200-bar setup.
    # `_knoxville_params()` below shrinks the indicator periods so this fixture
    # can test the same logic without hundreds of repetitive candles.
    close_values = [
        100.0,
        100.0,
        100.0,
        100.0,
        100.0,
        92.0,
        96.0,
        100.0 if with_divergence else 92.0,
        99.0,
        100.0,
        90.0,
        94.0,
        91.0,
        88.0,
        88.0,
        88.0,
        88.0,
        88.0,
        88.0,
    ]
    if not with_divergence:
        # A flat close path keeps RSI/Momentum from forming the lower-low /
        # higher-momentum-low disagreement needed for Knoxville.
        close_values = [88.0] * len(close_values)
    if not near_lower_band:
        # Lift the final closes far away from the lower Bollinger Band while
        # preserving the earlier divergence pivots. That isolates the BB filter.
        close_values[-5:] = [88.0, 110.0, 110.0, 110.0, 110.0]

    low_values = [value - 0.5 for value in close_values]
    if with_divergence:
        # Force clear pivot lows. The latest pivot is lower in price but has a
        # higher momentum reading than the earlier pivot when with_divergence=True.
        low_values[10] = 89.0
        low_values[13] = 87.0

    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(close_values), freq="D"),
            "open": [value + 0.25 for value in close_values],
            "high": [value + 1.0 for value in close_values],
            "low": low_values,
            "close": close_values,
            "volume": [1000.0] * len(close_values),
        }
    )


def _knoxville_params() -> dict:
    params = dict(bollinger_knoxville_buy.SCREENER["default_params"])
    params.update(
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 19),
            # Short periods make the synthetic candles easy to reason about.
            # The production defaults remain BB200, RSI21, Momentum20.
            "bb_period": 5,
            "bb_std": 2.5,
            "rsi_period": 3,
            "momentum_period": 3,
            "divergence_bars_back": 10,
            "signal_recency_bars": 10,
            "pivot_left": 1,
            "pivot_right": 1,
            "oversold": 30.0,
        }
    )
    return params


def _universe() -> pd.DataFrame:
    # The screeners only need enough universe columns for the loader contract.
    # These rows are fake symbols, not real market instruments.
    return pd.DataFrame(
        {
            "symbol": ["BUY", "SELL", "HOLD"],
            "security_id": ["1", "2", "3"],
            "exchange_segment": ["NSE_EQ", "NSE_EQ", "NSE_EQ"],
            "instrument_type": ["EQUITY", "EQUITY", "EQUITY"],
            "mapping_status": ["mapped", "mapped", "mapped"],
        }
    )


def test_heikin_ashi_supertrend_screener_returns_buy_and_sell_only():
    # BUY and SELL are shaped to create fresh latest-candle crossovers. HOLD has
    # enough candles but no fresh crossover, so it should be omitted.
    frames = {
        "BUY": _flat_candles([10.0] * 10 + [5.0, 15.0]),
        "SELL": _flat_candles([20.0] * 10 + [30.0, 10.0]),
        "HOLD": _flat_candles([10.0] * 12),
    }
    params = {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 12),
        "max_symbols": 10,
        "force_refresh": False,
        "atr_period": 3,
        "multiplier": 1.0,
    }

    result = heikin_ashi_supertrend.run(_universe(), FakeDataLoader(frames), params)

    # The result table is a shortlist. It should contain only actionable BUY/SELL
    # rows, not every stock in the universe.
    ratings = result.set_index("symbol")["rating"].to_dict()
    assert ratings == {"BUY": "BUY", "SELL": "SELL"}
    assert "HOLD" not in result["symbol"].tolist()
    assert "previous_ha_close" in result.columns
    assert "supertrend" in result.columns


def test_heikin_ashi_supertrend_screener_tolerates_empty_and_short_frames():
    # Empty and too-short data should be skipped gracefully. They are common in
    # real API scans when a symbol is newly listed or the fetch failed upstream.
    frames = {
        "BUY": pd.DataFrame(),
        "SELL": _flat_candles([10.0, 11.0]),
    }

    result = heikin_ashi_supertrend.run(
        _universe(),
        FakeDataLoader(frames),
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 2),
            "max_symbols": 10,
            "force_refresh": False,
            "atr_period": 10,
            "multiplier": 2.0,
        },
    )

    assert result.empty
    # Even an empty result should preserve the schema expected by Streamlit.
    assert list(result.columns) == heikin_ashi_supertrend.RESULT_COLUMNS


def test_bollinger_band_reversal_screener_returns_buy_and_sell_only():
    # With period=3, the third candle is the first candle with complete bands.
    # These fixtures make that latest candle trigger BUY, SELL, or no signal.
    frames = {
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
    params = {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 3),
        "max_symbols": 10,
        "force_refresh": False,
        "period": 3,
        "std_multiplier": 2.0,
    }

    result = bollinger_band_reversal.run(_universe(), FakeDataLoader(frames), params)

    # HOLD confirms that a stock without the exact latest-candle reversal pattern
    # stays out of the shortlist.
    ratings = result.set_index("symbol")["rating"].to_dict()
    assert ratings == {"BUY": "BUY", "SELL": "SELL"}
    assert "HOLD" not in result["symbol"].tolist()
    assert {"bb_middle", "bb_upper", "bb_lower"}.issubset(result.columns)


def test_bollinger_band_reversal_screener_tolerates_empty_and_short_frames():
    # A one-candle frame cannot form a 20-candle Bollinger Band, so it should not
    # produce a row or raise an exception.
    frames = {
        "BUY": pd.DataFrame(),
        "SELL": _bollinger_candles([10.0], [11.0], [9.0], [10.0]),
    }

    result = bollinger_band_reversal.run(
        _universe(),
        FakeDataLoader(frames),
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 1),
            "max_symbols": 10,
            "force_refresh": False,
            "period": 20,
            "std_multiplier": 2.0,
        },
    )

    assert result.empty
    # Fixed columns make the UI predictable even when no symbols pass the scan.
    assert list(result.columns) == bollinger_band_reversal.RESULT_COLUMNS


def test_bollinger_knoxville_buy_screener_returns_buy_only_when_both_filters_pass():
    # BUY passes both filters. NO_KD is near the lower band but lacks Knoxville.
    # NO_BB has Knoxville but is no longer near the lower band. The screener
    # should only return the stock where both conditions are true.
    frames = {
        "BUY": _knoxville_candles(with_divergence=True, near_lower_band=True),
        "NO_KD": _knoxville_candles(with_divergence=False, near_lower_band=True),
        "NO_BB": _knoxville_candles(with_divergence=True, near_lower_band=False),
    }

    result = bollinger_knoxville_buy.run(_universe(), FakeDataLoader(frames), _knoxville_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["close"] <= row["bb_lower"] * 1.01
    assert row["rsi"] <= 30.0
    assert row["momentum"] > -10.0
    assert "Knoxville" in row["reason"]


def test_bollinger_knoxville_buy_screener_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _knoxville_candles().head(4),
    }

    result = bollinger_knoxville_buy.run(_universe(), FakeDataLoader(frames), _knoxville_params())

    assert result.empty
    assert list(result.columns) == bollinger_knoxville_buy.RESULT_COLUMNS


def _synthetic_daily(periods: int) -> pd.DataFrame:
    # A gentle uptrend with mild oscillation — enough to exercise the Stochastic
    # screener's full indicator + entry pipeline without hand-tuning a signal.
    base = [100.0 + 0.3 * i + 2.0 * ((-1) ** i) for i in range(periods)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2024-01-01", periods=periods, freq="D"),
            "open": base,
            "high": [value + 1.5 for value in base],
            "low": [value - 1.5 for value in base],
            "close": base,
            "volume": [1000.0] * periods,
        }
    )


def _stochastic_params() -> dict:
    # Mirror the screener's own defaults so the test does not depend on the UI.
    params = dict(stochastic_swing.SCREENER["default_params"])
    params.update({"start_date": date(2024, 1, 1), "end_date": date(2024, 12, 31)})
    return params


def test_stochastic_swing_screener_runs_and_returns_schema():
    # A 260-candle history is enough for the 200 SMA plus warm-up. This is a
    # contract test: the screener must run end-to-end and return RESULT_COLUMNS.
    frames = {"AAA": _synthetic_daily(260)}

    result = stochastic_swing.run(_universe(), FakeDataLoader(frames), _stochastic_params())

    assert isinstance(result, pd.DataFrame)
    assert list(result.columns) == stochastic_swing.RESULT_COLUMNS
    # At most one symbol was scanned, so the shortlist is 0 or 1 row.
    assert len(result) <= 1
    if not result.empty:
        assert result.iloc[0]["rating"] in {"BUY", "SELL"}


def test_stochastic_swing_screener_tolerates_empty_and_short_frames():
    # SMA200 needs 200 prior candles. Empty and too-short frames must be skipped
    # gracefully, not raised as errors.
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _synthetic_daily(50),
    }

    result = stochastic_swing.run(_universe(), FakeDataLoader(frames), _stochastic_params())

    assert result.empty
    assert list(result.columns) == stochastic_swing.RESULT_COLUMNS


# ---------------------------------------------------------------------------
# 52 Week High/Low (Ceyhun)
# ---------------------------------------------------------------------------


def _week52_params(**overrides):
    """Compact defaults so the fixture does not need 252+ candles."""
    params = dict(week52_low_ceyhun.SCREENER["default_params"])
    # Smaller windows let the synthetic data prove the rule in ~20 candles.
    params.update({
        "window_bars": 10,
        "recent_window_bars": 3,
        "proximity_pct": 0.02,
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
    })
    params.update(overrides)
    return params


def test_week52_low_ceyhun_returns_buy_when_close_revisits_low():
    # First 12 candles establish a baseline low; the very last candle returns
    # close to that low. With proximity_pct=2%, the signal should fire.
    base = [100.0] * 8 + [90.0] + [100.0] * 4 + [91.5]
    frames = {
        "NEAR_LOW": _bollinger_candles(
            open_values=[v for v in base],
            high_values=[v + 1.0 for v in base],
            low_values=[v - 0.5 for v in base],
            close_values=base,
        ),
    }
    # Use window_bars=8 so the rolling 52w low forms early enough in the
    # synthetic 14-candle dataset. Recent window = last 3 candles.
    params = _week52_params(window_bars=8, recent_window_bars=3, proximity_pct=0.03)

    result = week52_low_ceyhun.run(_universe(), FakeDataLoader(frames), params)

    assert result["symbol"].tolist() == ["NEAR_LOW"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    # The signal proximity should be at most the configured threshold.
    assert row["proximity_pct_at_signal"] <= 0.03


def test_week52_low_ceyhun_skips_when_close_is_far_from_low():
    # All-time low of 90 is set early, but recent closes are at 130 — nowhere
    # near the 52w low. Signal must NOT fire.
    closes = [100.0] * 4 + [90.0] + [100.0] * 4 + [130.0, 131.0, 132.0]
    frames = {
        "FAR_FROM_LOW": _bollinger_candles(
            open_values=closes,
            high_values=[v + 1.0 for v in closes],
            low_values=[v - 0.5 for v in closes],
            close_values=closes,
        ),
    }
    params = _week52_params(window_bars=8, recent_window_bars=3, proximity_pct=0.02)

    result = week52_low_ceyhun.run(_universe(), FakeDataLoader(frames), params)

    assert result.empty
    assert list(result.columns) == week52_low_ceyhun.RESULT_COLUMNS


def test_week52_low_ceyhun_tolerates_empty_and_short_frames():
    # Empty frames and frames shorter than `window_bars` must be skipped, not
    # raise an exception that would abort the entire scan.
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _bollinger_candles([10.0, 10.0], [11.0, 11.0], [9.0, 9.0], [10.0, 10.0]),
    }
    params = _week52_params(window_bars=8, recent_window_bars=3)

    result = week52_low_ceyhun.run(_universe(), FakeDataLoader(frames), params)

    assert result.empty
    assert list(result.columns) == week52_low_ceyhun.RESULT_COLUMNS


# ---------------------------------------------------------------------------
# 14% Below 200 EMA
# ---------------------------------------------------------------------------


def _ema200_params(**overrides):
    """Smaller EMA period keeps the synthetic dataset short."""
    params = dict(ema200_14percent_below.SCREENER["default_params"])
    params.update({
        "ema_period": 5,
        "discount_pct": 0.14,
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
    })
    params.update(overrides)
    return params


def test_ema200_14percent_below_fires_when_close_is_well_below_ema():
    # Steady at 100 for several candles to build an EMA near 100, then a sharp
    # drop to 80. 80 vs an EMA still near 95 is comfortably more than 14%.
    closes = [100.0] * 10 + [80.0]
    frames = {
        "DISCOUNT": _bollinger_candles(
            open_values=closes,
            high_values=[v + 0.5 for v in closes],
            low_values=[v - 0.5 for v in closes],
            close_values=closes,
        ),
    }

    result = ema200_14percent_below.run(_universe(), FakeDataLoader(frames), _ema200_params())

    assert result["symbol"].tolist() == ["DISCOUNT"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    # The realized discount must be at least the configured threshold.
    assert row["actual_discount_pct"] >= 0.14


def test_ema200_14percent_below_skips_when_close_is_close_to_ema():
    # A 3% dip is below the 14% threshold; no signal should appear.
    closes = [100.0] * 10 + [97.0]
    frames = {
        "MILD": _bollinger_candles(
            open_values=closes,
            high_values=[v + 0.5 for v in closes],
            low_values=[v - 0.5 for v in closes],
            close_values=closes,
        ),
    }

    result = ema200_14percent_below.run(_universe(), FakeDataLoader(frames), _ema200_params())

    assert result.empty
    assert list(result.columns) == ema200_14percent_below.RESULT_COLUMNS


def test_ema200_14percent_below_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _bollinger_candles([10.0], [11.0], [9.0], [10.0]),
    }

    result = ema200_14percent_below.run(_universe(), FakeDataLoader(frames), _ema200_params())

    assert result.empty
    assert list(result.columns) == ema200_14percent_below.RESULT_COLUMNS
