"""OBS-003 — record important user actions to a durable audit trail.

Beginner note:
An *audit log* answers "who did what, and when?" Unlike the OBS-001 structured
logs (which stream to the console / a log aggregator and roll away), audit rows
are persisted in the ``audit_logs`` database table so they can be browsed and
filtered long after the fact.

This module is a thin *recorder* with two jobs:

1. emit the action as a normal OBS-001 ``log_event`` (so it also shows up in the
   live log stream), and
2. write one durable ``audit_logs`` row.

Two design rules:

- **Best-effort.** A failure to persist an audit row must never break the user's
  action (a login, a scan, a download). The database write is wrapped so any
  error is logged and swallowed — mirroring how scan persistence is best-effort.
- **Secret-safe.** Metadata is passed through the app's shared
  ``normalize_secret_safe_json`` redactor before it touches either sink, so a
  token accidentally placed in a field never becomes durable audit evidence.

Design note (why this file imports no Streamlit):
``backend`` never imports Streamlit. The recorder takes plain values
(``event``, ``user_email``, ``metadata``); the once-per-session de-duplication
that the UI needs accepts a plain mapping (``st.session_state``), so this module
stays framework-free and unit-testable while still letting audit-critical callers
mark dedup keys only after persistence succeeds.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from backend.observability import log_event
from backend.storage import create_audit_log_entry, session_scope

logger = logging.getLogger(__name__)

# Sessions/transactions are short-lived; the default factory is the app's real
# one, but tests inject a factory bound to a throwaway SQLite database.
SessionFactory = Any


def record_audit_event(
    *,
    event: str,
    user_email: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
    session_factory: SessionFactory = session_scope,
) -> bool:
    """Record one audit event to the log stream and the ``audit_logs`` table.

    Args:
        event: A stable event name from ``backend.observability`` (for example
            ``EVENT_LOGIN_SUCCESS``).
        user_email: The actor's email, or ``None`` for system actions that run
            before anyone signs in (the startup data refresh).
        metadata: Optional action context (screener key, file name, ...). It is
            redacted and made JSON-safe before storage.
        level: Log level for the OBS-001 event (denials use ``WARNING``).
        session_factory: Context-manager factory yielding a SQLAlchemy session;
            defaults to the app's ``session_scope``. Tests pass a temp-DB factory.

    Returns:
        ``True`` when the durable row was written, ``False`` when persistence
        failed (the action still proceeds; the log event is always emitted).
    """
    safe_metadata = _safe_metadata(metadata)

    # 1. Always emit the structured log event. The formatters redact the rendered
    #    line, so this is safe even though the action also lives in the DB.
    fields: dict[str, Any] = {"user_email": user_email}
    fields.update(safe_metadata)
    log_event(logger, event, level=level, **fields)

    # 2. Best-effort durable audit row. A DB hiccup must not break the action.
    try:
        with session_factory() as session:
            create_audit_log_entry(
                session,
                event=event,
                user_email=user_email,
                metadata=safe_metadata or None,
            )
    except Exception:  # noqa: BLE001 - audit persistence is best-effort.
        logger.warning(
            "Failed to persist audit event %s; continuing without it.",
            event,
            exc_info=True,
        )
        return False
    return True


def record_audit_event_once(
    *,
    session_state: Any,
    dedup_key: str,
    event: str,
    user_email: str | None = None,
    metadata: Mapping[str, Any] | None = None,
    level: int = logging.INFO,
    session_factory: SessionFactory = session_scope,
) -> bool:
    """Record a level-triggered event once after the durable write succeeds.

    ``should_record_once`` marks the key before the caller records anything,
    which is fine for low-risk UI noise but unsafe for audit-critical events:
    a transient DB failure would permanently suppress the retry on the next
    Streamlit rerun. This helper flips the order. It checks the key first, tries
    the normal best-effort recorder, and only then marks the session as recorded.
    """
    if session_state.get(dedup_key):
        return False

    wrote = record_audit_event(
        event=event,
        user_email=user_email,
        metadata=metadata,
        level=level,
        session_factory=session_factory,
    )
    if wrote:
        session_state[dedup_key] = True
    return wrote


def should_record_once(session_state: Any, key: str) -> bool:
    """Return ``True`` the first time ``key`` is seen this session, else ``False``.

    Streamlit re-runs the whole script on every interaction, so level-triggered
    events (a successful login, opening an admin page) would otherwise be recorded
    on every rerun. The UI calls this with ``st.session_state`` and a stable key
    to record such events exactly once per browser session.

    ``session_state`` is typed ``Any`` because it is a dynamic mapping-like object
    (Streamlit's ``SessionStateProxy`` or, in tests, a plain ``dict``); it only
    needs ``.get`` and item assignment. Keeping it framework-free keeps Streamlit
    out of ``backend`` and lets unit tests pass an ordinary ``dict``.
    """
    if session_state.get(key):
        return False
    session_state[key] = True
    return True


def _safe_metadata(metadata: Mapping[str, Any] | None) -> dict[str, Any]:
    """Return a redacted, JSON-safe copy of ``metadata`` (empty dict if absent).

    Reuses the same persistence redactor as scan parameters so credential-named
    keys are masked, strings are redacted, and the result is strict JSON. Imported
    lazily to avoid an import cycle (``storage`` imports ``scanning`` which imports
    ``storage``) and to keep this recorder a lean leaf module.
    """
    if not metadata:
        return {}
    from backend.scanning.result_contract import normalize_secret_safe_json

    normalized = normalize_secret_safe_json(dict(metadata))
    return normalized if isinstance(normalized, dict) else {}
