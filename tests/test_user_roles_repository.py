"""AUTH-003 — tests for the user_roles repository helpers."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage import (
    UserRole,
    count_user_role_admins,
    delete_user_role,
    get_user_role,
    list_user_roles,
    repository,
    set_user_role,
)


def test_set_then_get_round_trip(db_session: Session):
    previous = set_user_role(
        db_session, email="viewer@example.com", role="viewer", assigned_by="boss@example.com"
    )
    assert previous is None
    assert get_user_role(db_session, "viewer@example.com") == "viewer"


def test_get_returns_none_when_unassigned(db_session: Session):
    assert get_user_role(db_session, "nobody@example.com") is None


def test_email_is_normalized_on_write_and_read(db_session: Session):
    set_user_role(
        db_session, email="  Mixed@Example.COM ", role="analyst", assigned_by=None
    )
    # Stored lower-cased and trimmed; a lookup in any casing finds the one row.
    assert get_user_role(db_session, "mixed@example.com") == "analyst"
    assert get_user_role(db_session, "MIXED@EXAMPLE.com") == "analyst"
    assert len(list_user_roles(db_session)) == 1


def test_reassignment_updates_in_place_and_returns_previous(db_session: Session):
    set_user_role(db_session, email="x@example.com", role="viewer", assigned_by="a@example.com")

    previous = set_user_role(
        db_session, email="x@example.com", role="admin", assigned_by="b@example.com"
    )
    assert previous == "viewer"
    # Upsert, not insert: one row, updated in place with the new role + new author.
    rows = list_user_roles(db_session)
    assert len(rows) == 1
    assert rows[0].role == "admin"
    assert rows[0].assigned_by == "b@example.com"
    assert rows[0].updated_at is not None


def test_delete_returns_previous_then_absent(db_session: Session):
    set_user_role(db_session, email="gone@example.com", role="analyst", assigned_by=None)
    assert delete_user_role(db_session, "gone@example.com") == "analyst"
    assert get_user_role(db_session, "gone@example.com") is None
    # Deleting an absent row is a no-op that returns None (idempotent revoke).
    assert delete_user_role(db_session, "gone@example.com") is None


def test_list_user_roles_sorted_by_email(db_session: Session):
    set_user_role(db_session, email="charlie@example.com", role="viewer", assigned_by=None)
    set_user_role(db_session, email="alice@example.com", role="admin", assigned_by=None)
    set_user_role(db_session, email="bob@example.com", role="analyst", assigned_by=None)
    emails = [row.email for row in list_user_roles(db_session)]
    assert emails == ["alice@example.com", "bob@example.com", "charlie@example.com"]


def test_count_user_role_admins(db_session: Session):
    assert count_user_role_admins(db_session) == 0
    set_user_role(db_session, email="a@example.com", role="admin", assigned_by=None)
    set_user_role(db_session, email="b@example.com", role="admin", assigned_by=None)
    set_user_role(db_session, email="c@example.com", role="viewer", assigned_by=None)
    assert count_user_role_admins(db_session) == 2


def test_admin_lock_query_uses_for_update():
    """Postgres role mutations must serialize against the same admin rows."""
    # Any (not object) so the captured SQLAlchemy statement's .compile() stays
    # callable in the assertion below (QUAL-007).
    statements: list[Any] = []

    class RecordingSession:
        def scalars(self, statement):
            statements.append(statement)
            return []

    helper = getattr(repository, "list_user_role_admins_for_update", None)
    assert callable(helper)

    assert helper(RecordingSession()) == []
    sql = str(statements[0].compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE" in sql.upper()


def test_check_constraint_rejects_unknown_role(db_session: Session):
    # The admin service validates roles, but the database CHECK is the last line of
    # defense: a row with an unknown role must never be storable.
    db_session.add(UserRole(email="bad@example.com", role="superuser"))
    with pytest.raises(IntegrityError):
        db_session.flush()
