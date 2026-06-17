"""OBS-003 audit subsystem — record important user actions durably.

Friendly public path: ``from backend.audit import record_audit_event``. The
implementation lives in ``recorder.py``; this package surface keeps callers from
needing to know that. See ``recorder`` for the best-effort, secret-safe design.
"""

from backend.audit.recorder import record_audit_event, should_record_once

__all__ = [
    "record_audit_event",
    "should_record_once",
]
