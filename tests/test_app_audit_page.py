"""Tests for the OBS-003 admin audit log page (ui.audit_page).

Focus: the admin guard (security), the pure formatting helpers, and the read
path with the database fully faked so no real engine is touched.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from types import SimpleNamespace

from sqlalchemy.exc import OperationalError

from backend.auth.roles import Role
from backend.auth.session import AuthenticatedUser
from backend.security import MASK
from ui import audit_page


class _FakeCtx:
    """Context manager standing in for a Streamlit column / layout block."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeStreamlit:
    """Minimal Streamlit surface used by the audit page renderer tests."""

    def __init__(self):
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.dataframes: list[object] = []

    def subheader(self, *_args, **_kwargs):
        pass

    def caption(self, *_args, **_kwargs):
        pass

    def columns(self, count):
        return [_FakeCtx() for _ in range(count)]

    def selectbox(self, _label, options, **_kwargs):
        # Default selection (e.g. "All" event filter, smallest limit).
        return options[0]

    def text_input(self, *_args, **_kwargs):
        return ""

    def dataframe(self, data, *_args, **_kwargs):
        self.dataframes.append(data)

    def error(self, text, **_kwargs):
        self.errors.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))


def test_format_audit_time_handles_none_and_naive_and_aware():
    assert audit_page._format_audit_time(None) == "—"
    naive = dt.datetime(2026, 6, 17, 9, 30, 0)
    assert audit_page._format_audit_time(naive) == "2026-06-17 09:30:00 UTC"
    aware = dt.datetime(2026, 6, 17, 9, 30, 0, tzinfo=dt.UTC)
    assert audit_page._format_audit_time(aware) == "2026-06-17 09:30:00 UTC"


def test_format_metadata_redacts_and_handles_empty():
    assert audit_page._format_metadata(None) == ""
    rendered = audit_page._format_metadata({"access_token": "live-abcdef123456"})
    assert MASK in rendered
    assert "live-abcdef123456" not in rendered


def test_audit_row_renders_system_for_missing_user():
    entry = SimpleNamespace(
        created_at=dt.datetime(2026, 6, 17, 9, 0, tzinfo=dt.UTC),
        event="data_refresh_started",
        user_email=None,
        metadata_json=None,
    )
    row = audit_page._audit_row(entry)
    assert row["Event"] == "data_refresh_started"
    assert row["User"] == "system"
    assert row["Details"] == ""


def test_audit_page_rejects_non_admin(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(audit_page, "st", fake_st)

    audit_page._render_audit_log_page(
        AuthenticatedUser("person@example.com", "Person", role=Role.ANALYST)
    )

    assert fake_st.errors == ["Admin access is required to view the audit log."]


def test_audit_page_rejects_auth_disabled_session(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(audit_page, "st", fake_st)

    audit_page._render_audit_log_page(None)

    assert fake_st.errors == ["Admin access is required to view the audit log."]


def test_audit_page_renders_table_for_admin(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(audit_page, "st", fake_st)

    @contextmanager
    def fake_scope():
        yield None

    monkeypatch.setattr(audit_page, "session_scope", fake_scope)
    monkeypatch.setattr(
        audit_page, "list_distinct_audit_events", lambda _session: ["login_success"]
    )
    entries = [
        SimpleNamespace(
            created_at=dt.datetime(2026, 6, 17, 9, 0, tzinfo=dt.UTC),
            event="login_success",
            user_email="admin@example.com",
            metadata_json={"screener_key": "envelope"},
        )
    ]
    monkeypatch.setattr(
        audit_page,
        "get_recent_audit_logs",
        lambda _session, limit, event, user_email: entries,
    )

    audit_page._render_audit_log_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert len(fake_st.dataframes) == 1
    assert fake_st.errors == []


def test_audit_page_handles_missing_table(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(audit_page, "st", fake_st)

    @contextmanager
    def boom_scope():
        raise OperationalError("no such table: audit_logs", None, Exception())
        yield  # pragma: no cover - unreachable, keeps this a generator

    monkeypatch.setattr(audit_page, "session_scope", boom_scope)

    audit_page._render_audit_log_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert any("alembic upgrade head" in message for message in fake_st.errors)
