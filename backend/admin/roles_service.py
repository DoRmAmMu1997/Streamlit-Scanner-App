"""AUTH-003 — admin service for assigning and revoking user roles.

Beginner note:
The role *policy* (who can do what) lives in ``backend.auth.roles``; the durable
*assignments* live in the ``user_roles`` table. This module is the thin, testable
layer the admin Roles page calls to change those assignments safely:

- ``assign_role`` validates the role against the ``Role`` enum, normalizes the
  email, refuses self-demotion or a change that would leave the system with no
  admin, upserts the row, and records a ``role_changed`` audit event (old -> new).
- ``revoke_role`` removes an assignment (which also revokes table-granted sign-in,
  per the AUTH-003 entry widening), with the same last-admin guard and audit.
- ``list_role_assignments`` returns plain DTOs for the page table.

Design note: ``backend`` never imports Streamlit. This module exposes plain
functions and data; the page in ``ui/`` renders them. The last-admin guard treats
``ADMIN_EMAILS`` (env) as an always-present admin floor, so it only ever bites when
no env admin is configured and the table holds the only admin. Admin rows are
locked in the mutation transaction so concurrent demotions cannot race the guard.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.audit import record_audit_event
from backend.auth.roles import Role
from backend.config import get_settings
from backend.observability import EVENT_ROLE_CHANGED
from backend.storage import (
    delete_user_role,
    get_user_role,
    list_user_role_admins_for_update,
    list_user_roles,
    session_scope,
    set_user_role,
)

SessionFactory = Any


class RoleAssignmentError(RuntimeError):
    """Raised when a role assignment/revocation is invalid or unsafe.

    A dedicated error lets the admin page show a clear message (invalid email,
    unknown role, or "this would remove the last admin") instead of leaking a
    generic stack trace.
    """


@dataclass(frozen=True)
class RoleAssignment:
    """One row of the admin Roles table (a plain, session-detached DTO)."""

    email: str
    role: str
    assigned_by: str | None


@dataclass(frozen=True)
class RoleChangeResult:
    """Outcome of an attempted assign/revoke (used for UI feedback)."""

    email: str
    old_role: str | None
    new_role: str | None
    changed: bool


def _normalize_email(email: str) -> str:
    """Collapse an email to the canonical lowercase key the gate/table use."""
    return str(email or "").strip().lower()


def _guard_last_admin(locked_admins: list[Any]) -> None:
    """Refuse a change that would leave zero effective admins.

    Effective admins are the env ``ADMIN_EMAILS`` floor plus the ``user_roles``
    admins. When any env admin is configured, the system can never be locked out,
    so the guard is a no-op. Otherwise the table holds the only admins, and the
    caller has already established the target is currently a table admin — so if
    it is the last one, removing/demoting it would strand the app with no admin.
    """
    if get_settings().admin_emails:
        return
    if len(locked_admins) <= 1:
        raise RoleAssignmentError(
            "Refusing to remove the last remaining admin. Promote another user to "
            "admin first, or configure ADMIN_EMAILS."
        )


def assign_role(
    *,
    email: str,
    role: str,
    assigned_by: str | None,
    session_factory: SessionFactory = session_scope,
) -> RoleChangeResult:
    """Validate, persist, and audit one role assignment.

    Raises ``RoleAssignmentError`` for a blank/invalid email, an unknown role, or
    self-demotion, or a demotion that would remove the last admin. An unchanged
    role is a no-op that records nothing.
    """
    normalized = _normalize_email(email)
    if not normalized or "@" not in normalized:
        raise RoleAssignmentError("A valid email address is required.")
    parsed = Role.parse(role)
    if parsed is None:
        raise RoleAssignmentError(f"{role!r} is not a valid role.")
    new_role = parsed.name.lower()
    actor_email = _normalize_email(assigned_by or "")

    with session_factory() as session:
        old_role = get_user_role(session, normalized)
        if old_role == new_role:
            return RoleChangeResult(
                email=normalized, old_role=old_role, new_role=new_role, changed=False
            )
        # Demoting the last admin would strand the app with no admin.
        if old_role == "admin" and parsed is not Role.ADMIN:
            if actor_email and actor_email == normalized:
                raise RoleAssignmentError(
                    "Administrators cannot change their own admin role. "
                    "Ask another administrator to make this change."
                )
            locked_admins = list_user_role_admins_for_update(session)
            _guard_last_admin(locked_admins)
        set_user_role(
            session, email=normalized, role=new_role, assigned_by=assigned_by
        )

    record_audit_event(
        event=EVENT_ROLE_CHANGED,
        user_email=assigned_by,
        metadata={
            "target_email": normalized,
            "old_role": old_role,
            "new_role": new_role,
        },
        session_factory=session_factory,
    )
    return RoleChangeResult(
        email=normalized, old_role=old_role, new_role=new_role, changed=True
    )


def revoke_role(
    *,
    email: str,
    revoked_by: str | None,
    session_factory: SessionFactory = session_scope,
) -> RoleChangeResult:
    """Remove a role assignment (and its table-granted access), with audit.

    Revoking an absent assignment is a no-op. Revoking the last admin is refused
    by the same guard as ``assign_role``.
    """
    normalized = _normalize_email(email)
    if not normalized:
        raise RoleAssignmentError("A valid email address is required.")

    actor_email = _normalize_email(revoked_by or "")

    with session_factory() as session:
        old_role = get_user_role(session, normalized)
        if old_role is None:
            return RoleChangeResult(
                email=normalized, old_role=None, new_role=None, changed=False
            )
        if actor_email and actor_email == normalized:
            raise RoleAssignmentError(
                "Administrators cannot remove their own role assignment. "
                "Ask another administrator to make this change."
            )
        if old_role == "admin":
            locked_admins = list_user_role_admins_for_update(session)
            _guard_last_admin(locked_admins)
        delete_user_role(session, normalized)

    record_audit_event(
        event=EVENT_ROLE_CHANGED,
        user_email=revoked_by,
        metadata={
            "target_email": normalized,
            "old_role": old_role,
            "new_role": None,
        },
        session_factory=session_factory,
    )
    return RoleChangeResult(
        email=normalized, old_role=old_role, new_role=None, changed=True
    )


def list_role_assignments(
    *, session_factory: SessionFactory = session_scope
) -> list[RoleAssignment]:
    """Return all current role assignments as detached DTOs, sorted by email."""
    with session_factory() as session:
        return [
            RoleAssignment(email=row.email, role=row.role, assigned_by=row.assigned_by)
            for row in list_user_roles(session)
        ]
