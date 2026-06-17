"""Tests for the OBS-003 audit recorder (backend.audit).

The recorder has two sinks (the log stream and the audit_logs table) and two
hard rules: it is best-effort (a DB failure never breaks the caller) and
secret-safe (metadata is redacted before either sink). These tests use the
file-backed session factory so the recorder's own committed row is visible to a
separate read session, matching production.
"""

from __future__ import annotations

import logging

from backend.audit import record_audit_event, record_audit_event_once, should_record_once
from backend.security import MASK
from backend.storage.repository import get_recent_audit_logs


def test_record_audit_event_persists_row_and_emits_log(
    file_session_factory, caplog
):
    """A recorded event writes one durable row and emits a structured log event."""
    with caplog.at_level(logging.INFO):
        wrote = record_audit_event(
            event="login_success",
            user_email="person@example.com",
            metadata={"screener_key": "envelope"},
            session_factory=file_session_factory,
        )

    assert wrote is True

    # Read attributes while the session is open; commit expires ORM instances.
    with file_session_factory() as session:
        rows = get_recent_audit_logs(session)
        assert len(rows) == 1
        assert rows[0].event == "login_success"
        assert rows[0].user_email == "person@example.com"
        assert rows[0].metadata_json == {"screener_key": "envelope"}

    logged = [r for r in caplog.records if getattr(r, "event", None) == "login_success"]
    assert len(logged) == 1
    assert logged[0].structured_fields["user_email"] == "person@example.com"


def test_record_audit_event_redacts_metadata_before_storage(file_session_factory):
    """Sensitive metadata is masked in the durable row."""
    record_audit_event(
        event="config_changed",
        user_email="admin@example.com",
        metadata={"access_token": "live-abcdef123456", "setting": "LOG_LEVEL"},
        session_factory=file_session_factory,
    )

    with file_session_factory() as session:
        rows = get_recent_audit_logs(session)
        assert rows[0].metadata_json["access_token"] == MASK
        assert rows[0].metadata_json["setting"] == "LOG_LEVEL"


def test_record_audit_event_records_system_event_without_user(file_session_factory):
    """A system action records a NULL user_email."""
    record_audit_event(
        event="data_refresh_started",
        user_email=None,
        session_factory=file_session_factory,
    )

    with file_session_factory() as session:
        rows = get_recent_audit_logs(session)
        assert rows[0].user_email is None


def test_record_audit_event_is_best_effort_on_db_failure(caplog):
    """A database failure is swallowed: the log still fires and we return False."""

    def boom_factory():
        raise RuntimeError("database is down")

    with caplog.at_level(logging.INFO):
        wrote = record_audit_event(
            event="login_success",
            user_email="person@example.com",
            session_factory=boom_factory,
        )

    assert wrote is False
    # The action's log event still fired even though persistence failed.
    assert any(getattr(r, "event", None) == "login_success" for r in caplog.records)
    # And a warning explains the dropped audit row.
    assert any(
        "Failed to persist audit event" in r.getMessage()
        for r in caplog.records
        if r.levelno == logging.WARNING
    )


def test_record_audit_event_once_does_not_mark_failed_write(caplog):
    """A transient audit DB failure must not permanently suppress a retry."""

    def boom_factory():
        raise RuntimeError("database is down")

    state: dict[str, object] = {}
    with caplog.at_level(logging.INFO):
        wrote = record_audit_event_once(
            session_state=state,
            dedup_key="login:a@example.com",
            event="login_success",
            user_email="a@example.com",
            session_factory=boom_factory,
        )

    assert wrote is False
    assert "login:a@example.com" not in state
    assert any(getattr(r, "event", None) == "login_success" for r in caplog.records)


def test_record_audit_event_once_marks_after_success_and_suppresses_rerun(
    file_session_factory,
):
    """Once-per-session audit dedup should start only after the row is durable."""
    state: dict[str, object] = {}

    first_write = record_audit_event_once(
        session_state=state,
        dedup_key="login:a@example.com",
        event="login_success",
        user_email="a@example.com",
        session_factory=file_session_factory,
    )
    second_write = record_audit_event_once(
        session_state=state,
        dedup_key="login:a@example.com",
        event="login_success",
        user_email="a@example.com",
        session_factory=file_session_factory,
    )

    assert first_write is True
    assert second_write is False
    assert state == {"login:a@example.com": True}
    with file_session_factory() as session:
        rows = get_recent_audit_logs(session)
        assert len(rows) == 1
        assert rows[0].event == "login_success"


def test_should_record_once_dedups_per_key():
    """First sighting of a key returns True; later sightings return False."""
    state: dict[str, object] = {}

    assert should_record_once(state, "login:a@example.com") is True
    assert should_record_once(state, "login:a@example.com") is False
    # A different key is independent.
    assert should_record_once(state, "login:b@example.com") is True
