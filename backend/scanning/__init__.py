"""Scan orchestration service (SCAN-003).

Public surface:
- `run_scan` — run one screener and persist the run + results.
- `ScanRunResult` — the structured outcome returned to the caller.
"""

from backend.scanning.result_contract import (
    AIProvenance,
    JSONScalar,
    JSONValue,
    ResultContractError,
    RuleCheck,
    ScreenerResult,
    SignalProvenance,
    SignalSource,
    normalize_screener_row,
)
from backend.scanning.service import ScanRunResult, run_scan
from backend.storage.models import ScanStatus

__all__ = [
    "AIProvenance",
    "JSONScalar",
    "JSONValue",
    "ResultContractError",
    "RuleCheck",
    "ScanRunResult",
    "ScanStatus",
    "ScreenerResult",
    "SignalProvenance",
    "SignalSource",
    "normalize_screener_row",
    "run_scan",
]
