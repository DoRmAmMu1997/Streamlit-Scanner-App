"""Public API for scan orchestration and result normalization.

Public surface:
- `run_scan` — run one screener and persist the run + results.
- `ScanRunResult` — the structured outcome returned to the caller.
- `ScreenerResult` and `SignalProvenance` — typed descriptions of a result and
  the evidence explaining why it was produced.
- `normalize_screener_row` — the compatibility bridge that prepares flexible
  legacy dictionaries for JSON persistence.

Callers import these names from ``backend.scanning`` instead of depending on
the package's internal file layout. That keeps future refactors local to this
package rather than forcing every screener, test, and service to change imports.
"""

from backend.scanning.result_contract import (
    AIEvaluationOutcome,
    AIEvaluationRecord,
    AIProvenance,
    EvidenceReference,
    JSONScalar,
    JSONValue,
    ResultContractError,
    RuleCheck,
    ScreenerResult,
    SignalProvenance,
    SignalSource,
    normalize_screener_row,
    normalize_secret_safe_json,
)
from backend.scanning.service import ScanRunResult, run_scan
from backend.storage.models import ScanStatus

__all__ = [
    "AIEvaluationOutcome",
    "AIEvaluationRecord",
    "AIProvenance",
    "EvidenceReference",
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
    "normalize_secret_safe_json",
    "run_scan",
]
