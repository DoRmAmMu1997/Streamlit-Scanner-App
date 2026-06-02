"""Tests for the shared BaseScanner abstract class."""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.scanner_base import BaseScanner, COMMON_RESULT_COLUMNS


class _SimpleScanner(BaseScanner):
    """Minimal concrete scanner used by these unit tests."""

    SCREENER = {
        "key": "test_simple",
        "name": "Test Simple",
        "description": "Toy scanner used by tests only.",
        "universe": "test",
        "timeframe": "daily",
        "lookback_days": 10,
        "default_params": {"threshold": 5.0, "lookback": 3},
    }
    EXTRA_RESULT_COLUMNS = ["latest_close"]

    def compute_signal(self, symbol, candles, params):
        # Returns a row for any non-empty frame. The strategy itself is not the
        # point of these tests — we only verify the base-class plumbing.
        if candles.empty:
            return None
        latest_close = float(candles.iloc[-1]["close"])
        threshold = self.coerce_param(params, "threshold", float)
        if latest_close < threshold:
            return None
        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": candles.iloc[-1].get("timestamp", ""),
            "close": latest_close,
            "reason": f"close {latest_close} >= threshold {threshold}",
            "latest_close": latest_close,
        }


class _FailingScanner(BaseScanner):
    """A scanner whose compute_signal raises — for testing exception capture."""

    SCREENER = {
        "key": "test_failing",
        "name": "Test Failing",
        "description": "Throws on every symbol.",
        "universe": "test",
        "timeframe": "daily",
        "lookback_days": 10,
        "default_params": {},
    }

    def compute_signal(self, symbol, candles, params):
        raise RuntimeError(f"intentional failure on {symbol}")


class _FakeLoader:
    """Drop-in for DailyDataLoader. Returns prebuilt frames; no Dhan calls."""

    def __init__(self, frames):
        self.frames = frames
        self.last_max_symbols = None

    def load_universe_history(self, universe_df, start_date, end_date,
                              max_symbols=None, force_refresh=False, progress_callback=None):
        self.last_max_symbols = max_symbols
        selected = dict(self.frames)
        if max_symbols is not None:
            selected = dict(list(selected.items())[: int(max_symbols)])
        if progress_callback is not None:
            total = len(selected)
            for index, symbol in enumerate(selected.keys(), start=1):
                progress_callback(index, total, symbol)
        return SimpleNamespace(frames=selected, failures=[],
                               cache_hits=0, cache_misses=len(selected))


def _candles(closes):
    return pd.DataFrame({
        "timestamp": pd.date_range("2026-01-01", periods=len(closes), freq="D"),
        "open": closes,
        "high": [c + 1 for c in closes],
        "low": [c - 1 for c in closes],
        "close": closes,
        "volume": [1000] * len(closes),
    })


def _params(**overrides):
    base = {"start_date": date(2026, 1, 1), "end_date": date(2026, 1, 10)}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Result-column normalization
# ---------------------------------------------------------------------------


def test_result_columns_start_with_common_schema():
    scanner = _SimpleScanner()
    # The common columns must come first, in declared order. Streamlit's
    # emoji-badge logic and chart picker rely on this.
    assert scanner.result_columns[: len(COMMON_RESULT_COLUMNS)] == COMMON_RESULT_COLUMNS
    # The screener's extras come after the common schema.
    assert scanner.result_columns[-1] == "latest_close"


def test_result_columns_deduplicates_extras():
    class _DupExtras(_SimpleScanner):
        EXTRA_RESULT_COLUMNS = ["symbol", "latest_close", "latest_close"]

    columns = _DupExtras().result_columns
    # "symbol" already in common schema → must not appear twice.
    assert columns.count("symbol") == 1
    # "latest_close" listed twice in extras → must collapse to one.
    assert columns.count("latest_close") == 1


def test_empty_result_returns_dataframe_with_correct_columns():
    scanner = _SimpleScanner()
    empty = scanner.empty_result()
    assert isinstance(empty, pd.DataFrame)
    assert empty.empty
    assert list(empty.columns) == scanner.result_columns


# ---------------------------------------------------------------------------
# coerce_param
# ---------------------------------------------------------------------------


def test_coerce_param_uses_default_when_missing():
    scanner = _SimpleScanner()
    assert scanner.coerce_param({}, "threshold", float) == 5.0


def test_coerce_param_uses_override_when_present():
    scanner = _SimpleScanner()
    assert scanner.coerce_param({"threshold": 9.5}, "threshold", float) == 9.5


def test_coerce_param_coerces_type():
    scanner = _SimpleScanner()
    # Coerce "7" (string) to int — the registry/UI can pass numeric strings.
    assert scanner.coerce_param({"lookback": "7"}, "lookback", int) == 7


def test_coerce_param_raises_for_unknown_key():
    scanner = _SimpleScanner()
    with pytest.raises(KeyError):
        scanner.coerce_param({}, "not_a_param", int)


# ---------------------------------------------------------------------------
# Template run() behavior
# ---------------------------------------------------------------------------


def test_run_returns_rows_for_passing_symbols_only():
    frames = {
        "BUY": _candles([1.0, 2.0, 10.0]),   # latest close 10 ≥ 5.0
        "HOLD": _candles([1.0, 1.0, 1.0]),   # latest close 1 < 5.0
    }
    scanner = _SimpleScanner()
    result = scanner.run(pd.DataFrame(), _FakeLoader(frames), _params())

    assert list(result["symbol"]) == ["BUY"]
    assert list(result.columns) == scanner.result_columns


def test_run_returns_properly_shaped_empty_frame_when_nothing_matches():
    frames = {"HOLD": _candles([1.0, 1.0, 1.0])}
    scanner = _SimpleScanner()
    result = scanner.run(pd.DataFrame(), _FakeLoader(frames), _params())

    assert result.empty
    # An empty result must still expose the screener's column schema.
    assert list(result.columns) == scanner.result_columns


def test_run_swallows_per_symbol_exceptions(caplog):
    frames = {
        "A": _candles([1.0, 2.0]),
        "B": _candles([3.0, 4.0]),
    }
    scanner = _FailingScanner()
    with caplog.at_level("WARNING"):
        result = scanner.run(pd.DataFrame(), _FakeLoader(frames), _params())

    # No rows because every symbol raised; the loop logged and continued.
    assert result.empty
    # Both warnings should mention the symbol that failed.
    messages = " ".join(record.getMessage() for record in caplog.records)
    assert "A" in messages and "B" in messages


def test_run_forwards_progress_callback():
    frames = {"A": _candles([10.0, 10.0])}
    seen = []

    def callback(completed, total, symbol):
        seen.append((completed, total, symbol))

    scanner = _SimpleScanner()
    scanner.run(
        pd.DataFrame(),
        _FakeLoader(frames),
        _params(progress_callback=callback),
    )

    assert seen == [(1, 1, "A")]


def test_run_forwards_optional_max_symbols_to_loader():
    """`max_symbols` stays a supported params-level cap for tests/CLI callers."""
    frames = {
        "A": _candles([10.0, 10.0]),
        "B": _candles([10.0, 10.0]),
    }
    loader = _FakeLoader(frames)

    result = _SimpleScanner().run(
        pd.DataFrame(),
        loader,
        _params(max_symbols=1),
    )

    assert loader.last_max_symbols == 1
    assert result["symbol"].tolist() == ["A"]


def test_run_reports_compute_failures_through_callback():
    """Compute failures should be visible to the app, not only terminal logs."""
    frames = {"A": _candles([1.0, 2.0])}
    failures = []

    result = _FailingScanner().run(
        pd.DataFrame(),
        _FakeLoader(frames),
        _params(compute_failure_callback=failures.append),
    )

    assert result.empty
    assert failures == [
        {
            "symbol": "A",
            "scanner": "_FailingScanner",
            "message": "intentional failure on A",
        }
    ]


# ---------------------------------------------------------------------------
# Abstract instantiation guard
# ---------------------------------------------------------------------------


def test_cannot_instantiate_without_compute_signal():
    class _Incomplete(BaseScanner):
        SCREENER = {
            "key": "incomplete",
            "name": "Incomplete",
            "description": "",
            "universe": "test",
            "timeframe": "daily",
            "lookback_days": 1,
            "default_params": {},
        }
        # compute_signal NOT overridden — Python should refuse to instantiate.

    with pytest.raises(TypeError):
        _Incomplete()
