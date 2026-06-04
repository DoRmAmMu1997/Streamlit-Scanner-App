# SCAN-002 — Implement database layer · Handoff brief (for Codex)

| | |
|---|---|
| **Ticket** | SCAN-002 — Implement database layer |
| **Type / Priority** | Story · P0 |
| **Owner / Reviewer** | **Codex** / Claude |
| **Depends on** | SCAN-001 (schema — **merged/landed**: `backend/storage/models.py`) |
| **Unblocks** | SCAN-003 (scan service), SCAN-004 (history page) |

> Goal (from the backlog): *Add persistent storage for scan runs and results.*
> Acceptance: app can create `scan_runs` records · app can create `scan_results` records ·
> failed scans recorded with `error_message` · existing scanner behaviour still works ·
> tests use a temporary SQLite database.

---

## 0. What already exists (your starting point)

SCAN-001 shipped the **schema only** in `backend/storage/models.py`:
- `Base` (declarative base; `Base.metadata` holds both tables)
- `ScanRun`, `ScanResult` (full columns — see the SCAN-001 design doc, §3)
- `ScanStatus` enum (`running`/`success`/`partial`/`failed`)
- `BigIntPrimaryKey` (the `BigInteger`+SQLite-`Integer` variant — reuse, don't redefine)

There is **no** engine, session, migration, or repository yet — that is this ticket.
`tests/test_scan_persistence_models.py` shows the in-memory-SQLite test pattern to reuse.

**Boundary to keep:** SCAN-002 delivers the *connection layer + repository + migrations +
tests*. It does **not** wire persistence into the live scan flow — that orchestration is
**SCAN-003**. "App can create records" is satisfied by the repository API plus tests proving
it works; calling it during a real scan is SCAN-003's job.

---

## 1. File plan

| File | Action |
|---|---|
| `backend/storage/database.py` | **New** — engine, `SessionLocal`, SQLite pragmas, `session_scope()`, `init_db()`. |
| `backend/storage/repository.py` | **New** — CRUD helpers (the only place that builds queries). |
| `backend/storage/__init__.py` | **Edit** — re-export the new public surface. |
| `migrations/` (+ `alembic.ini`, `migrations/env.py`, `migrations/versions/*.py`) | **New** — Alembic, initial migration. |
| `tests/test_scan_storage_repository.py` | **New** — repository round-trip on temp SQLite. |
| `tests/test_scan_storage_migrations.py` | **New (optional)** — `alembic upgrade head` builds the schema. |
| `requirements.txt` / `constraints.txt` | **Edit** — add `alembic` (bare) + a pinned version. |
| `README.md` | **Edit** — one paragraph: `DATABASE_URL`, `alembic upgrade head`. |

---

## 2. Code skeletons

### 2.1 `backend/storage/database.py`
```python
"""SCAN-002 — database engine, session factory, and SQLite pragmas.

The table *shapes* live in models.py (SCAN-001). This module is the *connection*
layer: it decides WHERE the data lives (DATABASE_URL) and hands out Sessions.
"""
from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DATA_DIR, _clean_env_value, load_environment
from backend.storage.models import Base


def get_database_url() -> str:
    """DATABASE_URL from env, else a local SQLite file under data/ (git-ignored)."""
    load_environment()  # reuse the app's single .env loader
    url = _clean_env_value(os.getenv("DATABASE_URL"))
    if url:
        return url
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(DATA_DIR / 'scanner.db').as_posix()}"


def _make_engine(url: str | None = None) -> Engine:
    url = url or get_database_url()
    # check_same_thread=False: Streamlit may touch the connection from worker threads.
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    eng = create_engine(url, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        @event.listens_for(eng, "connect")
        def _enable_sqlite_fk(dbapi_conn, _record):  # noqa: D401
            dbapi_conn.execute("PRAGMA foreign_keys=ON")  # honour ON DELETE CASCADE
    return eng


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Dev/test convenience only. PRODUCTION creates tables via Alembic, not this."""
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Short-lived transactional session. Open → use → commit/rollback → close.

    Streamlit reruns the script top-to-bottom on every interaction, so NEVER hold a
    Session across reruns. Use one scope per unit of work.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
```

### 2.2 `backend/storage/repository.py`
```python
"""SCAN-002 — repository: the only module that builds queries for scan history.

The UI/service call these helpers; they never write raw SQL or touch Sessions
directly. Keeps Streamlit reruns from leaking database state.
"""
from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.storage.models import ScanResult, ScanRun, ScanStatus


def create_scan_run(
    session: Session,
    *,
    screener_key: str,
    universe_key: str,
    params: dict | None = None,
    data_snapshot_date: dt.date | None = None,
    app_version: str | None = None,
    git_commit_sha: str | None = None,
    triggered_by: str | None = None,
) -> ScanRun:
    """Insert a RUNNING scan_runs row and return it (id populated via flush)."""
    run = ScanRun(
        started_at=dt.datetime.now(dt.UTC),
        status=ScanStatus.RUNNING,
        screener_key=screener_key,
        universe_key=universe_key,
        params_json=params,
        data_snapshot_date=data_snapshot_date,
        app_version=app_version,
        git_commit_sha=git_commit_sha,
        triggered_by=triggered_by,
    )
    session.add(run)
    session.flush()  # assigns run.id without ending the transaction
    return run


def save_scan_results(
    session: Session, run: ScanRun, rows: Sequence[dict]
) -> list[ScanResult]:
    """Map screener output rows → ScanResult and attach them to `run`.

    Each `row` is a BaseScanner result dict whose keys include the common contract
    (symbol, rating, signal_date, close, reason) plus the screener's EXTRA columns.
    NOTE the column rename: the screener key is `close`; the DB column is `close_price`.
    The ENTIRE row is also stored in raw_result_json so nothing is lost.
    """
    results: list[ScanResult] = []
    for row in rows:
        results.append(
            ScanResult(
                symbol=str(row["symbol"]),
                signal_date=_as_date(row.get("signal_date")),
                close_price=_as_decimal(row.get("close")),
                rating=row.get("rating"),
                reason=row.get("reason"),
                raw_result_json=_json_safe(dict(row)),  # keep every extra column
                # provenance_json is filled by PROV-002 / PROV-003 later.
            )
        )
    run.results.extend(results)
    session.flush()
    return results


def finish_scan_run(
    session: Session,
    run: ScanRun,
    *,
    status: ScanStatus,
    error_message: str | None = None,
) -> None:
    """Stamp finished_at + final status. Pass FAILED/PARTIAL + error_message on failure."""
    run.status = status
    run.finished_at = dt.datetime.now(dt.UTC)
    run.error_message = error_message
    session.flush()


def get_latest_scan_runs(session: Session, limit: int = 50) -> list[ScanRun]:
    stmt = select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def get_scan_results(session: Session, run_id: int) -> list[ScanResult]:
    stmt = select(ScanResult).where(ScanResult.run_id == run_id).order_by(ScanResult.symbol)
    return list(session.scalars(stmt))


# --- small JSON/typed-value helpers (keep DB columns strongly typed) -----------

def _as_date(value) -> dt.date | None:
    if value in (None, ""):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    # pandas.Timestamp and "YYYY-MM-DD" strings both parse via fromisoformat/str.
    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_decimal(value) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_safe(obj):
    """Recursively coerce Decimal/date/datetime/NumPy scalars to JSON-safe values."""
    # Implement: dict/list recursion; Decimal->float or str; date/datetime->isoformat();
    # numpy types -> .item(); fall back to str() for anything exotic. (Mirror the existing
    # cache convention of json.dumps(..., default=str) in fundamentals_cache.py.)
    ...
```

### 2.3 `backend/storage/__init__.py` (extend the re-exports)
Add `engine`, `SessionLocal`, `session_scope`, `init_db`, `get_database_url`, and the
repository functions to the package surface + `__all__` (keep the existing model exports).

### 2.4 Alembic
```bash
pip install alembic                 # add to requirements.txt + pin in constraints.txt
alembic init migrations             # root-level `migrations/` (see gotcha #5 re: lint)
```
- In `migrations/env.py`:
  ```python
  from backend.storage.models import Base
  from backend.storage.database import get_database_url
  target_metadata = Base.metadata
  config.set_main_option("sqlalchemy.url", get_database_url())  # read env, don't hardcode
  ```
- Generate + review the initial migration:
  ```bash
  alembic revision --autogenerate -m "create scan_runs and scan_results"
  alembic upgrade head
  ```
- **Verify the autogenerated migration** creates: both tables, the FK with
  `ondelete="CASCADE"`, the `status` CHECK/VARCHAR (native_enum=False), and the indexes on
  `scan_runs(status, screener_key, universe_key)` and `scan_results(run_id, symbol)`.

---

## 3. Tests (acceptance lives here)

`tests/test_scan_storage_repository.py` — reuse the SCAN-001 fixture pattern
(`create_engine("sqlite://")` + FK pragma + `Base.metadata.create_all`), then:
- `create_scan_run(...)` → row exists, `status == RUNNING`, `id` set. ✅ *create scan_runs*
- `save_scan_results(run, [det_row, ai_row])` → 2 rows linked, `close`→`close_price`,
  full row in `raw_result_json`. ✅ *create scan_results*
- `finish_scan_run(run, status=FAILED, error_message="boom")` → reload shows `failed`
  + message + `finished_at`. ✅ *failed scans recorded*
- `get_latest_scan_runs` / `get_scan_results` return the expected rows.
- `session_scope()` rolls back on exception (wrap a failing block, assert nothing persisted).

`tests/test_scan_storage_migrations.py` *(optional but recommended)* — point Alembic at a
temp SQLite file, run `command.upgrade(cfg, "head")`, assert both tables exist via
`inspect(engine).get_table_names()`. ✅ *tests use a temporary SQLite database*

*Existing scanner behaviour still works* → you change nothing in `screeners/` or
`scanner_base.py`; the full suite (currently **304 passing**) must stay green.

---

## 4. Decisions from SCAN-001 to preserve (don't drift)

- Reuse `BigIntPrimaryKey`, the `ScanStatus` enum, tz-aware UTC datetimes, and `Numeric`
  (never float) for money — already in the models; don't redefine or weaken them.
- **JSON columns must receive JSON-safe values** — convert `Decimal`/`date`/`datetime`/NumPy
  before storing (`_json_safe`). The typed columns stay typed; only the JSON blobs need coercion.
- Keep the **layering**: UI/service → repository → models → engine. No raw SQL in the UI; no
  Session held across a Streamlit rerun.
- If you find you need a **schema change**, that's a real change: add an Alembic migration AND
  update `models.py` + the SCAN-001 design doc — don't silently mutate the schema.

---

## 5. Gotchas

1. **Streamlit reruns** re-execute the module each interaction. A module-level `engine` is
   fine (created once on import); Sessions must be per-operation via `session_scope()`.
2. **SQLite foreign keys are OFF by default** — the `connect` pragma listener is what makes
   `ON DELETE CASCADE` actually fire. Keep it.
3. **SQLite + threads** — `connect_args={"check_same_thread": False}`.
4. **Alembic autogenerate is not perfect** — it can miss/rename CHECK constraints and server
   defaults. Eyeball the generated migration against models.py before committing it.
5. **Lint scope** — CI runs `ruff`/`bandit` over `backend` (and `ruff` over `tests`), but NOT
   over a root-level `migrations/` dir. Putting Alembic at repo-root `migrations/` keeps the
   autogenerated version files out of the lint target. If you instead nest it under
   `backend/storage/migrations/`, clean the generated files so `ruff` passes.
6. **pip-audit** now also audits `alembic` — a current release is clean; keep the pin updated.

---

## 6. Verification (run before requesting review)
```bash
python -m pytest -q                                   # full suite incl. new tests
python -m ruff check app.py backend screeners Dependencies tests
python -m bandit -r app.py backend screeners Dependencies -q
python -m pip_audit -r requirements.txt
alembic upgrade head && alembic downgrade base        # migration round-trips cleanly
```

## 7. Open questions for the reviewer (Claude)
- Alembic location: root `migrations/` (recommended, dodges the lint target) vs
  `backend/storage/migrations/`?
- `_json_safe` policy for `Decimal`: store as `float` (queryable-ish) or `str` (lossless)?
  Recommendation: `str` inside JSON blobs; the typed `close_price`/`final_score` columns are
  the source of truth for numbers.
- Confirm SCAN-002 stops at the repository (no live-scan wiring) and SCAN-003 owns the
  in-flow `create → save → finish` calls.
