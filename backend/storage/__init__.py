"""Persistence subsystem — the database schema for scan history.

Public surface (SCAN-001, schema only):
- `Base` — the SQLAlchemy declarative base; `Base.metadata` holds every table.
- `ScanRun` — one row per scan execution (the audit header).
- `ScanResult` — one row per shortlisted stock (the audit line item).
- `ScanStatus` — allowed run states (running / success / partial / failed).

The engine, session, migrations, and repository helpers are SCAN-002 (Codex) —
see the "NEXT: SCAN-002" block at the bottom of `models.py`.
"""

from backend.storage.models import Base, ScanResult, ScanRun, ScanStatus

__all__ = [
    "Base",
    "ScanResult",
    "ScanRun",
    "ScanStatus",
]
