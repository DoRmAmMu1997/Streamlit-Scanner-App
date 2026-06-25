"""Admin runtime-config page (OBS-003).

A small admin-only form to change whitelisted *operational* settings
(``LOG_LEVEL``, ``LOG_FORMAT``) without redeploying. Saving validates the value,
persists it to the ``app_config`` table, applies it to the live process, and
records a ``config_changed`` audit event. The actual logic lives in
``backend.admin`` so it stays testable without Streamlit; this module is only the
rendering.

Admin-gated like the other admin pages: the main view selector hides it from
non-admins, and this renderer repeats the check as defense in depth.
"""

from __future__ import annotations

import streamlit as st

from backend.admin import EDITABLE_CONFIG_KEYS, update_config_value
from backend.auth.session import AuthenticatedUser
from backend.config.settings import SettingsError
from ui.common import _redact_secrets


def _render_config_page(authenticated_user: AuthenticatedUser | None) -> None:
    """Render the admin-only runtime settings form."""
    if authenticated_user is None or not authenticated_user.is_admin:
        st.error("Admin access is required to change settings.")
        return

    st.subheader("Admin settings")
    st.caption(
        "Change operational settings at runtime. Changes are validated, applied "
        "immediately, persisted across restarts, and recorded in the audit log."
    )

    # Pre-fill each control with the current effective value so the form shows
    # the live state. Selections are collected inside the form and only acted on
    # when the admin submits.
    with st.form("admin_config_form"):
        selections: dict[str, str] = {}
        for setting in EDITABLE_CONFIG_KEYS.values():
            current = setting.current()
            if setting.choices:
                index = (
                    setting.choices.index(current)
                    if current in setting.choices
                    else 0
                )
                selections[setting.key] = st.selectbox(
                    setting.label,
                    setting.choices,
                    index=index,
                    help=setting.help,
                    key=f"config_{setting.key}",
                )
            else:
                # Free-text settings (e.g. ALERT-002 alert destinations). The value
                # is still validated by ``setting.parse`` on save.
                selections[setting.key] = st.text_input(
                    setting.label,
                    value=current,
                    help=setting.help,
                    key=f"config_{setting.key}",
                )
        submitted = st.form_submit_button("Save settings", type="primary")

    if not submitted:
        return

    changed_any = False
    for key, value in selections.items():
        try:
            result = update_config_value(
                key, value, updated_by=authenticated_user.email
            )
        except SettingsError as exc:
            st.error(f"{key}: {_redact_secrets(str(exc))}")
            continue
        if result.changed:
            changed_any = True
            st.success(
                f"{EDITABLE_CONFIG_KEYS[key].label}: "
                f"{result.old_value} → {result.new_value}"
            )
    if not changed_any:
        st.info("No changes to save.")
