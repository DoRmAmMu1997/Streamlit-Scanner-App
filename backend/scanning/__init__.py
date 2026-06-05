"""Scan orchestration service (SCAN-003).

Public surface:
- `run_scan` — run one screener and persist the run + results.
- `ScanRunResult` — the structured outcome returned to the caller.
"""

from backend.scanning.service import ScanRunResult, run_scan
from backend.storage.models import ScanStatus

__all__ = ["ScanRunResult", "ScanStatus", "run_scan"]
