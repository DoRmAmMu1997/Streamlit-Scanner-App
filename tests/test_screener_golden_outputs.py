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
    envelope_knoxville_buy,
    heikin_ashi_supertrend,
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


def _golden_cases() -> list[GoldenCase]:
    """Define the three P0 screener snapshots covered by TEST-001."""
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
