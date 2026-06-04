"""Persistence subsystem for scan history.

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
    get_database_url,
    init_db,
    session_scope,
)
from backend.storage.models import Base, ScanResult, ScanRun, ScanStatus
from backend.storage.repository import (
    create_scan_run,
    finish_scan_run,
    get_latest_scan_runs,
    get_scan_results,
    save_scan_results,
)

__all__ = [
    "Base",
    "SessionLocal",
    "ScanResult",
    "ScanRun",
    "ScanStatus",
    "create_scan_run",
    "engine",
    "finish_scan_run",
    "get_database_url",
    "get_latest_scan_runs",
    "get_scan_results",
    "init_db",
    "save_scan_results",
    "session_scope",
]
