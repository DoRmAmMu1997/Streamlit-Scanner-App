# VALID-001 — Forward-return validation framework

| | |
|---|---|
| **Ticket** | VALID-001 — Design forward-return validation framework |
| **Type / Priority** | Story · P0 |
| **Owner / Reviewer** | Claude / Codex |
| **Status** | Design complete (methodology + schema stub + tests landed) |
| **Branch** | `claude/valid-001-forward-return-validation` |
| **Depends on** | SCAN-001…004 (run/result persistence), PROV-001/002 (provenance) |
| **Unblocks** | VALID-002 (forward-return calculator), VALID-003 (aggregate metrics + UI) |

---

## 1. Context & goal

The scanner now *remembers* every signal it fires: SCAN-001…004 persist one `scan_runs`
row per execution and one `scan_results` row per shortlisted stock, and PROV-001/002 attach
the receipts (`triggered_rules`, `indicator_values`, `source`). What the app still cannot
answer is the question that actually matters to a trader: **did the signal work?**

VALID-001 defines the methodology for measuring that — *what happened to the price after the
signal* — and ships the schema that stores it, so the signal history becomes a back-testable
record instead of a write-only log. The defining risk in any such measurement is **lookahead
bias** (accidentally using information that wasn't available at signal time); §4 makes the
guarantees against it explicit, because "no lookahead bias" is a literal acceptance criterion.

This document is the VALID-001 deliverable. The schema is materialised as the
`SignalForwardReturn` ORM model in
[`backend/storage/models.py`](../../backend/storage/models.py); this doc explains the
**methodology, the entry/exit assumptions, the benchmark model, and the no-lookahead
guarantees**. The calculator that fills the rows is **VALID-002 (Codex)**; its build brief is
[`valid-002-handoff.md`](valid-002-handoff.md).

---

## 2. Scope

**In scope (VALID-001, this ticket — design):**
- The forward-return **methodology**: horizons, trading-day counting, the entry/exit rule,
  benchmark-relative return, and max adverse/favorable excursion (§3).
- The **no-lookahead guarantees** that make the measurement honest (§4).
- The **`signal_forward_returns` schema** + the `ForwardReturnStatus` lifecycle, materialised
  as a schema-only ORM stub plus the deferred `(symbol, signal_date)` index (§5).
- The **benchmark configuration model** VALID-002 will implement (§6).
- A schema round-trip test extending `tests/test_scan_persistence_models.py`.

**Out of scope (later tickets):**
- The forward-return **calculator + service + repository helpers** → **VALID-002 (Codex)**.
  (The Alembic migration that creates the table ships *here*, in VALID-001 — see §5.2/§9 —
  because the repo's migration-drift CI guard requires migrations and models to stay in lockstep,
  so a schema change cannot land without its migration.)
- **Aggregate metrics** across signals — hit rate, median/average return, sector
  concentration — and the Streamlit validation page → **VALID-003**. (Sector concentration
  additionally needs sector metadata that the universe CSVs do not currently carry; that is a
  VALID-003 data dependency, not a VALID-002 one.)
- The **scheduling/trigger** that runs the calculator as data arrives (a headless job, like the
  existing daily prefetch) → VALID-003+.
- Supplying real **benchmark `security_id`s** (Dhan index instrument IDs) → a noted setup task
  (§6); the design specifies the config *shape*, not the live values.

The schema stub deliberately stops at "schema only" — exactly as SCAN-001 did for SCAN-002 —
so this methodology can be reviewed and agreed before VALID-002 wires the calculator in.

---

## 3. Methodology

### 3.1 Horizons — in trading days

Each signal is measured at **20, 60, and 120 trading days** forward (the EPIC-5 metric list:
"20/60/120-day forward return"). One `scan_results` row therefore fans out to one
`signal_forward_returns` row **per horizon**.

These are **trading days, not calendar days**. A 20-trading-day window is ~28 calendar days;
counting calendar days would silently shorten the window across weekends and the many NSE
holidays.

### 3.2 Counting trading days — off the candle frame, no new dependency

The codebase has **no market-calendar library**, and VALID-001 does not add one
(`pandas_market_calendars` would be a new pinned dependency to maintain for a problem the data
already solves). Instead, count trading days off the symbol's **own daily candle frame** — the
same Parquet history every screener already uses:

```
signal_index = position of the bar whose timestamp.date() == signal_date
```

The Nth trading day after the signal is simply `signal_index + N` in that frame. This is
**holiday-correct by construction**: a day with no trading is not a bar, so it never counts.
It is also self-consistent — the window is measured in the same bars the signal was found in.

### 3.3 Entry and exit — explicit and reproducible

The **entry is the next trading day's open** (not the signal bar's close). A signal is only
*known* at the signal bar's close; you cannot transact at a price the bar already printed.
Entering at the next bar's open models the earliest fill a trader could actually achieve and
removes any same-bar lookahead. The exit is the close `horizon_days` trading days on:

| Quantity | Definition |
|---|---|
| `entry_index` | `signal_index + 1` |
| `entry_date`  | `date[entry_index]` |
| `entry_price` | `open[entry_index]` |
| `exit_index`  | `signal_index + horizon_days` |
| `exit_date`   | `date[exit_index]` |
| `exit_price`  | `close[exit_index]` |
| `forward_return_pct` | `(exit_price − entry_price) / entry_price × 100` |

**Worked example.** Signal fires on the bar at `signal_index = 100` (say 2026-01-05). For the
20-day horizon: enter at `open[101]` (2026-01-06's open, ₹100.00), exit at `close[120]`
(₹112.50). `forward_return_pct = (112.50 − 100.00) / 100.00 × 100 = +12.50%`. The holding
window for path metrics is the bars `[101, 120]`.

### 3.4 Benchmark-relative return

Raw return conflates stock selection with market drift. The **benchmark-relative (excess)
return** isolates the signal's edge:

```
excess_return_pct = forward_return_pct − benchmark_return_pct
```

The benchmark is resolved **per universe** (§6): a `nifty_500` signal is compared to NIFTY 500,
an F&O signal to NIFTY 50, etc. Critically, the benchmark return is measured over the **same
`entry_date` → `exit_date` window as the signal**, by looking the benchmark's own bars up by
those two dates — not by index offset. Because a stock and its index can have slightly
different holiday/halt calendars, aligning on dates (not bar counts) keeps the comparison
apples-to-apples. The benchmark's entry uses its open on `entry_date` and its exit its close on
`exit_date`, mirroring the signal leg.

### 3.5 Max adverse / favorable excursion (MAE / MFE)

Over the holding window `[entry_index, exit_index]`, relative to `entry_price`:

```
max_adverse_excursion_pct   = (min(low[entry_index..exit_index])  − entry_price) / entry_price × 100   # ≤ 0
max_favorable_excursion_pct = (max(high[entry_index..exit_index]) − entry_price) / entry_price × 100   # ≥ 0
```

MAE is the worst drawdown the trade would have shown intra-window (how much heat you'd have to
sit through); MFE is the best unrealised gain (whether a tighter exit would have captured more).
These satisfy the EPIC-5 "max adverse / favorable excursion" metrics and are stored per
horizon-row alongside the point-to-point return.

---

## 4. No-lookahead guarantees

"No lookahead bias" is an acceptance criterion, so the design pins it down as four rules the
VALID-002 calculator must honour. The `ForwardReturnStatus` enum exists precisely to encode
them honestly rather than papering over missing data:

1. **The signal bar is never a fill.** Entry is the *next* bar's open (§3.3). Information from
   the signal bar's close is used only to *select* the signal, never to *price* it.
2. **Only measure a window that has actually elapsed.** A row becomes `computed` only when
   `exit_index` exists in history **and** `exit_date ≤ data-as-of date`. If the window has not
   closed yet, the row stays `pending` with NULL prices — it is retried later, not guessed.
3. **Never impute or forward-fill a missing bar.** If the entry or exit bar does not exist
   (a delisting, a long halt, a data gap), the row is recorded as `insufficient_data`, not
   filled with the last known price. Imputing a price would invent a return the trade could
   never have realised.
4. **Benchmark degrades gracefully, never fabricates.** If benchmark data is missing for the
   window, the **stock leg is still valid**: `forward_return_pct` is computed and the
   benchmark/`excess_return_pct` columns are left NULL. A missing benchmark must never null out
   a real, measurable signal return.

Status lifecycle:

```
                 window elapsed,
                 entry+exit bars exist
   pending ───────────────────────────────▶ computed
      │
      │ entry/exit bar absent (delisted/halted/gap)
      └───────────────────────────────────▶ insufficient_data
```

`pending → computed` is the normal path as data arrives; `pending → insufficient_data` is a
terminal, recorded fact. The `(result_id, horizon_days)` uniqueness (§5) makes re-running the
calculator idempotent: it upserts the existing row's status rather than appending duplicates.

---

## 5. The schema

One signal has many forward-return measurements (one per horizon).
`signal_forward_returns.result_id` references `scan_results.id` with `ON DELETE CASCADE`, so
deleting a run (and thus its results) removes the forward-return rows — no orphans.

```
scan_runs (1) ──< scan_results (1) ──< signal_forward_returns (many)
                       id  ◄────────── result_id  (FK, ON DELETE CASCADE)
```

### 5.1 `signal_forward_returns` — one measurement per signal per horizon

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `id` | BigInt PK¹ | no | PK | Surrogate key. |
| `result_id` | BigInt FK¹ | no | ✓² | → `scan_results.id`, `ON DELETE CASCADE`. |
| `horizon_days` | Integer | no | ✓² | Forward window in **trading** days (20/60/120). |
| `status` | Enum(`forward_return_status`)³ | no | ✓ | `pending` \| `computed` \| `insufficient_data`. |
| `entry_date` | Date | yes | — | Next trading day after the signal. |
| `exit_date` | Date | yes | — | `horizon_days` trading days after the signal. |
| `entry_price` | Numeric(18,4) | yes | — | Open of the entry bar (exact, not float). |
| `exit_price` | Numeric(18,4) | yes | — | Close of the exit bar. |
| `forward_return_pct` | Numeric(9,4) | yes | — | `(exit−entry)/entry × 100`. |
| `benchmark_key` | String(50) | yes | — | Index used, e.g. `nifty_50`. |
| `benchmark_entry_price` | Numeric(18,4) | yes | — | Benchmark open on `entry_date`. |
| `benchmark_exit_price` | Numeric(18,4) | yes | — | Benchmark close on `exit_date`. |
| `benchmark_return_pct` | Numeric(9,4) | yes | — | Benchmark return over the same window. |
| `excess_return_pct` | Numeric(9,4) | yes | — | `forward_return_pct − benchmark_return_pct`. |
| `max_adverse_excursion_pct` | Numeric(9,4) | yes | — | Worst intra-window move vs entry (MAE). |
| `max_favorable_excursion_pct` | Numeric(9,4) | yes | — | Best intra-window move vs entry (MFE). |
| `computed_at` | DateTime(tz) | yes | — | UTC time the row was last (re)computed; NULL while pending. |
| `created_at` | DateTime(tz) | no | — | UTC row-creation time (ORM default). |

¹ `BigInteger` on Postgres, `Integer` on SQLite — same `BigIntPrimaryKey` variant as SCAN-001.
² A `UNIQUE(result_id, horizon_days)` constraint enforces "one measurement per signal per
  horizon" and — leading with `result_id` — also serves the "all horizons for this signal"
  lookup, so no separate `result_id` index is declared (it would be redundant).
³ Stored as a VARCHAR + CHECK (`native_enum=False`), exactly like `ScanStatus` — see SCAN-001 §4.3.

### 5.2 The deferred `scan_results(symbol, signal_date)` index

SCAN-001 §4.6 deliberately deferred a `(symbol, signal_date)` composite index "to VALID-*,
when queries that need them actually exist." This is that moment: the calculator looks up
*every signal for symbol S on/after date D* to fetch the bars that follow each one, and the
single-column `symbol` index cannot serve that date-bounded scan efficiently. VALID-001 adds
the composite index `ix_scan_results_symbol_signal_date` on `scan_results` (declared in
`ScanResult.__table_args__`). The VALID-001 Alembic migration
(`migrations/versions/20260618valid001_create_signal_forward_returns.py`) realises both this index
and the new table.

### 5.3 What is *not* a constraint

No `CHECK` pins `horizon_days` to exactly {20, 60, 120}; the set is a service-layer convention
(`FORWARD_RETURN_HORIZONS`) so VALID-003 can add a horizon without a schema migration — the
same evolvability rationale SCAN-001 used for the CHECK-backed status enum.

---

## 6. Benchmark model

The codebase has **no benchmark concept today** (no index ticker management, no
benchmark-relative math). VALID-001 specifies the config shape; VALID-002 implements it in a
new `backend/validation/benchmarks.py`:

```python
# universe_key -> the index instrument to compare its signals against.
BENCHMARKS: dict[str, BenchmarkSpec] = {
    "fno":             BenchmarkSpec(key="nifty_50",  symbol="NIFTY 50",  security_id="<SET ME>", ...),
    "nifty_500":       BenchmarkSpec(key="nifty_500", symbol="NIFTY 500", security_id="<SET ME>", ...),
    "hemant_super_45": BenchmarkSpec(key="nifty_50",  symbol="NIFTY 50",  security_id="<SET ME>", ...),
    # ...one entry per known universe...
}

def benchmark_for_universe(universe_key: str) -> BenchmarkSpec | None: ...
```

A `BenchmarkSpec` is just an instrument the existing `DailyDataLoader` already understands —
`symbol`, `security_id`, `exchange_segment`, `instrument_type` — plus a stable `key` stored in
`signal_forward_returns.benchmark_key`. The benchmark's candles are fetched and cached through
the **same loader and Parquet cache** as any stock (an index is just another instrument to
Dhan), so no new data path is introduced.

**Setup task (flagged, not invented here):** the index `security_id`s must be supplied — these
are Dhan index instrument IDs from the scrip master (e.g. the `IDX_I` segment), and inventing
them risks a wrong-instrument comparison. VALID-002 leaves them as explicit placeholders and
treats an unresolved/blank `security_id` as "no benchmark available" — which, by rule 4 of §4,
degrades gracefully (stock return computed, benchmark columns NULL) rather than failing.

If `benchmark_for_universe` returns `None` (a universe with no mapping yet), the same graceful
path applies. Per-universe mapping (vs one global benchmark) was chosen so a broad-market
shortlist isn't unfairly compared to a large-cap index, or vice-versa.

---

## 7. Data sourcing & reproducibility

Forward prices come from the **existing daily-candle cache**, read **cache-first** through the
loader the screeners already use — no new fetch path:

- **Instrument resolution.** A `scan_results` row stores only `symbol`; fetching its bars needs
  the Dhan `security_id`. Resolve it from the parent run's `universe_key` via
  `load_universe(run.universe_key)` ([`backend/universe_loader.py`](../../backend/universe_loader.py))
  and match the row for `symbol`.
- **Candle fetch.** `DailyDataLoader.get_daily_history(instrument, start_date, end_date)`
  ([`backend/daily_data_loader.py`](../../backend/daily_data_loader.py)) returns the daily frame
  sliced to a range and a `from_cache` flag. Request `start_date = signal_date` and a generous
  `end_date` (≥ the max horizon in calendar terms, e.g. `signal_date + ~250 days` to cover 120
  trading days plus holidays), then index by bar position per §3.2. The 10-year cache
  (`DEFAULT_HISTORY_YEARS_BACK`) means historical signals' forward windows are already on disk.
- **Benchmark fetch.** Identical call with the `BenchmarkSpec` instrument; loaded **once per
  universe** per run, not per signal.

Reproducibility holds because the inputs are pinned: the signal's `signal_date` and the
deterministic bar-offset rule fully determine entry/exit, and `computed_at` records when the
measurement was taken. Re-running over the same cache yields the same `forward_return_pct`.

---

## 8. Acceptance-criteria mapping

| VALID-001 acceptance criterion | How this design satisfies it |
|---|---|
| **Validation method is documented** | §3 (horizons, trading-day counting, entry/exit, benchmark-relative, MAE/MFE) with worked numbers; §5 schema; §7 data sourcing. |
| **Entry/exit assumptions are explicit** | §3.3 table: entry = next-day open, exit = close at `signal_index + N`; rationale stated (earliest realisable fill, no same-bar lookahead). |
| **Benchmark comparison is included** | §3.4 excess return + §6 per-universe benchmark config; `benchmark_*` and `excess_return_pct` columns in §5.1. |
| **No lookahead bias** | §4's four rules, encoded by the `ForwardReturnStatus` lifecycle (`pending`/`computed`/`insufficient_data`) and the next-day-open entry. |

---

## 9. Files in this change

| File | Change |
|---|---|
| `backend/storage/models.py` | **Edit** — add `ForwardReturnStatus` + `SignalForwardReturn`, the `ScanResult.forward_returns` relationship + `ix_scan_results_symbol_signal_date` index, and a "NEXT: VALID-002" handoff block. |
| `backend/storage/__init__.py` | **Edit** — re-export `SignalForwardReturn` + `ForwardReturnStatus`. |
| `migrations/versions/20260618valid001_create_signal_forward_returns.py` | **New** — hand-written migration: the `signal_forward_returns` table + the `ix_scan_results_symbol_signal_date` index. |
| `tests/test_scan_persistence_models.py` | **Edit** — add a `signal_forward_returns` round-trip + cascade test (also the VALID-002 test template). |
| `tests/test_scan_storage_migrations.py` | **Edit** — extend the table-set / index / FK assertions to cover the new table (the migration-drift guard now compares it too). |
| `docs/architecture/valid-001-forward-return-validation.md` | **New** — this design doc. |
| `docs/architecture/valid-002-handoff.md` | **New** — the VALID-002 build brief for Codex. |

The migration ships here rather than in VALID-002 (unlike the SCAN-001 → SCAN-002 split): the
repo's `test_migration_matches_orm_metadata` drift guard — added with SCAN-002, after SCAN-001 —
requires the migrations and the ORM to match at all times, so a schema change cannot land green
without its migration. VALID-002 keeps all the *logic* (calculator, service, benchmark,
repository helpers, tests).

---

## 10. Notes for the reviewer (Codex)

- **Entry = next-day open** is the one methodology choice with real alternatives (signal-day
  close is simpler; next-day close is more conservative). Next-day open was chosen as the
  earliest *realisable* fill with no same-bar lookahead. If you want signal-day close as an
  additional column for comparison, flag it — it's an additive change.
- **Benchmark `security_id`s are placeholders.** Do not invent them; an unresolved id must take
  the graceful-NULL path (§4 rule 4, §6), and a VALID-003 setup step fills the real values.
- **Horizons are a service constant, not a schema CHECK** (§5.3) — keep them as
  `FORWARD_RETURN_HORIZONS = (20, 60, 120)` so VALID-003 can add one without a migration.
- **MAE/MFE precision:** `Numeric(9,4)` matches `forward_return_pct`; revisit if you ever store
  basis-point-level precision.
- **Idempotency contract:** the calculator must *upsert* on `(result_id, horizon_days)`, never
  insert blindly, so re-runs flip `pending → computed` instead of duplicating. The unique
  constraint enforces it; the service should rely on it.
