"""OBS-003 admin services — runtime config overrides behind the admin UI.

Friendly public path for the admin settings page in ``ui/``. The logic lives in
``config_service.py``; this surface re-exports the names the UI needs.
"""

from backend.admin.config_service import (
    EDITABLE_CONFIG_KEYS,
    ConfigUpdateResult,
    EditableSetting,
    apply_config_overrides,
    update_config_value,
)
from backend.admin.roles_service import (
    RoleAssignment,
    RoleAssignmentError,
    RoleChangeResult,
    assign_role,
    list_role_assignments,
    revoke_role,
)

__all__ = [
    "EDITABLE_CONFIG_KEYS",
    "ConfigUpdateResult",
    "EditableSetting",
    "RoleAssignment",
    "RoleAssignmentError",
    "RoleChangeResult",
    "apply_config_overrides",
    "assign_role",
    "list_role_assignments",
    "revoke_role",
    "update_config_value",
]
