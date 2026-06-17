"""Tests for the OBS-003 admin runtime-config service (backend.admin).

These exercise validation, persistence, live application to ``os.environ``, and
the ``config_changed`` audit trail. ``monkeypatch.delenv`` both clears any prior
value and registers the env var for restoration, so a raw ``os.environ`` write by
the code under test cannot leak between tests.
"""

from __future__ import annotations

import pytest

from backend.admin.config_service import (
    apply_config_overrides,
    update_config_value,
)
from backend.config.settings import SettingsError, get_settings
from backend.storage.repository import (
    get_recent_audit_logs,
    set_config_override,
)


@pytest.fixture(autouse=True)
def _isolate_config_env(monkeypatch):
    """Keep LOG_LEVEL / LOG_FORMAT changes from leaking across tests."""
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    monkeypatch.delenv("LOG_FORMAT", raising=False)


def test_update_config_value_persists_applies_and_audits(file_session_factory, monkeypatch):
    """A real change is validated, stored, applied to env, and audited."""
    import os

    old_value = get_settings().log_level  # "WARNING" with the env cleared above

    result = update_config_value(
        "LOG_LEVEL",
        "debug",
        updated_by="admin@example.com",
        session_factory=file_session_factory,
    )

    assert result.changed is True
    assert result.old_value == old_value
    assert result.new_value == "DEBUG"
    # Applied live: get_settings() reads os.environ on every call.
    assert os.environ["LOG_LEVEL"] == "DEBUG"
    assert get_settings().log_level == "DEBUG"

    with file_session_factory() as session:
        audit_rows = get_recent_audit_logs(session, event="config_changed")
        assert len(audit_rows) == 1
        assert audit_rows[0].user_email == "admin@example.com"
        assert audit_rows[0].metadata_json == {
            "setting": "LOG_LEVEL",
            "old_value": old_value,
            "new_value": "DEBUG",
        }


def test_update_config_value_noop_when_unchanged(file_session_factory):
    """Submitting the current value records nothing and reports no change."""
    current = get_settings().log_level

    result = update_config_value(
        "LOG_LEVEL",
        current,
        updated_by="admin@example.com",
        session_factory=file_session_factory,
    )

    assert result.changed is False
    with file_session_factory() as session:
        assert get_recent_audit_logs(session, event="config_changed") == []


def test_update_config_value_rejects_invalid_value(file_session_factory):
    """An invalid value is rejected by the same parser startup uses."""
    with pytest.raises(SettingsError):
        update_config_value(
            "LOG_LEVEL",
            "NOPE",
            updated_by="admin@example.com",
            session_factory=file_session_factory,
        )


def test_update_config_value_rejects_non_editable_key(file_session_factory):
    """Only whitelisted operational keys are editable."""
    with pytest.raises(SettingsError):
        update_config_value(
            "DATABASE_URL",
            "sqlite:///somewhere.db",
            updated_by="admin@example.com",
            session_factory=file_session_factory,
        )


def test_apply_config_overrides_loads_whitelisted_keys(file_session_factory):
    """Stored overrides are replayed into os.environ on startup."""
    import os

    with file_session_factory() as session:
        set_config_override(
            session, key="LOG_LEVEL", value="DEBUG", updated_by="admin@example.com"
        )
        # A non-whitelisted key must be ignored even if somehow stored.
        set_config_override(
            session, key="DATABASE_URL", value="sqlite:///x.db", updated_by="admin@example.com"
        )

    applied = apply_config_overrides(session_factory=file_session_factory)

    assert applied == {"LOG_LEVEL": "DEBUG"}
    assert os.environ["LOG_LEVEL"] == "DEBUG"
    assert os.environ.get("DATABASE_URL") != "sqlite:///x.db"


def test_apply_config_overrides_skips_invalid_stored_value(file_session_factory):
    """A stored value that no longer validates is skipped, not applied."""
    import os

    with file_session_factory() as session:
        set_config_override(
            session, key="LOG_FORMAT", value="bogus", updated_by="admin@example.com"
        )

    applied = apply_config_overrides(session_factory=file_session_factory)

    assert applied == {}
    assert "LOG_FORMAT" not in os.environ
