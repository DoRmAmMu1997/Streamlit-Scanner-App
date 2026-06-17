"""Admin audit log page (OBS-003).

Browses the durable ``audit_logs`` trail written by ``backend.audit``: logins,
manual scans, config changes, CSV exports, and admin-page access. Read-only, and
admin-gated like the health page — the main view selector hides it from
non-admins, and this renderer repeats the check so a future direct caller or an
auth-disabled development session cannot bypass it.

Metadata stored in the table is already secret-redacted at write time; the
rendered text is passed through the shared redactor again as defense in depth.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.exc import OperationalError

from backend.auth.session import AuthenticatedUser
from backend.storage import (
    AuditLog,
    get_recent_audit_logs,
    list_distinct_audit_events,
    session_scope,
)
from ui.common import _redact_secrets

# Keep the page bounded as the trail grows; an admin can widen it if needed.
_AUDIT_PAGE_LIMITS = (100, 250, 500)


def _format_audit_time(value: datetime | None) -> str:
    """Render a stored UTC timestamp consistently (SQLite returns it naive)."""
    if value is None:
        return "—"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_metadata(metadata: dict[str, Any] | None) -> str:
    """Render the metadata blob as compact, secret-safe text for the table."""
    if not metadata:
        return ""
    rendered = json.dumps(metadata, sort_keys=True, default=str)
    return _redact_secrets(rendered)


def _audit_row(entry: AuditLog) -> dict[str, Any]:
    """Convert one ORM row into a plain display dict while the session is open."""
    return {
        "Time (UTC)": _format_audit_time(entry.created_at),
        "Event": entry.event,
        "User": entry.user_email or "system",
        "Details": _format_metadata(entry.metadata_json),
    }


def _render_audit_log_page(authenticated_user: AuthenticatedUser | None) -> None:
    """Render the admin-only audit log viewer (filters + table)."""
    if authenticated_user is None or not authenticated_user.is_admin:
        st.error("Admin access is required to view the audit log.")
        return

    st.subheader("Audit log")
    st.caption(
        "Important user actions — logins, manual scans, config changes, exports, "
        "and admin-page access. Sensitive values are redacted before storage."
    )

    # Populate the event filter from what actually appears in the trail, so the
    # dropdown never offers an event that has not happened yet.
    try:
        with session_scope() as session:
            event_names = list_distinct_audit_events(session)
    except OperationalError:
        st.error(
            "Audit log table is missing. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        event_choice = st.selectbox(
            "Event",
            ["All", *event_names],
            key="audit_event_filter",
        )
    with filter_col2:
        email_text = st.text_input(
            "User email",
            key="audit_email_filter",
            help="Exact match, case-insensitive. Leave empty for everyone.",
        )
    with filter_col3:
        limit_choice = st.selectbox(
            "Show",
            _AUDIT_PAGE_LIMITS,
            format_func=lambda value: f"Latest {value}",
            key="audit_limit_filter",
        )

    event_filter = None if event_choice == "All" else event_choice
    email_filter = email_text.strip() or None
    with session_scope() as session:
        entries = get_recent_audit_logs(
            session,
            limit=int(limit_choice),
            event=event_filter,
            user_email=email_filter,
        )
        rows = [_audit_row(entry) for entry in entries]

    if not rows:
        st.info("No audit entries match the current filters.")
        return

    st.dataframe(
        pd.DataFrame(rows),
        width="stretch",
        hide_index=True,
        key="audit_log_table",
    )
