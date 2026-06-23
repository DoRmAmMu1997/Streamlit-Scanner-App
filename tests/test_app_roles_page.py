"""Tests for the AUTH-003 admin Roles page (ui.roles_page).

Focus: the admin guard (security) and the assign/revoke feedback flow with the
roles service fully faked so no real database write happens here.
"""

from __future__ import annotations

from backend.admin.roles_service import (
    RoleAssignment,
    RoleAssignmentError,
    RoleChangeResult,
)
from backend.auth.roles import Role
from backend.auth.session import AuthenticatedUser
from ui import roles_page

ADMIN = AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)


class _FakeForm:
    """Context manager standing in for ``st.form``."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeStreamlit:
    """Minimal Streamlit surface used by the roles page renderer tests."""

    def __init__(self, *, submit_label: str | None = None):
        self._submit_label = submit_label
        self.successes: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.dataframes: list[object] = []

    def subheader(self, *_args, **_kwargs):
        pass

    def caption(self, *_args, **_kwargs):
        pass

    def markdown(self, *_args, **_kwargs):
        pass

    def dataframe(self, data, *_args, **_kwargs):
        self.dataframes.append(data)

    def form(self, *_args, **_kwargs):
        return _FakeForm()

    def text_input(self, _label, **_kwargs):
        return "target@example.com"

    def selectbox(self, _label, choices, **_kwargs):
        return choices[0]

    def form_submit_button(self, label, **_kwargs):
        return label == self._submit_label

    def success(self, text, **_kwargs):
        self.successes.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))

    def error(self, text, **_kwargs):
        self.errors.append(str(text))


def test_roles_page_rejects_non_admin(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(roles_page, "st", fake_st)

    roles_page._render_roles_page(
        AuthenticatedUser("person@example.com", "Person", role=Role.ANALYST)
    )

    assert fake_st.errors == ["Admin access is required to manage roles."]


def test_roles_page_rejects_auth_disabled_session(monkeypatch):
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(roles_page, "st", fake_st)

    roles_page._render_roles_page(None)

    assert fake_st.errors == ["Admin access is required to manage roles."]


def test_roles_page_lists_assignments_without_submit(monkeypatch):
    fake_st = _FakeStreamlit(submit_label=None)
    monkeypatch.setattr(roles_page, "st", fake_st)
    monkeypatch.setattr(
        roles_page,
        "list_role_assignments",
        lambda: [RoleAssignment(email="a@example.com", role="viewer", assigned_by="b@x.com")],
    )
    monkeypatch.setattr(
        roles_page,
        "assign_role",
        lambda **_k: (_ for _ in ()).throw(AssertionError("must not assign before submit")),
    )

    roles_page._render_roles_page(ADMIN)

    assert len(fake_st.dataframes) == 1
    assert fake_st.successes == []
    assert fake_st.errors == []


def test_roles_page_reports_assignment_on_submit(monkeypatch):
    fake_st = _FakeStreamlit(submit_label="Save role")
    monkeypatch.setattr(roles_page, "st", fake_st)
    monkeypatch.setattr(roles_page, "list_role_assignments", list)
    monkeypatch.setattr(
        roles_page,
        "assign_role",
        lambda **_k: RoleChangeResult(
            email="target@example.com", old_role=None, new_role="viewer", changed=True
        ),
    )

    roles_page._render_roles_page(ADMIN)

    assert fake_st.successes
    assert fake_st.errors == []


def test_roles_page_reports_no_change(monkeypatch):
    fake_st = _FakeStreamlit(submit_label="Save role")
    monkeypatch.setattr(roles_page, "st", fake_st)
    monkeypatch.setattr(roles_page, "list_role_assignments", list)
    monkeypatch.setattr(
        roles_page,
        "assign_role",
        lambda **_k: RoleChangeResult(
            email="target@example.com", old_role="viewer", new_role="viewer", changed=False
        ),
    )

    roles_page._render_roles_page(ADMIN)

    assert any("No change" in message for message in fake_st.infos)


def test_roles_page_surfaces_assignment_errors(monkeypatch):
    fake_st = _FakeStreamlit(submit_label="Save role")
    monkeypatch.setattr(roles_page, "st", fake_st)
    monkeypatch.setattr(roles_page, "list_role_assignments", list)

    def boom(**_kwargs):
        raise RoleAssignmentError("Refusing to remove the last remaining admin.")

    monkeypatch.setattr(roles_page, "assign_role", boom)

    roles_page._render_roles_page(ADMIN)

    assert fake_st.errors


def test_roles_page_revokes_on_submit(monkeypatch):
    fake_st = _FakeStreamlit(submit_label="Remove role")
    monkeypatch.setattr(roles_page, "st", fake_st)
    monkeypatch.setattr(roles_page, "list_role_assignments", list)
    monkeypatch.setattr(
        roles_page,
        "revoke_role",
        lambda **_k: RoleChangeResult(
            email="target@example.com", old_role="viewer", new_role=None, changed=True
        ),
    )

    roles_page._render_roles_page(ADMIN)

    assert fake_st.successes
    assert fake_st.errors == []
