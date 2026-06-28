"""AUTH-003 admin Roles page (ui.roles_page).

Admin-only UI to assign and revoke viewer/analyst/admin roles. The logic lives in
``backend.admin.roles_service`` so it stays testable without Streamlit; this module
is only the rendering. Admin-gated like the other admin pages: the main view
selector hides it from non-admins, the dispatcher re-checks the ``manage_roles``
capability, and this renderer repeats the check as defense in depth.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.admin import (
    RoleAssignmentError,
    assign_role,
    list_role_assignments,
    revoke_role,
)
from backend.auth.session import AuthenticatedUser
from ui.common import _redact_secrets

_ROLE_CHOICES = ("viewer", "analyst", "admin")


def _render_roles_page(authenticated_user: AuthenticatedUser | None) -> None:
    """Render the admin-only role-management page."""
    if authenticated_user is None or not authenticated_user.is_admin:
        st.error("Admin access is required to manage roles.")
        return

    st.subheader("Admin roles")
    st.caption(
        "Assign viewer / analyst / admin roles. A user listed here may sign in and "
        "gets that role; ADMIN_EMAILS stays a bootstrap admin that cannot be removed "
        "from here. Every change is recorded in the audit log."
    )

    assignments = list_role_assignments()
    if assignments:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Email": item.email,
                        "Role": item.role,
                        "Assigned by": item.assigned_by or "—",
                    }
                    for item in assignments
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    else:
        st.info(
            "No role assignments yet. Admins come from ADMIN_EMAILS until you add some."
        )

    with st.form("assign_role_form"):
        st.markdown("**Assign or update a role**")
        email = st.text_input("User email", key="assign_role_email")
        role = st.selectbox("Role", _ROLE_CHOICES, key="assign_role_role")
        assign_submitted = st.form_submit_button("Save role", type="primary")
    if assign_submitted:
        try:
            result = assign_role(
                email=email, role=role, assigned_by=authenticated_user.email
            )
        except RoleAssignmentError as exc:
            st.error(_redact_secrets(str(exc)))
        else:
            if result.changed:
                st.success(f"{result.email}: {result.old_role or '—'} → {result.new_role}")
            else:
                st.info("No change — that user already has that role.")

    with st.form("revoke_role_form"):
        st.markdown("**Remove a role** (also revokes table-granted sign-in)")
        revoke_email = st.text_input("User email", key="revoke_role_email")
        revoke_submitted = st.form_submit_button("Remove role")
    if revoke_submitted:
        try:
            result = revoke_role(email=revoke_email, revoked_by=authenticated_user.email)
        except RoleAssignmentError as exc:
            st.error(_redact_secrets(str(exc)))
        else:
            if result.changed:
                st.success(f"Removed the role for {result.email}.")
            else:
                st.info("That user had no role assignment to remove.")
