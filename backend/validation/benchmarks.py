"""VALID-002 benchmark helpers for forward-return comparisons."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pandas as pd

from backend.indicators import prepare_ohlc

_MONEY_QUANT = Decimal("0.0001")
_PCT_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class BenchmarkSpec:
    """An index instrument shaped for ``DailyDataLoader.get_daily_history``."""

    key: str
    symbol: str
    security_id: str
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"

    @property
    def instrument(self) -> dict[str, str]:
        """Return the loader-ready mapping without exposing config internals."""
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
        }


@dataclass(frozen=True)
class BenchmarkLeg:
    """Benchmark return over the same entry/exit dates as the stock leg."""

    benchmark_key: str
    entry_price: Decimal | None
    exit_price: Decimal | None
    return_pct: Decimal | None


# These entries intentionally leave security_id blank until the Dhan IDX_I
# instrument IDs are verified. VALID-002 still supports benchmark comparison;
# unresolved production IDs take the graceful-null path instead of guessing.
BENCHMARKS: dict[str, BenchmarkSpec] = {
    "nifty_100": BenchmarkSpec(key="nifty_100", symbol="NIFTY 100", security_id=""),
    "nifty_500": BenchmarkSpec(key="nifty_500", symbol="NIFTY 500", security_id=""),
    "fno": BenchmarkSpec(key="nifty_50", symbol="NIFTY 50", security_id=""),
    "hemant_super_45": BenchmarkSpec(key="nifty_50", symbol="NIFTY 50", security_id=""),
    "hemant_good_45": BenchmarkSpec(key="nifty_50", symbol="NIFTY 50", security_id=""),
    "hemant_good_200": BenchmarkSpec(key="nifty_50", symbol="NIFTY 50", security_id=""),
    "hemant_super_good_union": BenchmarkSpec(
        key="nifty_50", symbol="NIFTY 50", security_id=""
    ),
    "hemant_super_good_200_union": BenchmarkSpec(
        key="nifty_50", symbol="NIFTY 50", security_id=""
    ),
}


def benchmark_for_universe(universe_key: str) -> BenchmarkSpec | None:
    """Return a configured benchmark only when its instrument id is usable."""
    spec = BENCHMARKS.get(universe_key)
    if spec is None or not spec.security_id.strip():
        return None
    return spec


def compute_benchmark_leg(
    benchmark_candles: pd.DataFrame,
    *,
    entry_date: dt.date,
    exit_date: dt.date,
    benchmark_key: str,
) -> BenchmarkLeg:
    """Compute the benchmark return by entry/exit date, not by bar offset."""
    frame = _prepared_frame(benchmark_candles)
    if frame.empty:
        return BenchmarkLeg(benchmark_key, None, None, None)

    entry_row = _row_for_date(frame, entry_date)
    exit_row = _row_for_date(frame, exit_date)
    if entry_row is None or exit_row is None:
        return BenchmarkLeg(benchmark_key, None, None, None)

    entry_price = _as_money(entry_row["open"])
    exit_price = _as_money(exit_row["close"])
    if entry_price is None or exit_price is None or entry_price <= 0:
        return BenchmarkLeg(benchmark_key, None, None, None)

    return BenchmarkLeg(
        benchmark_key=benchmark_key,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=_pct(exit_price - entry_price, entry_price),
    )


def _prepared_frame(candles: pd.DataFrame) -> pd.DataFrame:
    try:
        frame = prepare_ohlc(candles)
    except (TypeError, ValueError):
        return pd.DataFrame()
    if frame.empty or "timestamp" not in frame.columns:
        return pd.DataFrame()

    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    valid = timestamps.notna()
    if not valid.any():
        return pd.DataFrame()
    prepared = frame.loc[valid].copy()
    prepared["_date"] = timestamps.loc[valid].dt.date
    return prepared.reset_index(drop=True)


def _row_for_date(frame: pd.DataFrame, wanted: dt.date) -> pd.Series | None:
    matches = frame.loc[frame["_date"] == wanted]
    if matches.empty:
        return None
    return matches.iloc[0]


def _as_money(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANT)
    except (InvalidOperation, ValueError):
        return None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return ((numerator / denominator) * Decimal("100")).quantize(_PCT_QUANT)
