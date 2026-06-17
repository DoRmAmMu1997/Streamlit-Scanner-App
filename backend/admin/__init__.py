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

__all__ = [
    "EDITABLE_CONFIG_KEYS",
    "ConfigUpdateResult",
    "EditableSetting",
    "apply_config_overrides",
    "update_config_value",
]
