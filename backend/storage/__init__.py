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
- `SignalForwardReturn` — one forward-return measurement per signal/horizon (VALID-001).
- `ForwardReturnStatus` — allowed measurement states (pending / computed / insufficient_data).
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
from backend.storage.models import (
    AIEvaluation,
    AppConfig,
    AuditLog,
    Base,
    ForwardReturnStatus,
    ScanResult,
    ScanRun,
    ScanStatus,
    SignalForwardReturn,
)
from backend.storage.repository import (
    ForwardReturnMetricRecord,
    count_scan_results_for_runs,
    create_audit_log_entry,
    create_scan_run,
    finish_scan_run,
    get_ai_evaluations,
    get_config_overrides,
    get_forward_return_metric_records,
    get_latest_scan_runs,
    get_recent_audit_logs,
    get_scan_results,
    get_signals_needing_forward_returns,
    list_distinct_audit_events,
    list_distinct_screener_keys,
    list_distinct_triggered_by_values,
    list_distinct_universe_keys,
    save_ai_evaluations,
    save_scan_results,
    set_config_override,
    upsert_forward_return,
)

__all__ = [
    "AIEvaluation",
    "AppConfig",
    "AuditLog",
    "Base",
    "ForwardReturnMetricRecord",
    "ForwardReturnStatus",
    "ScanResult",
    "ScanRun",
    "ScanStatus",
    "SessionLocal",
    "SignalForwardReturn",
    "count_scan_results_for_runs",
    "create_audit_log_entry",
    "create_scan_run",
    "engine",
    "ensure_database_schema",
    "finish_scan_run",
    "get_ai_evaluations",
    "get_config_overrides",
    "get_database_url",
    "get_forward_return_metric_records",
    "get_latest_scan_runs",
    "get_recent_audit_logs",
    "get_scan_results",
    "get_signals_needing_forward_returns",
    "init_db",
    "list_distinct_audit_events",
    "list_distinct_screener_keys",
    "list_distinct_triggered_by_values",
    "list_distinct_universe_keys",
    "save_ai_evaluations",
    "save_scan_results",
    "session_scope",
    "set_config_override",
    "upsert_forward_return",
]
