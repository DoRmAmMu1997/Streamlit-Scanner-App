from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd

from screeners import bollinger_band_reversal, heikin_ashi_supertrend, stochastic_swing


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
