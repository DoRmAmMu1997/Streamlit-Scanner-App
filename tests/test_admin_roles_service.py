"""AUTH-003 — tests for the admin role assignment/revocation service."""

from __future__ import annotations

import pytest

from backend.admin.roles_service import (
    RoleAssignmentError,
    assign_role,
    list_role_assignments,
    revoke_role,
)
from backend.storage import get_recent_audit_logs, get_user_role


def _audit_events(file_session_factory) -> list[str]:
    with file_session_factory() as session:
        return [row.event for row in get_recent_audit_logs(session)]


def _role_changed_rows(file_session_factory) -> list[dict]:
    with file_session_factory() as session:
        return [
            row.metadata_json
            for row in get_recent_audit_logs(session)
            if row.event == "role_changed"
        ]


def test_assign_creates_row_and_audits(file_session_factory, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    result = assign_role(
        email="Viewer@Example.com",
        role="viewer",
        assigned_by="boss@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is True
    assert result.old_role is None
    assert result.new_role == "viewer"

    with file_session_factory() as session:
        assert get_user_role(session, "viewer@example.com") == "viewer"

    changed = _role_changed_rows(file_session_factory)
    assert len(changed) == 1
    assert changed[0]["target_email"] == "viewer@example.com"
    assert changed[0]["new_role"] == "viewer"


def test_assign_same_role_is_noop_without_audit(file_session_factory, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    assign_role(
        email="x@example.com", role="analyst", assigned_by="boss@example.com",
        session_factory=file_session_factory,
    )
    result = assign_role(
        email="x@example.com", role="analyst", assigned_by="boss@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is False
    # Only the first assignment recorded a role_changed row.
    assert _audit_events(file_session_factory).count("role_changed") == 1


@pytest.mark.parametrize("bad_email", ["", "   ", "not-an-email"])
def test_assign_rejects_invalid_email(file_session_factory, bad_email):
    with pytest.raises(RoleAssignmentError):
        assign_role(
            email=bad_email, role="viewer", assigned_by="boss@example.com",
            session_factory=file_session_factory,
        )


def test_assign_rejects_unknown_role(file_session_factory):
    with pytest.raises(RoleAssignmentError):
        assign_role(
            email="x@example.com", role="superuser", assigned_by="boss@example.com",
            session_factory=file_session_factory,
        )


def test_revoke_removes_row_and_audits(file_session_factory, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    assign_role(
        email="x@example.com", role="analyst", assigned_by="boss@example.com",
        session_factory=file_session_factory,
    )
    result = revoke_role(
        email="x@example.com", revoked_by="boss@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is True
    assert result.old_role == "analyst"
    assert result.new_role is None
    with file_session_factory() as session:
        assert get_user_role(session, "x@example.com") is None
    # An assign + a revoke both record role_changed.
    assert _audit_events(file_session_factory).count("role_changed") == 2


def test_revoke_absent_assignment_is_noop(file_session_factory, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    result = revoke_role(
        email="nobody@example.com", revoked_by="boss@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is False
    assert "role_changed" not in _audit_events(file_session_factory)


def test_last_admin_guard_blocks_demotion_when_no_env_admin(
    file_session_factory, monkeypatch
):
    # No env admin floor: the table holds the only admin.
    monkeypatch.setenv("ADMIN_EMAILS", "")
    assign_role(
        email="solo@example.com", role="admin", assigned_by=None,
        session_factory=file_session_factory,
    )
    with pytest.raises(RoleAssignmentError):
        assign_role(
            email="solo@example.com", role="viewer", assigned_by="solo@example.com",
            session_factory=file_session_factory,
        )
    with pytest.raises(RoleAssignmentError):
        revoke_role(
            email="solo@example.com", revoked_by="solo@example.com",
            session_factory=file_session_factory,
        )
    # The admin survived both blocked attempts.
    with file_session_factory() as session:
        assert get_user_role(session, "solo@example.com") == "admin"


def test_last_admin_guard_allows_demotion_with_another_admin(
    file_session_factory, monkeypatch
):
    monkeypatch.setenv("ADMIN_EMAILS", "")
    assign_role(email="a@example.com", role="admin", assigned_by=None,
                session_factory=file_session_factory)
    assign_role(email="b@example.com", role="admin", assigned_by=None,
                session_factory=file_session_factory)
    # Two table admins: demoting one is safe.
    result = assign_role(
        email="a@example.com", role="viewer", assigned_by="b@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is True


def test_last_admin_guard_noop_when_env_admin_configured(
    file_session_factory, monkeypatch
):
    # An env admin floor means a table admin can always be removed safely.
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    assign_role(email="solo@example.com", role="admin", assigned_by=None,
                session_factory=file_session_factory)
    result = revoke_role(
        email="solo@example.com", revoked_by="boss@example.com",
        session_factory=file_session_factory,
    )
    assert result.changed is True


def test_list_role_assignments_returns_sorted_dtos(file_session_factory, monkeypatch):
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    assign_role(email="bravo@example.com", role="viewer", assigned_by="boss@example.com",
                session_factory=file_session_factory)
    assign_role(email="alpha@example.com", role="admin", assigned_by="boss@example.com",
                session_factory=file_session_factory)
    rows = list_role_assignments(session_factory=file_session_factory)
    assert [(r.email, r.role) for r in rows] == [
        ("alpha@example.com", "admin"),
        ("bravo@example.com", "viewer"),
    ]
