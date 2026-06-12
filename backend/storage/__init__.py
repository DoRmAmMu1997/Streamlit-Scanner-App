"""Persistence subsystem for scan history.

Beginner note:
Importing from ``backend.storage`` is the friendly public path. Internally the
package is split into small files:

- ``models.py`` owns table shapes.
- ``database.py`` owns connections and sessions.
- ``repository.py`` owns queries and writes.

Re-exporting the important names here lets future code use
``from backend.storage import session_scope, create_scan_run`` without needing
to remember which small file each helper lives in.

Public surface:
- `Base` — the SQLAlchemy declarative base; `Base.metadata` holds every table.
- `ScanRun` — one row per scan execution (the audit header).
- `ScanResult` — one row per shortlisted stock (the audit line item).
- `ScanStatus` — allowed run states (running / success / partial / failed).
- `SessionLocal` / `session_scope` — short-lived SQLAlchemy sessions.
- Repository helpers — the only public query/write helpers for scan history.
"""

from backend.storage.database import (
    SessionLocal,
    engine,
    ensure_database_schema,
    get_database_url,
    init_db,
    session_scope,
)
from backend.storage.models import Base, ScanResult, ScanRun, ScanStatus
from backend.storage.repository import (
    count_scan_results_for_runs,
    create_scan_run,
    finish_scan_run,
    get_latest_scan_runs,
    get_scan_results,
    list_distinct_screener_keys,
    list_distinct_triggered_by_values,
    list_distinct_universe_keys,
    save_scan_results,
)

__all__ = [
    "Base",
    "ScanResult",
    "ScanRun",
    "ScanStatus",
    "SessionLocal",
    "count_scan_results_for_runs",
    "create_scan_run",
    "engine",
    "ensure_database_schema",
    "finish_scan_run",
    "get_database_url",
    "get_latest_scan_runs",
    "get_scan_results",
    "init_db",
    "list_distinct_screener_keys",
    "list_distinct_triggered_by_values",
    "list_distinct_universe_keys",
    "save_scan_results",
    "session_scope",
]
