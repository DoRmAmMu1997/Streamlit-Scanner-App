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
    IpoDocument,
    IpoFinancial,
    IpoIssue,
    IpoManualExtraction,
    IpoManualFinancialPeriod,
    IpoManualPeerValuation,
    IpoRecommendation,
    IpoScore,
    IpoSubscription,
    ScanResult,
    ScanRun,
    ScanStatus,
    SignalForwardReturn,
    UserRole,
)
from backend.storage.repository import (
    ForwardReturnMetricRecord,
    count_scan_results_for_runs,
    count_user_role_admins,
    create_audit_log_entry,
    create_scan_run,
    delete_user_role,
    finish_scan_run,
    get_ai_evaluations,
    get_config_overrides,
    get_forward_return_metric_records,
    get_latest_finalized_scan_runs,
    get_latest_scan_runs,
    get_recent_audit_logs,
    get_scan_results,
    get_scan_run,
    get_scan_runs,
    get_signals_needing_forward_returns,
    get_top_ranked_results,
    get_user_role,
    list_distinct_audit_events,
    list_distinct_screener_keys,
    list_distinct_triggered_by_values,
    list_distinct_universe_keys,
    list_finalized_scan_groups,
    list_user_role_admins_for_update,
    list_user_roles,
    save_ai_evaluations,
    save_scan_results,
    set_config_override,
    set_user_role,
    upsert_forward_return,
)

__all__ = [
    "AIEvaluation",
    "AppConfig",
    "AuditLog",
    "Base",
    "ForwardReturnMetricRecord",
    "ForwardReturnStatus",
    "IpoDocument",
    "IpoFinancial",
    "IpoIssue",
    "IpoManualExtraction",
    "IpoManualFinancialPeriod",
    "IpoManualPeerValuation",
    "IpoRecommendation",
    "IpoScore",
    "IpoSubscription",
    "ScanResult",
    "ScanRun",
    "ScanStatus",
    "SessionLocal",
    "SignalForwardReturn",
    "UserRole",
    "count_scan_results_for_runs",
    "count_user_role_admins",
    "create_audit_log_entry",
    "create_scan_run",
    "delete_user_role",
    "engine",
    "ensure_database_schema",
    "finish_scan_run",
    "get_ai_evaluations",
    "get_config_overrides",
    "get_database_url",
    "get_forward_return_metric_records",
    "get_latest_finalized_scan_runs",
    "get_latest_scan_runs",
    "get_recent_audit_logs",
    "get_scan_results",
    "get_scan_run",
    "get_scan_runs",
    "get_signals_needing_forward_returns",
    "get_top_ranked_results",
    "get_user_role",
    "init_db",
    "list_distinct_audit_events",
    "list_distinct_screener_keys",
    "list_distinct_triggered_by_values",
    "list_distinct_universe_keys",
    "list_finalized_scan_groups",
    "list_user_role_admins_for_update",
    "list_user_roles",
    "save_ai_evaluations",
    "save_scan_results",
    "session_scope",
    "set_config_override",
    "set_user_role",
    "upsert_forward_return",
]
