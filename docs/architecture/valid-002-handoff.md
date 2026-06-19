# VALID-002 — Implement forward-return calculator · Handoff brief (for Codex)

| | |
|---|---|
| **Ticket** | VALID-002 — Implement forward-return calculator |
| **Type / Priority** | Story · P1 |
| **Owner / Reviewer** | **Codex** / Claude |
| **Depends on** | VALID-001 (methodology + schema — **landed**: `SignalForwardReturn` in `backend/storage/models.py`) |
| **Unblocks** | VALID-003A (backend aggregate metrics) and later VALID-003 UI / sector concentration |
| **Implementation status** | Implemented in the stacked Codex PR as `backend/validation/` + repository helpers + tests |

> Goal (from the backlog): *Compute future returns for stored signals.*
> Acceptance: can calculate forward return for N trading days · handles missing future data
> gracefully · supports benchmark comparison · unit tests cover edge cases.

**Read first:** the methodology is fully specified in
[`valid-001-forward-return-validation.md`](valid-001-forward-return-validation.md). This brief
is the *build plan*; that doc is the *contract*. Where they ever disagree, the design doc wins —
flag it in §7.

---

## 0. What already exists (your starting point)

VALID-001 shipped the **schema + its migration** (the migration-drift CI guard requires both to
land together — see §5 gotcha #2):
- `SignalForwardReturn` table (`signal_forward_returns`) + `ForwardReturnStatus` enum in
  [`backend/storage/models.py`](../../backend/storage/models.py) — columns per design §5.1.
- The `ScanResult.forward_returns` relationship (cascade delete-orphan) and the new
  `ix_scan_results_symbol_signal_date` composite index (design §5.2).
- The Alembic migration
  [`20260618valid001_create_signal_forward_returns.py`](../../migrations/versions/20260618valid001_create_signal_forward_returns.py)
  — the table + index already build via `alembic upgrade head`.
- Both models re-exported from `backend.storage` (`SignalForwardReturn`, `ForwardReturnStatus`).
- Round-trip + enum + unique + cascade tests in
  [`tests/test_scan_persistence_models.py`](../../tests/test_scan_persistence_models.py) — the
  pattern to reuse for your service tests.

There is **no** calculator, service, benchmark config, or repository helper yet — that is this
ticket. The table already exists, so you do **not** write a migration unless you change the
schema. Infrastructure you will build on (don't reinvent):
- `DailyDataLoader.get_daily_history(instrument, start_date, end_date) -> (frame, from_cache)`
  — [`backend/daily_data_loader.py:262`](../../backend/daily_data_loader.py). Returns daily
  candles with columns `timestamp, open, high, low, close, volume` (naive India-market dates).
- `load_universe(universe_key) -> DataFrame` and `mapped_only(...)` —
  [`backend/universe_loader.py:28`](../../backend/universe_loader.py). Resolves a `symbol` to its
  `security_id` / `exchange_segment` / `instrument_type` (what the loader needs).
- `prepare_ohlc(candles)` — [`backend/indicators.py`](../../backend/indicators.py). Sort, dedupe,
  coerce OHLC to float64. Run it before indexing bars.
- The repository layering (`backend/storage/repository.py`) and the `db_session` test fixture
  ([`tests/conftest.py`](../../tests/conftest.py)).

**Boundary to keep:** VALID-002 delivers the *pure calculator + benchmark config + service +
repository helpers + tests*. It does **not** build aggregate dashboards, a Streamlit
page, sector concentration, or the trigger that *schedules* the calculator — those are
**VALID-003**. "Compute future returns for stored signals" is satisfied by the service filling
`signal_forward_returns` rows plus tests proving it.

---

## 1. File plan

| File | Action |
|---|---|
| `backend/validation/__init__.py` | **New** — package surface; re-export the public calculator/service/config names. |
| `backend/validation/forward_return.py` | **New** — the **pure** calculator (no DB, no network) + `ForwardReturnPoint` dataclass. |
| `backend/validation/benchmarks.py` | **New** — `BENCHMARKS` registry + `benchmark_for_universe()` + `BenchmarkSpec`. |
| `backend/validation/service.py` | **New** — load pending signals → load candles (cache-first) + benchmark → compute → upsert. |
| `backend/storage/repository.py` | **Edit** — add `get_signals_needing_forward_returns()` + `upsert_forward_return()` (queries live ONLY here). |
| `backend/storage/__init__.py` | **Edit** — re-export the two new repository helpers. |
| `tests/test_forward_return_calculator.py` | **New** — pure-function edge cases (synthetic frames, no DB). |
| `tests/test_forward_return_service.py` | **New** — end-to-end on in-memory SQLite with a `FakeDataLoader`. |

---

## 2. Code skeletons

### 2.1 `backend/validation/forward_return.py` — the pure calculator
The heart of the ticket, and the easiest to test, *because it touches nothing but a DataFrame*.
Keep it free of DB/network/Streamlit so the edge cases in §3 are plain unit tests.

```python
"""VALID-002 — pure forward-return math over a single symbol's candle frame.

No database, no network, no Streamlit. Given the bars a screener already loads, it
answers: "if you entered this signal at the next day's open, what had happened N
trading days later?" Purity is the point — every no-lookahead rule from the design
(valid-001-forward-return-validation.md §4) is enforceable and testable here.
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from backend.indicators import prepare_ohlc
from backend.storage.models import ForwardReturnStatus

# Trading-day horizons (design §3.1). A service constant, not a schema CHECK, so a new
# horizon needs no migration (design §5.3).
FORWARD_RETURN_HORIZONS: tuple[int, ...] = (20, 60, 120)


@dataclass(frozen=True)
class ForwardReturnPoint:
    """One horizon's measurement, or a non-computed status with the reason implied."""

    horizon_days: int
    status: ForwardReturnStatus
    entry_date: dt.date | None = None
    exit_date: dt.date | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    forward_return_pct: Decimal | None = None
    max_adverse_excursion_pct: Decimal | None = None
    max_favorable_excursion_pct: Decimal | None = None


def compute_forward_return(
    candles: pd.DataFrame,
    signal_date: dt.date,
    horizon_days: int,
    *,
    as_of: dt.date | None = None,
) -> ForwardReturnPoint:
    """Measure the forward return for one signal at one horizon.

    Rules (design §3.3, §4):
      entry_index = signal_index + 1     -> entry_price = open[entry_index]
      exit_index  = signal_index + N     -> exit_price  = close[exit_index]
      forward_return_pct = (exit - entry) / entry * 100

    Status outcomes:
      - PENDING            : exit bar not present yet, OR exit_date > as_of (window
                             not elapsed) -> do NOT guess; retry later.
      - INSUFFICIENT_DATA  : signal_date not in frame, or no entry bar (signal is the
                             last bar), or the exit index is past the available bars
                             for a reason other than "not yet" (gap/delist).
      - COMPUTED           : entry+exit bars exist and the window has elapsed.

    MAE/MFE span the holding window [entry_index, exit_index] vs entry_price.
    Never forward-fill a missing bar (design §4 rule 3). Use Decimal for the money math.
    """
    frame = prepare_ohlc(candles)
    # 1. locate signal_index by DATE (frame timestamps are datetimes).
    # 2. entry_index/exit_index = +1 / +N; bounds-check -> PENDING vs INSUFFICIENT_DATA.
    # 3. as_of gate: if exit bar's date > (as_of or today) -> PENDING.
    # 4. compute return + MAE/MFE with Decimal(str(float_price)) to stay exact.
    ...
```

### 2.2 `backend/validation/benchmarks.py` — per-universe benchmark config
```python
"""VALID-002 — which index each universe's signals are compared against (design §6)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

import pandas as pd

from backend.indicators import prepare_ohlc


@dataclass(frozen=True)
class BenchmarkSpec:
    """An index expressed as an instrument the DailyDataLoader already understands."""

    key: str                      # stored in signal_forward_returns.benchmark_key
    symbol: str
    security_id: str              # <-- Dhan index instrument id; MUST be supplied (design §6)
    exchange_segment: str = "IDX_I"
    instrument_type: str = "INDEX"

    @property
    def instrument(self) -> dict[str, str]:
        return {
            "symbol": self.symbol,
            "security_id": self.security_id,
            "exchange_segment": self.exchange_segment,
            "instrument_type": self.instrument_type,
        }


# Leave security_id blank where unknown; a blank id takes the graceful-NULL path
# (design §4 rule 4), it does NOT crash. A future benchmark setup task fills real ids.
BENCHMARKS: dict[str, BenchmarkSpec] = {
    "fno":             BenchmarkSpec(key="nifty_50",  symbol="NIFTY 50",  security_id=""),
    "nifty_500":       BenchmarkSpec(key="nifty_500", symbol="NIFTY 500", security_id=""),
    "hemant_super_45": BenchmarkSpec(key="nifty_50",  symbol="NIFTY 50",  security_id=""),
    # ...one entry per known universe...
}


def benchmark_for_universe(universe_key: str) -> BenchmarkSpec | None:
    """Return the configured benchmark, or None (caller degrades gracefully)."""
    spec = BENCHMARKS.get(universe_key)
    return spec if (spec and spec.security_id) else None


@dataclass(frozen=True)
class BenchmarkLeg:
    benchmark_key: str
    entry_price: Decimal | None
    exit_price: Decimal | None
    return_pct: Decimal | None


def compute_benchmark_leg(
    benchmark_candles: pd.DataFrame,
    entry_date: dt.date,
    exit_date: dt.date,
    benchmark_key: str,
) -> BenchmarkLeg:
    """Benchmark return over the SAME entry_date->exit_date window (design §3.4).

    Look bars up by DATE (not bar offset) so per-symbol holiday differences stay
    aligned. Missing either date -> prices/return None (graceful), key still recorded.
    """
    frame = prepare_ohlc(benchmark_candles)
    ...
```

### 2.3 `backend/validation/service.py` — orchestration
```python
"""VALID-002 — fill signal_forward_returns for stored signals (design §7)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from sqlalchemy.orm import Session

from backend.daily_data_loader import DailyDataLoader
from backend.universe_loader import load_universe
from backend.validation.benchmarks import benchmark_for_universe, compute_benchmark_leg
from backend.validation.forward_return import (
    FORWARD_RETURN_HORIZONS,
    compute_forward_return,
)
from backend.storage.repository import (
    get_signals_needing_forward_returns,
    upsert_forward_return,
)


@dataclass
class ForwardReturnRunSummary:
    computed: int = 0
    pending: int = 0
    insufficient: int = 0


def compute_pending_forward_returns(
    session: Session,
    loader: DailyDataLoader,
    *,
    as_of: dt.date | None = None,
    horizons: tuple[int, ...] = FORWARD_RETURN_HORIZONS,
    limit: int | None = None,
) -> ForwardReturnRunSummary:
    """Load signals that still need measuring, compute each horizon, upsert the rows.

    Efficiency: group by (run.universe_key, symbol) so each symbol's candles load once
    (cache-first via get_daily_history with end_date = signal_date + buffer covering the
    max horizon), and each universe's benchmark loads once. Resolve the symbol's
    instrument via load_universe(run.universe_key). Idempotent: upsert on
    (result_id, horizon_days) so re-runs flip PENDING -> COMPUTED, never duplicate.
    """
    ...
```

### 2.4 `backend/storage/repository.py` — the only place that builds queries
```python
def get_signals_needing_forward_returns(
    session: Session,
    *,
    horizons: Sequence[int],
    limit: int | None = None,
) -> list[ScanResult]:
    """Signals missing a row, or still PENDING, for any requested horizon.

    Only signals with a non-null signal_date are eligible. Eager-load `.run` (its
    universe_key drives instrument + benchmark resolution) to avoid N+1 queries.
    """
    ...


def upsert_forward_return(
    session: Session,
    *,
    result_id: int,
    point: ForwardReturnPoint,
    benchmark: BenchmarkLeg | None = None,
) -> SignalForwardReturn:
    """Insert or update the (result_id, horizon_days) row from a computed point.

    Maps point + benchmark -> columns; sets computed_at only when COMPUTED. Relies on
    the UNIQUE(result_id, horizon_days) constraint as the upsert key.
    """
    ...
```

### 2.5 Alembic migration — already shipped (VALID-001)
The `signal_forward_returns` table + the `ix_scan_results_symbol_signal_date` index are created
by the VALID-001 migration
[`20260618valid001_create_signal_forward_returns.py`](../../migrations/versions/20260618valid001_create_signal_forward_returns.py).
You do **not** write a migration for VALID-002 unless you change the schema. If you do (an extra
column, say), update `models.py` *and* add a follow-on migration in the same change — the
`test_migration_matches_orm_metadata` drift guard fails otherwise (gotcha #2).

---

## 3. Tests (acceptance lives here)

`tests/test_forward_return_calculator.py` — pure, synthetic frames, **no DB**. Build a tiny
OHLC frame with a helper and assert each branch:
- **Happy path** — known frame, `horizon_days=20` → exact `entry_price`/`exit_price` and
  `forward_return_pct` (Decimal), correct `entry_date`/`exit_date`, MAE ≤ 0, MFE ≥ 0. ✅ *N-day return*
- **Window not elapsed** — exit bar exists but its date > `as_of` (or fewer than N future bars
  with a recent last bar) → `PENDING`, prices None. ✅ *no lookahead*
- **Signal is the last bar** — no entry bar → `INSUFFICIENT_DATA`. ✅ *missing future data*
- **Gap / delisting mid-window** — frame ends before `exit_index` → `INSUFFICIENT_DATA`, no
  crash, nothing forward-filled. ✅ *handles missing future data gracefully*
- **Signal date absent from frame** → `INSUFFICIENT_DATA`.
- **Holiday gaps** — non-contiguous dates → trading-day count comes from bar positions, not
  calendar arithmetic.
- **Decimal exactness** — a price like `12.07` survives as `Decimal`, never binary float.

`tests/test_forward_return_service.py` — in-memory SQLite (`db_session`) + a `FakeDataLoader`
returning canned frames (mirror the existing FakeDataLoader test pattern):
- Seed a run + results, run `compute_pending_forward_returns`, assert rows upserted with the
  right statuses and a populated `benchmark_key` when the universe has a benchmark.
- **Benchmark missing** (blank `security_id` / loader returns empty) → `forward_return_pct`
  computed, `benchmark_return_pct`/`excess_return_pct` NULL. ✅ *supports benchmark comparison* (gracefully)
- **Idempotency** — run twice; a `PENDING` row with newly-available data flips to `COMPUTED`,
  and the row count does **not** grow (unique `(result_id, horizon_days)`).

*Existing behaviour still works* → you add files and two repository helpers; change no screener
and no existing service. The full suite (currently green, see CI) must stay green, and coverage
must stay ≥ 84% (CI gate) — the new pure calculator is cheap to cover.

---

## 4. Decisions to preserve (don't drift)

- **Entry = next-day open; exit = close at `signal_index + horizon_days`** (design §3.3). Do not
  silently switch to signal-day close.
- **Count trading days off the candle frame** (design §3.2) — do not add a market-calendar
  dependency, do not use calendar-day arithmetic.
- **Never impute a missing bar** (design §4 rule 3). Missing → `INSUFFICIENT_DATA`.
- **Benchmark degrades gracefully** (design §4 rule 4) — a missing benchmark NULLs only the
  benchmark/excess columns, never the stock return.
- **`Numeric`/`Decimal`, never float**, for every price/percentage column (SCAN-001 §4.4).
- **Queries live only in the repository**; the service/UI never write raw SQL (SCAN-001 §6).
- **Upsert on `(result_id, horizon_days)`** — idempotent re-runs, no duplicates.
- **Horizons are a service constant** (`FORWARD_RETURN_HORIZONS`), not a schema CHECK.

---

## 5. Gotchas

1. **Candle timestamps are datetimes, naive India-market time.** Compare on `.dt.date`, and run
   `prepare_ohlc(...)` first so bars are sorted/deduped before you index by position.
2. **The schema + migration must stay in lockstep.** `test_migration_matches_orm_metadata`
   asserts `alembic upgrade head` builds the *exact* same schema as the ORM. The VALID-001
   migration already satisfies it, so you need no migration unless you change `models.py` — and if
   you do, add the follow-on migration in the same change or this guard goes red (it's why the
   VALID-001 schema and migration shipped together rather than split).
3. **`get_daily_history` is cache-hit only when the file covers the whole requested range.** Ask
   for `end_date = signal_date + ~250 days` (120 trading days + holiday slack) so the 120-day
   horizon resolves from one fetch; a too-tight range forces avoidable refetches.
4. **`security_id`, not `symbol`, fetches candles.** `scan_results` stores only `symbol`; resolve
   the instrument via `load_universe(run.universe_key)`. A symbol absent from its (possibly
   re-mapped) universe → treat as `INSUFFICIENT_DATA`, don't crash.
5. **Lint scope** — `ruff`/`mypy`/`bandit` run over `backend` (and `ruff` over `tests`); keep the
   new `backend/validation/` package clean. The root-level `migrations/` dir is outside the lint
   target (SCAN-002 §5), so version files aren't linted — fine, since VALID-002 adds none.
6. **Coverage gate is 84%.** The pure calculator carries most of the risk and is the cheapest to
   cover exhaustively — lean your tests there.

---

## 6. Verification (run before requesting review)
```bash
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
alembic upgrade head && alembic downgrade base        # migration round-trips cleanly
```

---

## 7. Open questions for the reviewer (Claude)

- **Benchmark `security_id`s:** resolved as documented blanks. VALID-002 does not invent Dhan
  index IDs; blank IDs return `None` from `benchmark_for_universe`, so stock returns compute and
  benchmark/excess fields stay NULL.
- **`as_of` source:** resolved as an explicit service/calculator parameter defaulting to
  `date.today()`. Tests pin it.
- **Signal-day close comparison:** not added. Next-day open remains the single stored entry rule.
- **Trigger:** VALID-002 stops at the callable service. VALID-003 owns scheduling and UI.
