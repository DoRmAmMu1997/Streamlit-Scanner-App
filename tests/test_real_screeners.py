from __future__ import annotations

import concurrent.futures
import json
import time
from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.sixty_seven.agent import EvidenceItem, SixtySevenVerdict
from backend.technical.technical_agent import TechnicalVerdict
from screeners import (
    bollinger_band_reversal,
    bollinger_lower_band,
    envelope,
    envelope_knoxville_buy,
    green_candles_20pct_up,
    heikin_ashi_supertrend,
    sixty_seven_ka_funda,
    stochastic_swing,
    technical_analysis,
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


def _bb_frame(close_values: list[float]) -> pd.DataFrame:
    """OHLC frame for the Bollinger-Band screener (candle colour is irrelevant)."""
    return _bollinger_candles(
        open_values=close_values,
        high_values=[value + 1.0 for value in close_values],
        low_values=[value - 1.0 for value in close_values],
        close_values=close_values,
    )


def _bollinger_lower_band_params() -> dict:
    params = dict(bollinger_lower_band.SCREENER["default_params"])
    params.update(
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 19),
            # Short period keeps the synthetic candles easy to reason about.
            "bb_period": 5,
            "bb_std": 2.5,
            "bb_proximity_pct": 0.01,
        }
    )
    return params


def _env_knox_candles(close_values: list[float]) -> pd.DataFrame:
    """Compact candles for the Envelope + Knoxville screener.

    `low = close - 0.5` so the pivot-low arithmetic in the fixtures below is easy
    to follow; open/high are unused by the envelope or Knoxville logic.
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
    params = dict(envelope_knoxville_buy.SCREENER["default_params"])
    params.update(
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 13),
            # Short periods + SMA basis make the synthetic candles easy to reason
            # about. The production defaults remain EMA200, 14% bands, RSI14.
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
            # RSI gate loosened so the test isolates the price/momentum
            # divergence and envelope filters, not the exact RSI(3) value.
            "oversold": 95.0,
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


def _universe_for(symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbols,
            "security_id": [str(index) for index, _symbol in enumerate(symbols, start=1)],
            "exchange_segment": ["NSE_EQ"] * len(symbols),
            "instrument_type": ["EQUITY"] * len(symbols),
            "mapping_status": ["mapped"] * len(symbols),
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


def test_bollinger_lower_band_screener_returns_buy_only_near_lower_band():
    # NEAR ends on a flat low run, so its latest close sits on the lower band.
    # FAR is steadily rising, so its latest close is well above the lower band.
    frames = {
        "NEAR": _bb_frame([100.0, 96.0, 92.0, 90.0, 88.0, 88.0, 88.0, 88.0, 88.0, 88.0]),
        "FAR": _bb_frame([80.0, 82.0, 84.0, 86.0, 88.0, 90.0, 92.0, 94.0, 96.0, 98.0]),
    }

    result = bollinger_lower_band.run(
        _universe(), FakeDataLoader(frames), _bollinger_lower_band_params()
    )

    assert result["symbol"].tolist() == ["NEAR"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["close"] <= row["bb_lower"] * 1.01
    assert {"bb_lower", "bb_middle", "bb_upper"}.issubset(result.columns)


def test_bollinger_lower_band_screener_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _bb_frame([10.0, 10.0, 10.0]),  # 3 candles < bb_period (5)
    }

    result = bollinger_lower_band.run(
        _universe(), FakeDataLoader(frames), _bollinger_lower_band_params()
    )

    assert result.empty
    assert list(result.columns) == bollinger_lower_band.RESULT_COLUMNS


def test_envelope_knoxville_buy_returns_buy_only_when_both_filters_pass():
    # BUY: latest close is below the lower Envelope band AND a recent pivot shows
    # a bullish Knoxville divergence (lower price low, higher momentum low).
    # NO_KD: below the band but no divergence. NO_ENV: has the divergence but the
    # latest close recovered above the band. Only BUY passes both filters.
    frames = {
        "BUY": _env_knox_candles(
            # Last close 78 sits clearly below the lower Envelope band (no reliance
            # on the 1% proximity buffer); the earlier 92->100 then 90 dip is the
            # bullish divergence the Knoxville filter needs.
            [100.0, 100.0, 100.0, 100.0, 96.0, 92.0, 96.0, 100.0, 95.0, 90.0, 93.0, 85.0, 78.0]
        ),
        "NO_KD": _env_knox_candles(
            [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 90.0, 80.0]
        ),
        "NO_ENV": _env_knox_candles(
            [100.0, 100.0, 100.0, 100.0, 96.0, 92.0, 96.0, 100.0, 95.0, 90.0, 93.0, 97.0, 100.0]
        ),
    }

    result = envelope_knoxville_buy.run(_universe(), FakeDataLoader(frames), _env_knox_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["close"] <= row["env_lower"] * 1.01
    assert row["entry_trigger"] == "recent_envelope_kd"
    assert "Knoxville" in row["reason"]


def test_envelope_knoxville_buy_shortlists_old_kd_retest_without_envelope():
    params = _env_knox_params()
    kd_pivot_low = 89.5
    exactly_two_pct_up = kd_pivot_low * 1.02
    above_two_pct = kd_pivot_low * 1.02 + 0.10

    base_path = [
        100.0, 100.0, 100.0, 100.0, 96.0,
        92.0, 96.0, 100.0, 95.0, 90.0,
        93.0, 110.0, 105.0, 100.0, 98.0,
        99.0, 100.0, 99.0, 98.0, 97.0,
        96.0, 95.0, 94.0, 93.0,
    ]
    frames = {
        "OLD": _env_knox_candles([*base_path, exactly_two_pct_up]),
        "ABOVE": _env_knox_candles([*base_path, above_two_pct]),
    }

    result = envelope_knoxville_buy.run(
        _universe_for(["OLD", "ABOVE"]),
        FakeDataLoader(frames),
        params,
    )

    assert result["symbol"].tolist() == ["OLD"]
    row = result.iloc[0]
    assert row["entry_trigger"] == "old_kd_retest"
    assert row["divergence_price"] == pytest.approx(kd_pivot_low)
    assert row["divergence_bars_ago"] > params["signal_recency_bars"]
    assert row["kd_retest_distance_pct"] == pytest.approx(0.02)
    assert row["close"] > row["env_lower"] * 1.01


def test_envelope_knoxville_buy_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _env_knox_candles([100.0, 99.0, 98.0]),  # 3 candles < ema_period (5)
    }

    result = envelope_knoxville_buy.run(_universe(), FakeDataLoader(frames), _env_knox_params())

    assert result.empty
    assert list(result.columns) == envelope_knoxville_buy.RESULT_COLUMNS


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
            open_values=list(base),
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
# Envelope (lower band)
# ---------------------------------------------------------------------------


def _envelope_params(**overrides):
    """Compact params: a 5-period SMA basis keeps the synthetic dataset short.

    `exponential=False` uses an SMA basis, whose value is just the mean of the
    last 5 closes — easy to verify by hand. The production default is a 200-EMA
    basis with 14% bands.
    """
    params = dict(envelope.SCREENER["default_params"])
    params.update({
        "ema_period": 5,
        "percent": 14.0,
        "exponential": False,
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
    })
    params.update(overrides)
    return params


def test_envelope_fires_when_close_is_below_lower_band():
    # Steady at 100 (SMA basis ~ 96 after the drop), then a fall to 80. The lower
    # band is 0.86 * 96 = 82.56, so a close of 80 sits below it -> BUY.
    closes = [100.0] * 10 + [80.0]
    frames = {
        "DISCOUNT": _bollinger_candles(
            open_values=closes,
            high_values=[v + 0.5 for v in closes],
            low_values=[v - 0.5 for v in closes],
            close_values=closes,
        ),
    }

    result = envelope.run(_universe(), FakeDataLoader(frames), _envelope_params())

    assert result["symbol"].tolist() == ["DISCOUNT"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["close"] <= row["env_lower"]
    # With 14% bands, being at/below the lower band means >= 14% below the basis.
    assert row["pct_below_basis"] >= 0.14
    # PROV-002: a deterministic screener records its rule and indicator values,
    # and the whole receipt is JSON-serializable for persistence.
    provenance = row["provenance"]
    assert provenance["source"] == "deterministic"
    assert provenance["screener_key"] == envelope.SCREENER["key"]
    assert provenance["triggered_rules"] == ["close_at_or_below_lower_envelope_band"]
    assert provenance["indicator_values"]["env_lower"] == row["env_lower"]
    json.dumps(provenance, allow_nan=False)


def test_envelope_skips_when_close_is_near_the_basis():
    # A 3% dip stays well inside the lower band (0.86 * basis), so no signal.
    closes = [100.0] * 10 + [97.0]
    frames = {
        "MILD": _bollinger_candles(
            open_values=closes,
            high_values=[v + 0.5 for v in closes],
            low_values=[v - 0.5 for v in closes],
            close_values=closes,
        ),
    }

    result = envelope.run(_universe(), FakeDataLoader(frames), _envelope_params())

    assert result.empty
    assert list(result.columns) == envelope.RESULT_COLUMNS


def test_envelope_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _bollinger_candles([10.0], [11.0], [9.0], [10.0]),  # 1 candle < ema_period (5)
    }

    result = envelope.run(_universe(), FakeDataLoader(frames), _envelope_params())

    assert result.empty
    assert list(result.columns) == envelope.RESULT_COLUMNS


# ---------------------------------------------------------------------------
# 20% Up Green Candles (Lovevanshi)
# ---------------------------------------------------------------------------


def _green_params(**overrides):
    params = dict(green_candles_20pct_up.SCREENER["default_params"])
    params.update({
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 1, 31),
    })
    params.update(overrides)
    return params


def _green_run_candles(opens, highs, lows, closes) -> pd.DataFrame:
    """OHLC frame where candle colour (close vs open) is meaningful."""
    return _bollinger_candles(opens, highs, lows, closes)


def test_green_candles_fires_on_a_20pct_all_green_run():
    # Six consecutive green candles (close > open each), rising from a low of 99
    # to a high of 130: (130 - 99) / 99 = 31% > 20% -> BUY.
    opens = [100.0, 105.0, 110.0, 115.0, 120.0, 125.0]
    closes = [104.0, 109.0, 114.0, 119.0, 124.0, 129.0]
    highs = [105.0, 110.0, 115.0, 120.0, 125.0, 130.0]
    lows = [99.0, 104.0, 109.0, 114.0, 119.0, 124.0]
    frames = {"RUNNER": _green_run_candles(opens, highs, lows, closes)}

    result = green_candles_20pct_up.run(_universe(), FakeDataLoader(frames), _green_params())

    assert result["symbol"].tolist() == ["RUNNER"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["run_length"] == 6
    assert row["run_gain_pct"] > 0.20
    # PROV-002: rule name + key indicator values travel with the row.
    provenance = row["provenance"]
    assert provenance["source"] == "deterministic"
    assert provenance["triggered_rules"] == ["green_run_gain_above_threshold"]
    assert provenance["indicator_values"]["run_length"] == 6
    json.dumps(provenance, allow_nan=False)


def test_green_candles_skips_when_latest_candle_is_red():
    # The run must be in progress on the most recent bar. A red final candle
    # (close < open) breaks the run, so nothing is shortlisted.
    opens = [100.0, 105.0, 110.0]
    closes = [104.0, 109.0, 108.0]  # last close < last open -> red
    highs = [105.0, 110.0, 111.0]
    lows = [99.0, 104.0, 107.0]
    frames = {"REDLAST": _green_run_candles(opens, highs, lows, closes)}

    result = green_candles_20pct_up.run(_universe(), FakeDataLoader(frames), _green_params())

    assert result.empty
    assert list(result.columns) == green_candles_20pct_up.RESULT_COLUMNS


def test_green_candles_skips_when_run_gain_is_too_small():
    # An all-green but tight run: high 103 vs low 99.5 is only ~3.5%, below 20%.
    opens = [100.0, 101.0, 102.0]
    closes = [100.5, 101.5, 102.5]
    highs = [101.0, 102.0, 103.0]
    lows = [99.5, 100.5, 101.5]
    frames = {"TIGHT": _green_run_candles(opens, highs, lows, closes)}

    result = green_candles_20pct_up.run(_universe(), FakeDataLoader(frames), _green_params())

    assert result.empty
    assert list(result.columns) == green_candles_20pct_up.RESULT_COLUMNS


def test_green_candles_tolerates_empty_and_short_frames():
    frames = {
        "EMPTY": pd.DataFrame(),
        # A single green candle whose own range is < 20% must not raise or fire.
        "ONE": _green_run_candles([100.0], [101.0], [99.0], [100.5]),
    }

    result = green_candles_20pct_up.run(_universe(), FakeDataLoader(frames), _green_params())

    assert result.empty
    assert list(result.columns) == green_candles_20pct_up.RESULT_COLUMNS


# ---------------------------------------------------------------------------
# Technical Analysis (AI) — the cheap pivot gate (no live LLM)
# ---------------------------------------------------------------------------


class _StubTechnicalAgent:
    """Stand-in for TechnicalAnalysisAgent: records calls, returns a fixed verdict."""

    def __init__(self, verdict: TechnicalVerdict):
        self.verdict = verdict
        self.calls = 0
        self.force_refreshes: list[bool] = []

    def analyze(self, symbol, candles, levels, *, params=None, force_refresh=False):
        self.calls += 1
        self.force_refreshes.append(bool(force_refresh))
        return self.verdict.model_copy(update={"symbol": str(symbol).upper()})


def _ta_candles(lows: list[float]) -> pd.DataFrame:
    """OHLC frame from a low path: high = low + 2, open = close = low + 1.

    Candle colour is irrelevant to the TA gate, which only reads the high/low
    pivots and the latest close.
    """
    closes = [lo + 1.0 for lo in lows]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2020-01-01", periods=len(lows), freq="D"),
            "open": closes,
            "high": [lo + 2.0 for lo in lows],
            "low": lows,
            "close": closes,
            "volume": [1000.0] * len(lows),
        }
    )


def _ta_params(**overrides) -> dict:
    """Compact pivot windows so a short synthetic frame still confirms levels."""
    params = dict(technical_analysis.SCREENER["default_params"])
    params.update(
        {
            "pivot_left": 2,
            "pivot_right": 2,
            "cluster_pct": 3.0,
            "min_touches": 3,
            "support_tolerance_pct": 2.0,
            "breakout_lookback_bars": 5,
            "start_date": date(2020, 1, 1),
            "end_date": date(2020, 3, 1),
        }
    )
    params.update(overrides)
    return params


# Three clean dips to ~90 (a major support after min_touches=3), then a final
# close that settles right at that support zone.
_AT_SUPPORT_LOWS = [
    98.0, 96.0, 90.0, 96.0, 98.0,   # pivot low @90 (idx 2)
    100.0, 96.0, 90.0, 96.0, 98.0,  # pivot low @90 (idx 7)
    100.0, 96.0, 90.0, 96.0, 98.0,  # pivot low @90 (idx 12)
    96.0, 89.5,                     # latest close = 90.5, at the ~90 support
]

def _midrange_candles() -> pd.DataFrame:
    """A genuine non-setup: a 3-touch support at 90, but price drifts sideways in
    the middle with WIDE, overlapping candles — so no FVG/order-block forms, the
    close is far from support, and nothing broke out. The gate must reject it.

    Wide candles (range 8) are deliberate: gentle moves never leave a 3-candle
    imbalance, so none of the new price-action triggers can fire on noise.
    """
    lows = [
        95.0, 92.0, 90.0, 92.0, 95.0,
        97.0, 92.0, 90.0, 92.0, 95.0,
        97.0, 92.0, 90.0, 92.0, 95.0,
        96.0, 95.0, 96.0, 95.0, 96.0,  # sideways drift around 95–100, above 90
    ]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2020-01-01", periods=len(lows), freq="D"),
            "open": [lo + 4.0 for lo in lows],
            "high": [lo + 8.0 for lo in lows],
            "low": lows,
            "close": [lo + 4.0 for lo in lows],
            "volume": [1000.0] * len(lows),
        }
    )


def _ta_verdict(**overrides) -> TechnicalVerdict:
    base = dict(
        symbol="BUY",
        pattern="at_support",
        confirmed=True,
        key_levels=[90.0],
        confidence=6,
        reasoning="Price is basing at the 90 major support.",
        signal_date="2020-01-17",
        model_used="test-model",
    )
    base.update(overrides)
    return TechnicalVerdict(**base)


def test_technical_analysis_gate_admits_at_support_and_calls_agent(monkeypatch):
    # The stub agent replaces the real one, so no SDK or network is touched.
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {"BUY": _ta_candles(_AT_SUPPORT_LOWS)}
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _ta_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["pattern"] == "at_support"
    assert row["reason"] == "Price is basing at the 90 major support."
    # PROV-002: a gate+AI agreement is a hybrid signal carrying the fired gate
    # rule and the AI-qualified marker.
    provenance = row["provenance"]
    assert provenance["source"] == "hybrid"
    assert provenance["screener_key"] == technical_analysis.SCREENER["key"]
    assert "gate_at_support" in provenance["triggered_rules"]
    assert "ai_setup_qualified" in provenance["triggered_rules"]
    # The candidate reached the agent exactly once.
    assert stub.calls == 1
    assert list(result.columns) == [
        "symbol",
        "rating",
        "signal_date",
        "close",
        "reason",
        "pattern",
        "confirmed",
        "confidence",
        "trend",
        "nearest_level",
        "provenance",
    ]


def test_technical_analysis_honors_max_ai_candidates(monkeypatch):
    """Cap expensive AI confirmations after the deterministic gate admits symbols."""
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {
        "BUY": _ta_candles(_AT_SUPPORT_LOWS),
        "SELL": _ta_candles(_AT_SUPPORT_LOWS),
    }
    result = technical_analysis.run(
        _universe(),
        FakeDataLoader(frames),
        _ta_params(max_ai_candidates=1),
    )

    assert result["symbol"].tolist() == ["BUY"]
    assert stub.calls == 1


def test_technical_analysis_forwards_force_refresh_to_agent(monkeypatch):
    """A UI refresh should bypass the AI verdict cache, not only candle cache."""
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {"BUY": _ta_candles(_AT_SUPPORT_LOWS)}
    result = technical_analysis.run(
        _universe(),
        FakeDataLoader(frames),
        _ta_params(force_refresh=True),
    )

    assert result["symbol"].tolist() == ["BUY"]
    assert stub.force_refreshes == [True]


def test_technical_analysis_gate_rejects_midrange_without_calling_agent(monkeypatch):
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {"BUY": _midrange_candles()}
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _ta_params())

    assert result.empty
    # The gate dropped the stock BEFORE any LLM call.
    assert stub.calls == 0
    assert list(result.columns) == technical_analysis.RESULT_COLUMNS


def test_technical_analysis_degrades_when_agent_unavailable(monkeypatch):
    # Agent raises (e.g. Claude Agent SDK not installed / plan limit) → the
    # screener still surfaces a gate-only at-support candidate.
    from backend.fundamentals.fundamental_agent import FundamentalsAgentError

    def _raising_agent():
        class _Boom:
            def analyze(self, *a, **k):
                raise FundamentalsAgentError("claude-agent-sdk is not installed.")

        return _Boom()

    monkeypatch.setattr(technical_analysis, "_get_agent", _raising_agent)

    frames = {"BUY": _ta_candles(_AT_SUPPORT_LOWS)}
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _ta_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["pattern"] == "at_support"
    assert bool(row["confirmed"]) is False
    assert "unavailable" in row["reason"].lower()
    # PROV-002: the gate-only fallback is purely deterministic and says so.
    provenance = row["provenance"]
    assert provenance["source"] == "deterministic"
    assert "gate_at_support" in provenance["triggered_rules"]
    assert "ai_confirmation_unavailable" in provenance["triggered_rules"]
    assert list(result.columns) == [
        "symbol",
        "rating",
        "signal_date",
        "close",
        "reason",
        "pattern",
        "confirmed",
        "confidence",
        "trend",
        "nearest_level",
        "provenance",
    ]


def _double_bottom_candles() -> pd.DataFrame:
    """Two equal 45 lows (idx 2 & 8) around a 60 neckline (idx 5), then a close
    above 60 at idx 11 → a confirmed double bottom that the gate should admit."""
    high = [57, 54, 48, 55, 58, 60, 57, 53, 48, 55, 59, 63, 65, 67, 69]
    low = [55, 52, 45, 52, 56, 58, 54, 50, 45, 52, 56, 60, 62, 64, 66]
    close = [56, 53, 47, 54, 57, 59, 55, 51, 47, 54, 58, 62, 64, 66, 68]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2020-01-01", periods=len(low), freq="D"),
            "open": [c - 1.0 for c in close],
            "high": [float(h) for h in high],
            "low": [float(lo) for lo in low],
            "close": [float(c) for c in close],
            "volume": [1000.0] * len(low),
        }
    )


def _double_bottom_params(**overrides) -> dict:
    """Short-frame knobs so the 2-touch double bottom forms a level and confirms."""
    return _ta_params(
        min_touches=2,
        cluster_pct=4.0,
        swing_left=2,
        swing_right=2,
        double_tolerance_pct=4.0,
        breakout_lookback_bars=6,
        **overrides,
    )


def test_technical_analysis_admits_fresh_double_bottom_and_surfaces_trend(monkeypatch):
    # A NEW first-class trigger: the stock is nowhere near support and never broke
    # resistance, but a freshly-confirmed double bottom must still shortlist it.
    stub = _StubTechnicalAgent(
        _ta_verdict(pattern="double_bottom", confirmed=True, key_levels=[45.0], trend="uptrend")
    )
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {"BUY": _double_bottom_candles()}
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _double_bottom_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["pattern"] == "double_bottom"
    assert bool(row["confirmed"]) is True
    assert row["trend"] == "uptrend"  # the new structure column is surfaced
    assert stub.calls == 1


def test_technical_analysis_double_bottom_degrades_without_agent(monkeypatch):
    # Same fresh double bottom, but the agent is unavailable → the gate-only row
    # should still surface it (labelled double_bottom, unconfirmed).
    from backend.fundamentals.fundamental_agent import FundamentalsAgentError

    def _raising_agent():
        class _Boom:
            def analyze(self, *a, **k):
                raise FundamentalsAgentError("claude-agent-sdk is not installed.")

        return _Boom()

    monkeypatch.setattr(technical_analysis, "_get_agent", _raising_agent)

    frames = {"BUY": _double_bottom_candles()}
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _double_bottom_params())

    assert result["symbol"].tolist() == ["BUY"]
    row = result.iloc[0]
    assert row["pattern"] == "double_bottom"
    assert bool(row["confirmed"]) is False
    assert "unavailable" in row["reason"].lower()


def test_technical_analysis_tolerates_empty_and_short_frames(monkeypatch):
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    frames = {
        "EMPTY": pd.DataFrame(),
        "SHORT": _ta_candles([100.0, 99.0, 98.0]),  # too short to confirm pivots
    }
    result = technical_analysis.run(_universe(), FakeDataLoader(frames), _ta_params())

    assert result.empty
    assert stub.calls == 0
    assert list(result.columns) == technical_analysis.RESULT_COLUMNS


def test_technical_analysis_confirms_multiple_candidates_in_universe_order(monkeypatch):
    """The parallel AI pass must confirm every gate-passing candidate and return
    rows in deterministic universe order (not thread-completion order)."""
    stub = _StubTechnicalAgent(_ta_verdict())
    monkeypatch.setattr(technical_analysis, "_get_agent", lambda: stub)

    # Three at-support stocks; the universe fixture lists them as AAA, BBB, CCC.
    frames = {
        "AAA": _ta_candles(_AT_SUPPORT_LOWS),
        "BBB": _ta_candles(_AT_SUPPORT_LOWS),
        "CCC": _ta_candles(_AT_SUPPORT_LOWS),
    }
    universe = pd.DataFrame(
        {
            "symbol": ["AAA", "BBB", "CCC"],
            "security_id": ["1", "2", "3"],
            "exchange_segment": ["NSE_EQ", "NSE_EQ", "NSE_EQ"],
            "instrument_type": ["EQUITY", "EQUITY", "EQUITY"],
            "mapping_status": ["mapped", "mapped", "mapped"],
        }
    )
    result = technical_analysis.run(
        universe, FakeDataLoader(frames), _ta_params(max_ai_candidates=10)
    )

    # All three confirmed, and the row order matches universe/candidate order
    # regardless of which thread finished first.
    assert result["symbol"].tolist() == ["AAA", "BBB", "CCC"]
    assert stub.calls == 3


def test_technical_analysis_agent_cache_is_thread_safe(monkeypatch):
    """Parallel confirmations should not race while building the shared agent.

    PR #23 made `_get_agent()` reachable from worker threads. This test slows
    construction just enough that an unlocked cache tends to build several
    agents for the same `(model, fast_mode)` key.
    """
    created_agents: list[object] = []

    class _SlowAgent:
        def __init__(self, *, model, fast_mode):
            time.sleep(0.05)
            self.model = model
            self.fast_mode = fast_mode
            created_agents.append(self)

    monkeypatch.setattr(technical_analysis, "get_fundamentals_model", lambda: "test-model")
    monkeypatch.setattr(technical_analysis, "get_agent_fast_mode", lambda: True)
    monkeypatch.setattr(technical_analysis, "TechnicalAnalysisAgent", _SlowAgent)
    technical_analysis._AGENT_CACHE.clear()

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        agents = list(executor.map(lambda _: technical_analysis._get_agent(), range(8)))

    assert len(created_agents) == 1
    assert len({id(agent) for agent in agents}) == 1


# ---------------------------------------------------------------------------
# 67 Ka Funda (AI) - deterministic drawdown gate + AI approval
# ---------------------------------------------------------------------------


class _StubSixtySevenAgent:
    def __init__(self, approvals: dict[str, bool]):
        self.approvals = approvals
        self.calls: list[tuple[str, bool]] = []

    def verify(self, symbol, candidate, *, force_refresh=False, search_result_count=5):
        self.calls.append((str(symbol).upper(), bool(force_refresh)))
        approved = self.approvals.get(str(symbol).upper(), False)
        return SixtySevenVerdict(
            symbol=str(symbol).upper(),
            approved=approved,
            fall_reason_category="business" if approved else "unclear",
            fall_reason_clear=approved,
            fall_reason_no_longer_exists=approved,
            proven_profit_record=approved,
            future_growth_prospects=approved,
            quarterly_improvement=approved,
            minimum_upside_100pct=approved,
            confidence=8 if approved else 3,
            evidence=[
                EvidenceItem(
                    source="Screener.in",
                    title="Quarterly trend",
                    link="https://www.screener.in/company/BUY/",
                    snippet="Latest quarter improved.",
                )
            ],
            rejection_reason="" if approved else "Reason for the fall is still unclear.",
            summary="Approved 67 ka funda setup." if approved else "Rejected.",
            model_used="test-model",
        )


def _sixty_seven_candles(ath: float, latest_close: float) -> pd.DataFrame:
    closes = [ath * 0.9, ath * 0.6, latest_close]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="D"),
            "open": closes,
            "high": [ath, ath * 0.75, latest_close * 1.1],
            "low": [ath * 0.85, ath * 0.55, latest_close * 0.95],
            "close": closes,
            "volume": [1000.0] * 3,
        }
    )


def _sixty_seven_params(**overrides) -> dict:
    params = dict(sixty_seven_ka_funda.SCREENER["default_params"])
    params.update(
        {
            "start_date": date(2026, 1, 1),
            "end_date": date(2026, 1, 3),
        }
    )
    params.update(overrides)
    return params


def test_sixty_seven_screener_returns_ai_approved_rows_only(monkeypatch):
    stub = _StubSixtySevenAgent({"BUY": True, "SELL": False})
    monkeypatch.setattr(sixty_seven_ka_funda, "_get_agent", lambda: stub)
    frames = {
        "BUY": _sixty_seven_candles(300.0, 90.0),
        "SELL": _sixty_seven_candles(300.0, 90.0),
        "HOLD": _sixty_seven_candles(300.0, 150.0),
    }

    result = sixty_seven_ka_funda.run(
        _universe(),
        FakeDataLoader(frames),
        _sixty_seven_params(max_ai_candidates=10),
    )

    assert result["symbol"].tolist() == ["BUY"]
    assert stub.calls == [("BUY", False), ("SELL", False)]
    row = result.iloc[0]
    assert row["rating"] == "BUY"
    assert row["drawdown_pct"] == pytest.approx(70.0)
    assert row["fall_reason_category"] == "business"
    assert "Approved" in row["reason"]
    assert list(result.columns) == sixty_seven_ka_funda.RESULT_COLUMNS


def test_sixty_seven_screener_honors_max_ai_candidates(monkeypatch):
    stub = _StubSixtySevenAgent({"BUY": True, "SELL": True})
    monkeypatch.setattr(sixty_seven_ka_funda, "_get_agent", lambda: stub)
    frames = {
        "BUY": _sixty_seven_candles(300.0, 90.0),
        "SELL": _sixty_seven_candles(300.0, 90.0),
    }

    result = sixty_seven_ka_funda.run(
        _universe(),
        FakeDataLoader(frames),
        _sixty_seven_params(max_ai_candidates=1),
    )

    assert result["symbol"].tolist() == ["BUY"]
    assert stub.calls == [("BUY", False)]


def test_sixty_seven_screener_forwards_force_refresh_to_agent(monkeypatch):
    stub = _StubSixtySevenAgent({"BUY": True})
    monkeypatch.setattr(sixty_seven_ka_funda, "_get_agent", lambda: stub)

    sixty_seven_ka_funda.run(
        _universe(),
        FakeDataLoader({"BUY": _sixty_seven_candles(300.0, 90.0)}),
        _sixty_seven_params(force_refresh=True),
    )

    assert stub.calls == [("BUY", True)]
