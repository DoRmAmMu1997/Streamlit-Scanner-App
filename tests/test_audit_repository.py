"""Tests for the OBS-003 audit-log and app-config repository helpers.

These use the shared in-memory ``db_session`` fixture (tests/conftest.py) so they
exercise the same ORM mapping and redaction the app uses, without Streamlit or a
real database file.
"""

from __future__ import annotations

import datetime as dt

from backend.security import MASK
from backend.storage.models import AuditLog
from backend.storage.repository import (
    create_audit_log_entry,
    get_config_overrides,
    get_recent_audit_logs,
    list_distinct_audit_events,
    set_config_override,
)


def test_create_audit_log_entry_stores_event_user_and_metadata(db_session):
    """A basic audit row round-trips with its event, actor, and metadata."""
    entry = create_audit_log_entry(
        db_session,
        event="login_success",
        user_email="person@example.com",
        metadata={"screener_key": "envelope"},
    )

    assert entry.id is not None
    assert entry.event == "login_success"
    assert entry.user_email == "person@example.com"
    assert entry.metadata_json == {"screener_key": "envelope"}
    # Timestamp is populated by the ORM default in tz-aware UTC.
    assert entry.created_at.tzinfo is not None


def test_create_audit_log_entry_redacts_sensitive_metadata(db_session):
    """Credential-named keys and token-shaped strings must never persist raw."""
    entry = create_audit_log_entry(
        db_session,
        event="config_changed",
        user_email="admin@example.com",
        metadata={
            "access_token": "live-abcdef123456",
            "note": "authorization: Bearer SECRETVALUE123",
            "setting": "LOG_LEVEL",
        },
    )

    assert entry.metadata_json is not None
    # Masked because the KEY looks like a credential.
    assert entry.metadata_json["access_token"] == MASK
    # Masked because the VALUE matches a secret-bearing pattern.
    assert MASK in entry.metadata_json["note"]
    assert "SECRETVALUE123" not in entry.metadata_json["note"]
    # Ordinary fields survive untouched.
    assert entry.metadata_json["setting"] == "LOG_LEVEL"


def test_create_audit_log_entry_allows_system_event_without_user(db_session):
    """System actions (the startup data refresh) record a NULL user_email."""
    entry = create_audit_log_entry(
        db_session, event="data_refresh_started", user_email=None
    )

    assert entry.user_email is None
    assert entry.metadata_json is None


def test_get_recent_audit_logs_orders_newest_first_with_id_tiebreak(db_session):
    """Newest-first ordering, with the primary key breaking ms ties."""
    base = dt.datetime(2026, 6, 17, 12, 0, tzinfo=dt.UTC)
    # Two rows share a timestamp; a third is older. Insert directly so the shared
    # created_at is exact (the helper would otherwise stamp now()).
    older = AuditLog(event="login_success", user_email="a@example.com", created_at=base)
    tie_low = AuditLog(
        event="manual_scan_started",
        user_email="b@example.com",
        created_at=base + dt.timedelta(minutes=1),
    )
    tie_high = AuditLog(
        event="export_downloaded",
        user_email="c@example.com",
        created_at=base + dt.timedelta(minutes=1),
    )
    db_session.add_all([older, tie_low, tie_high])
    db_session.flush()

    rows = get_recent_audit_logs(db_session)

    # Same-timestamp rows order by descending id; the older row comes last.
    assert [row.id for row in rows] == [tie_high.id, tie_low.id, older.id]


def test_get_recent_audit_logs_filters_by_event_and_email(db_session):
    """Optional filters narrow by event name and (case-insensitive) email."""
    create_audit_log_entry(
        db_session, event="login_success", user_email="boss@example.com"
    )
    create_audit_log_entry(
        db_session, event="export_downloaded", user_email="boss@example.com"
    )
    create_audit_log_entry(
        db_session, event="login_success", user_email="other@example.com"
    )

    by_event = get_recent_audit_logs(db_session, event="login_success")
    assert {row.user_email for row in by_event} == {
        "boss@example.com",
        "other@example.com",
    }

    by_email = get_recent_audit_logs(db_session, user_email="BOSS@EXAMPLE.COM")
    assert {row.event for row in by_email} == {"login_success", "export_downloaded"}

    assert get_recent_audit_logs(db_session, limit=1) and len(
        get_recent_audit_logs(db_session, limit=1)
    ) == 1


def test_list_distinct_audit_events_sorted(db_session):
    """Distinct event names come back sorted for the viewer's filter dropdown."""
    for event in ("login_success", "login_success", "config_changed"):
        create_audit_log_entry(db_session, event=event, user_email="a@example.com")

    assert list_distinct_audit_events(db_session) == ["config_changed", "login_success"]


def test_config_override_upsert_returns_previous_value(db_session):
    """set_config_override upserts and returns the prior value for auditing."""
    first = set_config_override(
        db_session, key="LOG_LEVEL", value="DEBUG", updated_by="admin@example.com"
    )
    assert first is None
    assert get_config_overrides(db_session) == {"LOG_LEVEL": "DEBUG"}

    previous = set_config_override(
        db_session, key="LOG_LEVEL", value="INFO", updated_by="admin@example.com"
    )
    assert previous == "DEBUG"
    assert get_config_overrides(db_session) == {"LOG_LEVEL": "INFO"}


def test_get_config_overrides_skips_null_values(db_session):
    """A NULL override value is treated as 'use the environment default'."""
    set_config_override(
        db_session, key="LOG_FORMAT", value=None, updated_by="admin@example.com"
    )

    assert get_config_overrides(db_session) == {}
