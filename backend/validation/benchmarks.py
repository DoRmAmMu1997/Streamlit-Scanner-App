"""VALID-002 benchmark helpers for forward-return comparisons.

Beginner note:
A *benchmark* is the market index a signal's return is compared against (for
example NIFTY 50 for the F&O universe). The "excess return" the validation
dashboard shows is the stock's forward return minus this index's return over the
same dates.

To fetch an index's candles, Dhan needs its numeric ``security_id`` in the
``IDX_I`` (index) segment. Those ids are **verified from the Dhan instrument
master**, not guessed (VALID-002B), and stored in ``config/benchmarks.yaml`` so
they are reviewable and easy to update. If an id is blank or the config is
missing, ``benchmark_for_universe`` returns ``None`` and the caller keeps its
graceful-null behaviour (stock returns still compute; benchmark/excess stay null).
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

import pandas as pd
import yaml

from backend.config.settings import PROJECT_ROOT
from backend.validation._pricing import as_money, pct, prepared_frame

logger = logging.getLogger(__name__)

# Index instruments live in this Dhan instrument-master slice. The same three
# values identify an index row across any master snapshot, so the resolver below
# can extract verified ``security_id`` values without guessing.
_INDEX_EXCH_ID = "NSE"
_INDEX_SEGMENT = "I"
_INDEX_INSTRUMENT = "INDEX"


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


def _benchmarks_config_path() -> Path:
    """Return the path to the committed benchmark mapping file."""
    return PROJECT_ROOT / "config" / "benchmarks.yaml"


def load_benchmarks(path: Path | None = None) -> dict[str, BenchmarkSpec]:
    """Load the ``universe_key -> BenchmarkSpec`` mapping from committed config.

    Defensive on purpose: a missing or malformed config must not crash import or
    the validation job. Any problem logs a warning and returns an empty mapping,
    which makes every universe take the graceful-null path (no benchmark return)
    rather than raising. Entries keep whatever ``security_id`` the file provides
    (possibly blank); ``benchmark_for_universe`` is the single place that turns a
    blank/absent id into ``None``.
    """
    config_path = path or _benchmarks_config_path()
    try:
        data = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError):
        logger.warning(
            "Could not read benchmark config %s; benchmark-relative returns disabled.",
            config_path,
            exc_info=True,
        )
        return {}

    raw = data.get("benchmarks") if isinstance(data, Mapping) else None
    if not isinstance(raw, Mapping):
        logger.warning(
            "Benchmark config %s has no 'benchmarks' mapping; "
            "benchmark-relative returns disabled.",
            config_path,
        )
        return {}

    specs: dict[str, BenchmarkSpec] = {}
    for universe_key, entry in raw.items():
        if not isinstance(entry, Mapping):
            continue
        universe = _config_text(universe_key)
        symbol = _config_text(entry.get("symbol"))
        if not symbol:
            # Without an index symbol the entry cannot name a benchmark at all.
            continue
        specs[universe] = BenchmarkSpec(
            key=_config_text(entry.get("key")) or universe,
            symbol=symbol,
            security_id=_config_text(entry.get("security_id")),
        )
    return specs


def _config_text(value: object) -> str:
    """Normalize optional YAML scalar values without turning null into "None"."""
    return "" if value is None else str(value).strip()


# Loaded once at import from config/benchmarks.yaml. Kept module-level (like the
# previous literal) so callers and tests can read the resolved mapping directly.
BENCHMARKS: dict[str, BenchmarkSpec] = load_benchmarks()


def benchmark_for_universe(universe_key: str) -> BenchmarkSpec | None:
    """Return a configured benchmark only when its instrument id is usable."""
    spec = BENCHMARKS.get(universe_key)
    if spec is None or not spec.security_id.strip():
        return None
    return spec


def resolve_index_security_ids(
    instrument_master: pd.DataFrame,
    wanted: Sequence[str] = ("NIFTY 50", "NIFTY 100", "NIFTY 500"),
) -> dict[str, str]:
    """Resolve verified Dhan ``IDX_I`` index security ids from the instrument master.

    This is the "do not guess" half of VALID-002B: it reads ids straight from the
    Dhan master so the values committed to ``config/benchmarks.yaml`` are verified,
    and gives operators a one-call way to re-check them (see the validation LLD).

    It narrows the master to NSE index rows (``EXCH_ID=NSE``, ``SEGMENT=I``,
    ``INSTRUMENT=INDEX``) and matches each wanted name case-insensitively against
    ``SYMBOL_NAME`` **or** ``DISPLAY_NAME`` (NIFTY 50's trading symbol is ``NIFTY``
    while its display name is ``Nifty 50``, so both are checked). Only an
    unambiguous single id is returned; a missing or ambiguous name is omitted so
    callers keep graceful-null behaviour.

    Returns a ``{wanted_name: security_id}`` mapping (string ids, as Dhan uses).
    """
    # Imported lazily to avoid importing the universe builder (and its heavier
    # dependencies) just to import this validation helper.
    from backend.universe_builder import normalize_instrument_master_columns

    master = normalize_instrument_master_columns(instrument_master)
    index_rows = master.loc[
        (master["EXCH_ID"].str.upper() == _INDEX_EXCH_ID)
        & (master["SEGMENT"].str.upper() == _INDEX_SEGMENT)
        & (master["INSTRUMENT"].str.upper() == _INDEX_INSTRUMENT)
    ]

    resolved: dict[str, str] = {}
    for name in wanted:
        target = name.strip().casefold()
        matches = index_rows.loc[
            (index_rows["SYMBOL_NAME"].str.strip().str.casefold() == target)
            | (index_rows["DISPLAY_NAME"].str.strip().str.casefold() == target)
        ]
        # Collapse to the distinct ids found. Exactly one => a confident resolve;
        # zero (absent) or more than one (ambiguous) => leave it for graceful-null.
        security_ids = {sid for sid in matches["SECURITY_ID"].str.strip() if sid}
        if len(security_ids) == 1:
            resolved[name] = security_ids.pop()
    return resolved


def compute_benchmark_leg(
    benchmark_candles: pd.DataFrame,
    *,
    entry_date: dt.date,
    exit_date: dt.date,
    benchmark_key: str,
) -> BenchmarkLeg:
    """Compute the benchmark return by entry/exit date, not by bar offset."""
    frame = prepared_frame(benchmark_candles)
    if frame.empty:
        return BenchmarkLeg(benchmark_key, None, None, None)

    entry_row = _row_for_date(frame, entry_date)
    exit_row = _row_for_date(frame, exit_date)
    if entry_row is None or exit_row is None:
        return BenchmarkLeg(benchmark_key, None, None, None)

    entry_price = as_money(entry_row["open"])
    exit_price = as_money(exit_row["close"])
    if entry_price is None or exit_price is None or entry_price <= 0:
        return BenchmarkLeg(benchmark_key, None, None, None)

    return BenchmarkLeg(
        benchmark_key=benchmark_key,
        entry_price=entry_price,
        exit_price=exit_price,
        return_pct=pct(exit_price - entry_price, entry_price),
    )


def _row_for_date(frame: pd.DataFrame, wanted: dt.date) -> pd.Series | None:
    matches = frame.loc[frame["_date"] == wanted]
    if matches.empty:
        return None
    return matches.iloc[0]
