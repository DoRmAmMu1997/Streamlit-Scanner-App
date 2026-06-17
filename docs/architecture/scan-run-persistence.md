# SCAN-001 — Scan-run persistence schema

| | |
|---|---|
| **Ticket** | SCAN-001 — Design scan run persistence schema |
| **Type / Priority** | Story · P0 |
| **Owner / Reviewer** | Claude / Codex |
| **Status** | Design complete (schema + starter models + tests landed) |
| **Branch** | `claude/scan-001-scan-run-persistence-schema` |
| **Unblocks** | SCAN-002 (DB layer), SCAN-003 (service), SCAN-004 (history UI), PROV-*, VALID-*, RANK-* |

---

## 1. Context & goal

Today the scanner is **interactive-only**: you prefetch data, run a screener, look at
the shortlist, and the result evaporates. There is no record of *what* was scanned,
*with which parameters*, *against which data*, or *why a stock was shortlisted*.

SCAN-001 defines the data model that fixes this. With it, the app can answer
**"why did it shortlist this stock on 2026-06-03?"** months later — without re-running
today's (possibly changed) universe, data, or model. This is the *foundation stone* the
tech-lead called out: every later ticket (scheduled scans, history page, provenance,
forward-return validation, ranking) builds on these audit tables.

This document is the SCAN-001 deliverable. The schema is materialised as SQLAlchemy ORM
models in [`backend/storage/models.py`](../../backend/storage/models.py); this doc explains
the **design, the migration plan, and the model boundaries**.

---

## 2. Scope

**In scope (SCAN-001, this ticket — design):**
- The `scan_runs` and `scan_results` table schema: columns, types, nullability,
  constraints, indexes, relationships.
- A starter ORM materialisation (`backend/storage/models.py`) + a schema round-trip test.
- The migration plan (§5) and the model boundaries (§6).

**Out of scope (later tickets):**
- Engine, `Session` factory, Alembic env + migration files, the real DB file → **SCAN-002 (Codex)**.
- The scan service that *writes* runs/results → **SCAN-003**.
- The history page that *reads* them → **SCAN-004**.
- Populating strategy-specific deterministic provenance → **PROV-002** *(implemented:
  screeners emit `triggered_rules` + `indicator_values` via
  `BaseScanner.build_provenance`, carried on a reserved `provenance` row column and
  normalized into `provenance_json`; the two AI screeners label `source` hybrid/deterministic)*.
- Full AI verdict/evidence provenance → **PROV-003** *(implemented for the
  Technical Analysis and 67 Ka Funda screeners through the `ai_evaluations`
  ledger and approved-result receipts)*.
- Populating `final_score` → **RANK-***. User-action audit log → **OBS-003** *(implemented:
  standalone `audit_logs` + `app_config` tables on this same `Base` — see
  [obs-003-audit-log.md](obs-003-audit-log.md))*; per-user identity/roles → **AUTH-***.

The starter models intentionally stop at "schema only" so this design can be reviewed and
agreed before the implementation tickets wire a database into the app.

---

## 3. The schema

One run has many shortlisted results and many AI evaluations. Both child tables
reference `scan_runs.id` with `ON DELETE CASCADE`, so deleting a run removes its
complete audit trail with no orphans.

```
scan_runs (1) ─────< (many) scan_results
         (1) ─────< (many) ai_evaluations
```

### 3.1 `scan_runs` — one row per scan execution (the audit header)

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `id` | BigInt PK¹ | no | PK | Surrogate key (auto-increments). |
| `started_at` | DateTime(tz) | no | — | UTC start time. |
| `finished_at` | DateTime(tz) | yes | — | UTC end time; NULL while running / on crash. |
| `status` | Enum(`scan_status`)² | no | ✓ | `running` \| `success` \| `partial` \| `failed`. |
| `screener_key` | String(100) | no | ✓ | Screener registry key (e.g. `envelope`). |
| `universe_key` | String(100) | no | ✓ | Universe key (e.g. `nifty_500`). |
| `params_json` | JSON | yes | — | Screener parameters snapshot (replay). |
| `data_snapshot_date` | Date | yes | — | Trading date the candle data was current to. |
| `app_version` | String(50) | yes | — | App/release version that produced the run. |
| `git_commit_sha` | String(40) | yes | — | Full git commit SHA (code provenance). |
| `triggered_by` | String(100) | yes | — | Origin: `ui:<email>` / `cron` / `cli`. |
| `error_message` | Text | yes | — | Why a `partial`/`failed` run went wrong. |

### 3.2 `scan_results` — one row per shortlisted stock (the audit line item)

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `id` | BigInt PK¹ | no | PK | Surrogate key. |
| `run_id` | BigInt FK¹ | no | ✓ | → `scan_runs.id`, `ON DELETE CASCADE`. |
| `symbol` | String(50) | no | ✓ | Trading symbol (e.g. `RELIANCE`). |
| `signal_date` | Date | yes | —³ | Candle date the signal fired on. |
| `close_price` | Numeric(18,4) | yes | — | Price at the signal (exact, not float). |
| `rating` | String(20) | yes | — | Verdict label (`BUY`, `STRONG BUY`, …). |
| `final_score` | Numeric(6,2) | yes | — | Composite rank score (RANK-*); NULL for now. |
| `reason` | Text | yes | — | Plain-English reason for the shortlist. |
| `raw_result_json` | JSON | yes | — | Full raw screener output row (all extra columns). |
| `provenance_json` | JSON | yes | — | Receipts / evidence (PROV-001 contract). |
| `created_at` | DateTime(tz) | no | — | UTC row-creation time (ORM default). |

¹ `BigInteger` on Postgres, `Integer` on SQLite — see §4.1.
² Stored as a VARCHAR + CHECK (not a native PG enum) — see §4.3.
³ A `(symbol, signal_date)` index is deferred to VALID-* (the forward-return queries that
  will actually filter by date) to avoid speculative indexing now.

The first five `scan_results` business columns (`symbol`, `rating`, `signal_date`,
`close_price`, `reason`) deliberately mirror the app's existing screener output contract,
`backend.scanner_base.COMMON_RESULT_COLUMNS` = `["symbol", "rating", "signal_date",
"close", "reason"]`. Persisting a screener row is therefore a near 1:1 copy, with the
screener's own `EXTRA_RESULT_COLUMNS` captured in `raw_result_json`.

### 3.3 `ai_evaluations` — one row per attempted AI decision

This ledger records approved, rejected, and error outcomes before shortlist
filtering. It stores the code-stamped model and prompt version, full prompt hash,
validated verdict JSON, trusted provenance JSON, confidence, and UTC creation
time. `scan_results` remains shortlist-only. An approved AI decision therefore
appears in both places, while rejected and malformed/error decisions remain
auditable without being presented as signals.

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `id` | BigInt PK¹ | no | PK | Surrogate key. |
| `run_id` | BigInt FK¹ | no | ✓ | → `scan_runs.id`, `ON DELETE CASCADE`. |
| `symbol` | String(50) | no | ✓ | Stock the verdict is about. |
| `signal_date` | Date | yes | — | Candle date the verdict was based on. |
| `outcome` | String(16)² | no | ✓ | `approved` \| `rejected` \| `error`. |
| `verdict_label` | String(50) | yes | — | Screener verdict (e.g. AI pattern / approved). |
| `confidence` | Numeric(8,4) | yes | — | Model confidence (0–10), exact not float. |
| `model_name` | String(100) | no | — | LLM that produced the verdict (code-stamped). |
| `prompt_version` | String(100) | no | — | Semantic prompt version (code-stamped). |
| `validated_verdict_json` | JSON | no | — | The Pydantic-validated verdict object. |
| `provenance_json` | JSON | no | — | Trusted receipt: prompt SHA-256, evidence references (label/URL/hash), input-context hash, generated-at, cache flag. |
| `created_at` | DateTime(tz) | no | — | UTC row-creation time (ORM default). |

² Stored as VARCHAR + CHECK (`ck_ai_evaluations_outcome`), same portable pattern as `scan_runs.status` (§4.3).

Raw model responses and scraped snippets are not stored. Research evidence is
represented by sanitized source labels/URLs and full SHA-256 hashes. The on-disk
verdict cache that feeds this ledger is itself HMAC-signed and verified before
reuse, so a tampered cache entry is rejected and recomputed rather than trusted
(see `backend/ai_cache_integrity.py`; set `SCANNER_AI_CACHE_SIGNING_KEY` for a
restart-stable, cross-process key).

---

## 4. Design decisions (and why)

### 4.1 Surrogate `BigInteger` PK with a SQLite `Integer` variant
IDs are auto-incrementing surrogates (`BigInteger().with_variant(Integer, "sqlite")`).
`BIGINT` gives a practically unlimited id space on Postgres; on SQLite the `Integer`
variant makes the column an alias of the built-in `rowid`, which is what actually
auto-increments. Declaring `BIGINT PRIMARY KEY` directly on SQLite would *not* behave as a
rowid alias. UUID keys were considered and rejected: unnecessary for a single-writer
research app, and they bloat indexes. (UUIDs remain an easy future option if scans ever
run on multiple writers.)

### 4.2 Timezone-aware UTC timestamps
All datetimes use `DateTime(timezone=True)` and are written as `datetime.now(UTC)`. The app
already standardises on tz-aware UTC (see `backend/fundamentals/fundamentals_cache.py`), so
timestamps from a laptop, a cron box, and a cloud server compare correctly. (SQLite does not
physically store the offset; values are written as UTC, which keeps comparisons correct.)

### 4.3 `status` as an enum stored as a lowercase string
`ScanStatus` is a Python `enum.Enum`, but the column uses `Enum(..., native_enum=False)`,
which stores a small `VARCHAR` guarded by a `CHECK` constraint on **both** engines rather
than a native Postgres `ENUM` type. Rationale: native PG enums can only gain new members via
an `ALTER TYPE` migration, whereas a CHECK constraint is trivial to evolve. `values_callable`
makes the stored value the lowercase `.value` (`"running"`), so the raw data stays
human-readable and stable.

### 4.4 `Numeric`, never `float`, for money & scores
`close_price` and `final_score` are `Numeric` (fixed-point `Decimal`). Binary floats can't
represent values like `12.07` exactly; for prices and scores that feed decisions, that
rounding error is unacceptable. The round-trip test asserts `Decimal("12.07")` survives intact.

### 4.5 JSON columns for schema-flexible, AI-and-non-AI output
`params_json`, `raw_result_json`, `provenance_json`,
`validated_verdict_json`, and AI-evaluation `provenance_json` use the generic `JSON` type
(portable: stored as `TEXT` on SQLite, `json` on Postgres — `JSONB` is an easy Postgres-only
upgrade later). This is the key to **one schema serving every screener**: deterministic
screeners store triggered rules + indicator values; AI screeners store model name, prompt
version, source labels, and evidence hashes — all without per-screener tables or migrations.
It is also the seam PROV-001 plugs into without changing the schema.

### 4.6 Indexes chosen for known access patterns (not speculation)
- `scan_runs`: `status`, `screener_key`, `universe_key` are indexed because the history page
  (SCAN-004) filters by them.
- `scan_results`: `run_id` (load a run's results — the most common query) and `symbol`
  (a symbol's history across runs — the validation use case) are indexed.
- `ai_evaluations`: `run_id`, `symbol`, and `outcome` support ledger lookup and
  operational review.
- Deferred on purpose: a `(symbol, signal_date)` composite and any `final_score` sort index
  are left for VALID-*/RANK-*, when queries that need them actually exist.

### 4.7 Cascade delete, no orphans
Both child relationships use `cascade="all, delete-orphan"` (ORM level) and their FKs use
`ON DELETE CASCADE` (DB level, with `passive_deletes=True`). Deleting a run cleans up its
results either way. SQLite only enforces the DB-level cascade when `PRAGMA foreign_keys=ON`
is set — SCAN-002 sets that on the engine; the test demonstrates it.

---

## 5. Migration plan

**Tooling:** **Alembic + SQLAlchemy 2.0.** Alembic is SQLAlchemy's standard migration tool,
supports autogeneration from `Base.metadata`, and versions schema changes in code review —
which matters once real scan history exists and tables must evolve without data loss.

**Database URL & location:** read `DATABASE_URL` from the environment.
- Default (local/dev/test): `sqlite:///<DATA_DIR>/scanner.db`, where `DATA_DIR` is
  `backend.config.DATA_DIR` — so the DB sits beside the existing Parquet/JSON caches under
  `data/`. The file is already git-ignored (`data/*.db`, added in this ticket), so real scan
  history never gets committed.
- Deployment: a Postgres `DATABASE_URL` (DEPLOY-004 centralises this).

**Steps for SCAN-002 (Codex) to execute:**
1. Add `backend/storage/database.py`: `engine = create_engine(DATABASE_URL, future=True)`,
   `SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)`, and a `connect`
   event listener that runs `PRAGMA foreign_keys=ON` for SQLite.
2. `alembic init`, set `target_metadata = Base.metadata` (import `Base` from
   `backend.storage.models`).
3. Autogenerate the **initial migration**. It must create:
   - `scan_runs` (all columns in §3.1) with indexes on `status`, `screener_key`, `universe_key`;
   - `scan_results` (all columns in §3.2) with the FK (`ON DELETE CASCADE`) and indexes on
     `run_id`, `symbol`.
   Review the generated migration, then commit it.
4. SQLite is the dev/test database; Postgres is the deployment database. The same migration
   runs on both (every type in §4 is portable).

**Evolving the schema later:** because `status` is a CHECK-backed VARCHAR (§4.3), adding a
status value is a normal Alembic migration (no `ALTER TYPE`). New nullable columns
(e.g. populating `final_score`) are additive and backward-compatible.

---

## 6. Model boundaries

```
Streamlit UI / pages            ← reads only, never touches the DB directly
        │
        ▼
Scan service (SCAN-003)         ← orchestrates a run; owns the "create → run → save → finish" flow
        │
        ▼
Repository (SCAN-002/REFACTOR-002)  ← the ONLY place that builds queries / sessions
        │
        ▼
ORM models (SCAN-001 / PROV-003) ← table shapes: ScanRun, ScanResult, AIEvaluation, ScanStatus
        │
        ▼
Engine + Session (SCAN-002)     ← DATABASE_URL → SQLite (dev) / Postgres (prod)
```

**Rules this layering enforces:**
- The UI and screeners never import the engine or write SQL; they go through the service /
  repository. This keeps Streamlit reruns from leaking database sessions.
- **ORM models ≠ domain models.** PROV-001A defines typed *domain* objects
  (e.g. a `ScreenerResult` / `SignalProvenance` contract). Those are serialized into the
  `raw_result_json` / `provenance_json` columns — the JSON columns are the **seam** between
  the typed app layer and the stored rows, so the provenance contract can evolve without a
  schema migration.
- **Serialization is the writer's job.** JSON columns must receive JSON-safe values. The scan
  service converts `Decimal`/`date`/`datetime` to JSON-safe forms before storing (mirroring
  the existing cache's `json.dumps(..., default=str)` habit). The dedicated columns
  (`close_price`, `signal_date`, `rating`, …) stay strongly typed for querying.
- **Scraped / AI text is untrusted evidence.** Raw scraped text and model responses
  are transient only. Durable receipts contain validated verdict fields plus
  sanitized source labels/URLs and evidence hashes.

### 6.1 PROV-001A result normalization boundary

`backend/scanning/result_contract.py` supplies the domain models anticipated
above. It deliberately sits between flexible screener output and the repository:

1. A screener emits candidate row mappings.
2. `BaseScanner.build_result_frame(...)` validates and normalizes each accepted
   row before constructing the live DataFrame. Invalid rows are skipped and
   reported to the scan service.
3. `backend.scanning.service` creates separate row dictionaries for persistence;
   `normalize_screener_row(...)` redacts credential-shaped data and writes
   canonical `provenance_json`.
4. The repository stores that persistence copy in `raw_result_json` and extracts
   the same canonical provenance into `provenance_json`.
5. The validated DataFrame is returned to Streamlit. PROV-002 producers include
   a trailing internal `provenance` column, and transitional callers may include
   canonical `provenance_json`; render and CSV helpers remove both columns from
   display/export copies.

The word **copy** is important here. A pandas DataFrame is the live result that
the UI may sort, display, chart, or export. The persistence copy is a separate
tree of ordinary JSON values. Normalization never mutates pandas/NumPy values
inside the live frame; UI/export code explicitly strips internal provenance
columns before rendering or download.

The provenance envelope contains `screener_key`, optional
`screener_version`, `triggered_rules`, scalar `indicator_values`,
`params_snapshot`, `data_snapshot_date`, optional
`deterministic | ai | hybrid` source, optional notes, and a deliberately small
AI receipt with model, semantic prompt version, full prompt SHA-256, UTC
generation timestamp, cache status, decision fields, and hashed evidence
references. Unknown existing provenance fields are preserved.

---

## 7. Acceptance-criteria mapping

| Acceptance criterion | How the schema satisfies it |
|---|---|
| **Supports replay/audit of past runs** | `started_at`/`finished_at`, `params_json`, `data_snapshot_date`, `app_version`, `git_commit_sha`, `triggered_by` capture exactly *what ran, on what data, from what code, triggered by whom*. Results are append-only line items. |
| **Supports failed / partial / successful runs** | `status` enum (`running`→`success`/`partial`/`failed`) + `error_message`. A partial run keeps the `scan_results` it managed to produce before failing. |
| **Supports AI and non-AI scanner outputs** | Shared typed columns (`symbol`/`rating`/`signal_date`/`close_price`/`reason`/`final_score`) for both, plus `raw_result_json` (any screener's bespoke fields) and `provenance_json` (deterministic rules *or* AI model/prompt/sources). No per-screener table. |
| **Claude provides migration plan and model boundaries** | §5 (migration plan) and §6 (model boundaries). |

---

## 8. Verification

- Schema round-trip test: [`tests/test_scan_persistence_models.py`](../../tests/test_scan_persistence_models.py)
  — builds an in-memory SQLite DB from the models and checks the run/results round-trip,
  the enum's stored lowercase value, exact `Decimal` prices, and cascade delete.
  **Result: `4 passed`.**
- Repo-wide quality/security scan on this branch — **all green**:
  `compileall` ✓ · `ruff` ✓ · `bandit` ✓ · `pip-audit` ✓ ("No known vulnerabilities found",
  including the newly-pinned `SQLAlchemy==2.0.50`).

Run them yourself from the repo root:
```bash
python -m pytest -q tests/test_scan_persistence_models.py
python -m ruff check backend/storage tests/test_scan_persistence_models.py
python -m bandit -r backend/storage -q
```

---

## 9. Files in this change

| File | Change |
|---|---|
| `backend/storage/models.py` | **New** — ORM schema (`Base`, `ScanStatus`, `ScanRun`, `ScanResult`) + a "NEXT: SCAN-002" handoff checklist. |
| `backend/storage/__init__.py` | **New** — package surface re-exporting the models. |
| `tests/test_scan_persistence_models.py` | **New** — schema round-trip test (also the SCAN-002 test template). |
| `docs/architecture/scan-run-persistence.md` | **New** — this design doc. |
| `requirements.txt` | +`SQLAlchemy` (bare name, repo convention). |
| `constraints.txt` | +`SQLAlchemy==2.0.50` (verified pin). |
| `.gitignore` | +`data/*.db`, `*.sqlite`, `*.sqlite3` (future DB file must never be committed). |

---

## 10. Notes for the reviewer (Codex)

- **SQLAlchemy vs SQLModel:** chose SQLAlchemy 2.0 (standard, Alembic-native, no Pydantic
  coupling). The column design is identical under SQLModel, so the swap is mechanical if you
  prefer it for SCAN-002.
- **No `UNIQUE(run_id, symbol, signal_date)`:** runs are append-only and a screener may emit
  more than one row per symbol; de-duplication (if ever wanted) is a service concern, not a
  schema constraint. Flag if you disagree.
- **`rating` is a free string, not an enum:** AI screeners use varied vocabularies
  (`STRONG BUY`, `WATCH`, …); a hard enum would fight them. Revisit if a canonical set emerges.
- **`final_score Numeric(6,2)`** reserves the RANK-* column now so historical rows stay
  replay-stable once scoring exists; adjust precision when the scoring model is designed.
