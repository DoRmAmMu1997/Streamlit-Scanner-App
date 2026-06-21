# RANK-002 — Implement the scoring model · Handoff brief (for Codex)

| | |
|---|---|
| **Ticket** | RANK-002 — Implement the four-component scorer |
| **Type / Priority** | Story · P2 |
| **Status** | Implemented by `codex/rank-002-ranking-layer` (scorer, persistence receipt, scanner/history UI sort + component tables) |
| **Owner / Reviewer** | **Codex** / Claude |
| **Depends on** | RANK-001 (methodology — **landed**: [`rank-001-final-scoring-model.md`](rank-001-final-scoring-model.md)) · SCAN-001…004 (the `final_score` column + persistence) |
| **Unblocks** | RANK-003 (fundamental/valuation components) and later portfolio-aware ranking tickets |

> Goal (from EPIC 11): *Convert raw scanner results into a ranked shortlist.*
> Acceptance (RANK-002): `final_score` populated per row from the documented formula · component
> breakdown persisted · missing data degrades per design · raw `reason`/columns untouched · unit +
> integration tests cover the edge cases · all CI gates green.

**Read first:** the formula, ranges, missing-data rules, and the no-hidden-reasons invariant are
fully specified in [`rank-001-final-scoring-model.md`](rank-001-final-scoring-model.md). This brief
is the *build plan*; that doc is the *contract*. Where they ever disagree, the design wins — flag
it in §7.

---

## 0. What already exists (your starting point)

RANK-001 is **design only** — there is no `backend/scoring/` package yet; that is this ticket. But
the persistence surface is **already in place**, which is why RANK-002 needs **no migration**:

- The **`scan_results.final_score` column** (`Numeric(6,2)`, nullable) —
  [`backend/storage/models.py`](../../backend/storage/models.py), reserved by SCAN-001 "filled later
  by RANK-*".
- The typed **`ScreenerResult.final_score` field** —
  [`backend/scanning/result_contract.py`](../../backend/scanning/result_contract.py).
- The **provenance pipeline** — `normalize_screener_row` preserves unknown provenance keys and runs
  every value through redaction + NaN→null, so a `score_breakdown` block dropped into
  `provenance_json` persists safely **with no schema change** (design §5).

Infrastructure to build on (don't reinvent):
- **`rank_levels` / `_RELEVANCE_WEIGHTS`** — [`backend/indicators.py`](../../backend/indicators.py).
  The weighted-sum + relative-normalization precedent to mirror (weights sum to 1.0; sub-scores in a
  fixed range; `pd.to_numeric(errors="coerce")` for defensive parsing).
- **`prepare_ohlc(candles)`** — [`backend/indicators.py`](../../backend/indicators.py). Sort/dedupe/
  coerce OHLC before computing volatility or traded value.
- **`DailyDataLoader.get_daily_history(instrument, start_date, end_date) -> (frame, from_cache)`** —
  [`backend/daily_data_loader.py`](../../backend/daily_data_loader.py). Cache-first trailing candles
  for the liquidity/risk legs.
- **The YAML-config + null-safe loader pattern** — [`config/benchmarks.yaml`](../../config/benchmarks.yaml)
  + [`backend/validation/benchmarks.py`](../../backend/validation/benchmarks.py). Copy the shape and
  the "handle null config values" defaulting (VALID-002B).
- **`run_scan`** — [`backend/scanning/service.py`](../../backend/scanning/service.py). Your one call
  site (§2.4); note how it already copies caller params and never raises for failures.

**Implemented boundary:** RANK-002 delivers the *pure scorer + config + the one `run_scan` call +
scanner/history UI sort + component tables + tests + a component LLD*. It does **not** build
`fundamental_score`/`valuation_score` (→ **RANK-003**). "Convert raw results into a ranked
shortlist" is satisfied by `final_score` being populated, persisted, and used as the default
display/export ordering.

---

## 1. File plan

| File | Action |
|---|---|
| `backend/scoring/__init__.py` | **New** — package surface; re-export `score_candidates`, `ScoringContext`, `ScoringConfig`. |
| `backend/scoring/model.py` | **New** — `score_candidates(...)` orchestration + `ScoringContext` + the renormalized weighted-mean aggregation. |
| `backend/scoring/components.py` | **New** — the four **pure** component functions (no DB/network) + the cross-sectional and absolute normalizers. |
| `backend/scoring/config.py` | **New** — `ScoringConfig` dataclass + `load_scoring_config()` (null-safe YAML, mirrors `benchmarks.py`). |
| `config/scoring_model.yaml` | **New** — weights + params (design §6); defaults apply when absent. |
| `backend/scanning/service.py` | **Edit** — call `score_candidates` once, right after `run_callable` returns (§2.4), wrapped non-fatally. |
| `backend/scanning/result_contract.py` | **Edit (optional)** — add a typed `score_breakdown: Mapping \| None = None` to `SignalProvenance` (mypy-friendlier than a bare preserved key). |
| `app.py`, `ui/common.py`, `ui/history_page.py` | **Edit** — sort scanner/history tables by `final_score`, show the compact Score components expander, and keep CSV exports free of raw receipt dicts. |
| `tests/test_scoring_components.py` | **New** — pure-function edge cases (synthetic frames/sets, no DB). |
| `tests/test_scoring_model.py` | **New** — aggregation, renormalization, missing-data, determinism, "reason untouched." |
| `tests/test_scan_service.py` | **Edit** — assert `run_scan` populates `final_score`/`score_breakdown` and stays non-fatal when scoring fails. |
| `docs/architecture/components/scoring.md` | **New** — component LLD (purpose · position · interface · decisions · failure modes · config · UI behavior · testing · extension points). |
| `docs/architecture/README.md` · `high-level-design.md` | **Edit** — link the new LLD from the component map (RANK-001 already added the ticket-doc index rows). |

---

## 2. Code skeletons

### 2.1 `backend/scoring/components.py` — the pure heart (easiest to test)
Keep it free of DB/network/Streamlit so every design §3–4 edge case is a plain unit test.

```python
"""RANK-002 — pure scoring components and normalizers (design §3).

No database, no network, no Streamlit. Each function turns raw, possibly-dirty inputs
into a [0, 100] score, or signals "no input" so the aggregator can drop and renormalize.
Untrusted numbers are coerced with pd.to_numeric(errors="coerce") and NaN/inf treated as
missing (design §8) — a garbage value yields a dropped component, never inf or a crash.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

NEUTRAL = 50.0   # degenerate distribution: data present but no relative signal (design §3.2)


def cross_sectional(values: pd.Series) -> pd.Series:
    """Min–max normalize to [0, 100] across the run's candidates (design §3.2).

    NaN inputs stay NaN (a dropped component for that row). When max == min (all equal
    or a single candidate) every present value scores NEUTRAL, not 0/100.
    """
    v = pd.to_numeric(values, errors="coerce")
    lo, hi = v.min(), v.max()
    if not np.isfinite(lo) or not np.isfinite(hi) or hi <= lo:
        return v.where(v.isna(), NEUTRAL)
    return ((v - lo) / (hi - lo) * 100.0).clip(0, 100)


def liquidity_raw(candles: pd.DataFrame, window: int) -> float | None:
    """mean(volume × close) over the trailing `window` bars; None if too few bars."""
    ...


def risk_score_absolute(candles: pd.DataFrame, window: int, vol_cap: float) -> float | None:
    """100 × clamp(1 − σ/vol_cap, 0, 1) where σ = std of trailing daily log returns.

    None when there are too few bars to compute a volatility (design §4: drop, don't guess).
    """
    ...


def freshness_score_absolute(staleness_days: int | None, halflife_days: float) -> float | None:
    """100 × 0.5 ** (staleness/halflife). staleness from STORED dates, never now() (design §8)."""
    ...
```

### 2.2 `backend/scoring/config.py` — null-safe YAML (mirror benchmarks.py)
```python
"""RANK-002 — scoring weights + params from config/scoring_model.yaml (design §6)."""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_WEIGHTS = {"technical": 0.45, "risk": 0.25, "liquidity": 0.20, "freshness": 0.10}


@dataclass(frozen=True)
class ScoringConfig:
    model_version: str = "rank-1.0"
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_WEIGHTS))
    liquidity_window: int = 20
    risk_window: int = 60
    risk_vol_cap: float = 0.06
    freshness_halflife_days: float = 5.0


def load_scoring_config(path: str | None = None) -> ScoringConfig:
    """Read the YAML if present; fall back to defaults for any absent/null key.

    Use `yaml.safe_load` (NEVER `yaml.load`/`full_load`) — same as benchmarks.py:90 — so a
    config file can never deserialize arbitrary Python objects. Catch (OSError, yaml.YAMLError)
    and fall back to defaults, mirroring benchmarks.py. Reuse the VALID-002B lesson: a null
    value must NOT crash — coalesce to the default. Validate weights are finite and > 0;
    renormalize to sum 1.0 if they don't.
    """
    ...
```

### 2.3 `backend/scoring/model.py` — orchestration (never mutates the caller)
```python
"""RANK-002 — annotate a result frame with final_score + score_breakdown (design §3–4, §7)."""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

import pandas as pd

from backend.scoring.config import ScoringConfig
from backend.scoring.components import (
    cross_sectional, liquidity_raw, risk_score_absolute, freshness_score_absolute,
)


@dataclass(frozen=True)
class ScoringContext:
    universe_key: str
    data_loader: object                    # DailyDataLoader
    data_snapshot_date: dt.date | None
    config: ScoringConfig


def score_candidates(results: pd.DataFrame, *, context: ScoringContext) -> pd.DataFrame:
    """Return a COPY of `results` with `final_score` + a `score_breakdown` in each row's
    provenance. Cross-sectional components (technical, liquidity) are computed across the
    candidate set; absolute components (risk, freshness) per row. Weights renormalize over
    the components that have data (design §3.4). NEVER edits reason/raw columns (design §4).
    """
    if results is None or results.empty:
        return results
    out = results.copy()
    # 1. technical raw <- result-row signal fields; liquidity/risk raw <- trailing candles
    #    via context.data_loader; freshness <- (data_snapshot_date - signal_date).
    # 2. cross_sectional(technical), cross_sectional(log10(1+liquidity_raw)); risk/freshness absolute.
    # 3. per row: P = present components; final = Σ(w·s)/Σ(w) over P, round(2); NULL if P empty.
    # 4. write out["final_score"]; attach score_breakdown (components, weights_effective,
    #    coverage, missing, model_version) into the row's provenance dict.
    ...
    return out
```

### 2.4 `backend/scanning/service.py` — the single call site (non-fatal)
```python
results = run_callable(universe_df, data_loader, run_params)   # existing
# RANK-002: additive ranking annotation. Must never fail the scan (service owns failure
# observation; scoring failure -> NULL scores, rows + reasons intact). Mirror the existing
# try/except-and-log posture; emit an OBS-001 event on failure.
try:
    results = score_candidates(results, context=_build_scoring_context(universe_key, data_loader, run_params))
except Exception:                       # pragma: no cover - defensive; logged, never raised
    log_event(logger, EVENT_SCAN_..., level=logging.WARNING, run_id=run_id, phase="scoring")
```

---

## 3. Tests (acceptance lives here)

`tests/test_scoring_components.py` — pure, synthetic, **no DB**:
- **Cross-sectional happy path** — three distinct values → 0 / mid / 100 at the extremes. ✅ *formula*
- **Degenerate distribution** — all equal, and single-candidate → every present value `NEUTRAL` (50),
  not 0/100. ✅ *score ranges*
- **NaN / inf input** — `to_numeric` coerces → that row's component is NaN (dropped), no crash, no inf. ✅ *missing data / §8*
- **Risk absolute** — known σ → `100·clamp(1−σ/cap,0,1)`; too few bars → `None`. ✅ *ranges + missing*
- **Freshness decay** — `s=0 → 100`, `s=halflife → 50`; computed from passed dates (no `now()`). ✅ *determinism*

`tests/test_scoring_model.py`:
- **Renormalization** — drop one component, assert `final_score = Σ(w·s)/Σ(w)` over the rest and the
  breakdown lists `missing`. ✅ *missing-data behaviour*
- **No row dropped; NULL when empty** — a row with zero computable components → `final_score` NaN/NULL,
  row still present, `reason` unchanged. ✅ *never hide reasons*
- **`reason`/`raw_result_json` untouched** — byte-for-byte equal before/after scoring. ✅ *no hidden reasons*
- **Determinism** — scoring the same frame twice yields identical scores. ✅ *replay-stable*
- **Caller not mutated** — input DataFrame is unchanged (scorer returns a copy).

`tests/test_scan_service.py` (edit) — in-memory SQLite + a `FakeDataLoader` (reuse the existing
pattern): `run_scan` populates `final_score`/`score_breakdown`; a scorer that raises is swallowed and
the run still succeeds with NULL scores. ✅ *non-fatal*

Existing behaviour still works → you add a package, one config file, and one wrapped call; you change
no screener and no existing service logic. The full suite stays green and coverage stays **≥ 84%**
(CI gate) — the pure `components.py`/`model.py` carry the risk and are cheap to cover exhaustively.

---

## 4. Decisions to preserve (don't drift from the design)

- **Scale [0, 100], 2 dp**, fits `Numeric(6,2)` (§3.1). `final_score` NULL **only** when no component
  is computable (§4).
- **Renormalize over present components; never fabricate a missing input** (§3.4, §4). Degenerate
  distribution → NEUTRAL 50 is a *separate* case (data present, no relative signal).
- **Hybrid normalization** — technical/liquidity cross-sectional, risk/freshness absolute (§3.2–3.3).
- **Additive only** — set `final_score`, append `score_breakdown`; never edit/remove `reason`,
  `raw_result_json`, or any column (§4 rule 4).
- **No wall-clock** — freshness from stored dates; deterministic, replay-stable (§8).
- **Non-fatal** — scoring failure degrades to NULL scores, never fails the scan (§7).
- **No migration** — `final_score` exists; `score_breakdown` rides in `provenance_json` (§5). If you
  add a typed `SignalProvenance.score_breakdown` field, that's an ORM-*model* change with **no DB
  column**, so the drift guard is unaffected — but re-run the migration test to confirm.

---

## 5. Gotchas

1. **Don't mutate the caller's DataFrame.** `run_scan` reuses the result frame for charts; return a
   `copy()` (the service already guards its params dict the same way).
2. **Coverage gate is 84%.** Lean tests on the pure `components.py`/`model.py`; they're the cheapest
   to cover and hold most of the logic.
3. **Lint/type scope** — `ruff`/`mypy`/`bandit` run over `backend` (and `ruff` over `tests`). Keep the
   new `backend/scoring/` package clean; add no `# type: ignore` without a reason.
4. **Untrusted numbers** — always `pd.to_numeric(errors="coerce")` and treat NaN/inf as missing before
   normalizing; never let an input produce an `inf`/`NaN` `final_score` (§8, bandit-friendly).
5. **`score_breakdown` is JSON-strict.** It rides through `normalize_screener_row` → keep it to plain
   floats/strings/lists (no Decimal-NaN, no numpy scalars that don't unwrap); the pipeline coerces,
   but feed it clean values so nothing silently becomes `null`.
6. **Trailing candles need the instrument, not just the symbol** — resolve via the run's
   `universe_key` exactly as VALID-002 does (`load_universe`), and request a window long enough for
   `risk_window` bars.

---

## 6. Verification (run before requesting review)
```bash
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
```
(No `alembic` step — RANK-002 changes no schema.)

---

## 7. Open questions for the reviewer (Claude)

- **Weights / vol cap / halflife defaults** — design §6 values are a starting point; confirm or retune.
- **`score_breakdown` as a typed `SignalProvenance` field vs a preserved key** — preference?
- **`final_score` repository mapping** — confirm the save path already maps it from the normalized row
  (the `ScreenerResult` contract says so); if not, add the one-line repository mapping (no migration).
- **Technical raw inputs differ per screener** (`confidence`/`confirmed`/pattern fields aren't uniform)
  — agree on the per-screener field resolution (with a documented neutral fallback when absent).
