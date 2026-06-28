"""ALERT-002 — alert preferences are editable runtime config (OBS-003 rail).

These exercise the admin config service against a real (temp) database: an admin
edit validates, persists to ``app_config``, applies to ``os.environ`` live, and is
replayed on the next startup. The destination validators reject bad input.

``monkeypatch.setenv`` establishes a known baseline for each touched key so the
direct ``os.environ`` writes made by ``update_config_value`` are restored after
the test (no cross-test pollution).
"""

from __future__ import annotations

import os

import pytest

from backend.admin import apply_config_overrides, update_config_value
from backend.config.settings import SettingsError
from backend.notifications.config import load_notification_settings
from backend.security import MASK
from backend.storage import get_config_overrides, get_recent_audit_logs


def test_update_alert_content_persists_applies_and_replays(
    file_session_factory, monkeypatch
) -> None:
    monkeypatch.setenv("ALERT_CONTENT", "full")

    result = update_config_value(
        "ALERT_CONTENT",
        "summary",
        updated_by="admin@example.com",
        session_factory=file_session_factory,
    )

    assert result.changed is True
    assert result.old_value == "full"
    assert result.new_value == "summary"
    assert os.environ["ALERT_CONTENT"] == "summary"  # applied to the live process

    # A fresh process (env cleared) still picks the override up from app_config.
    monkeypatch.delenv("ALERT_CONTENT", raising=False)
    applied = apply_config_overrides(session_factory=file_session_factory)
    assert applied.get("ALERT_CONTENT") == "summary"
    assert os.environ["ALERT_CONTENT"] == "summary"


def test_update_alert_destination_round_trips(
    file_session_factory, monkeypatch
) -> None:
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "")

    result = update_config_value(
        "TELEGRAM_CHAT_ID",
        "-1001234567890",
        updated_by="admin@example.com",
        session_factory=file_session_factory,
    )

    assert result.changed is True
    assert os.environ["TELEGRAM_CHAT_ID"] == "-1001234567890"
    assert load_notification_settings().telegram_chat_id == "-1001234567890"


def test_update_alert_destination_masks_audit_values_but_persists_value(
    file_session_factory, monkeypatch
) -> None:
    """Recipients are operational config, but audit/log copies must hide them."""
    monkeypatch.setenv("ALERT_EMAIL_TO", "old-recipient@example.com")

    update_config_value(
        "ALERT_EMAIL_TO",
        "new-recipient@example.com",
        updated_by="admin@example.com",
        session_factory=file_session_factory,
    )

    with file_session_factory() as session:
        assert get_config_overrides(session)["ALERT_EMAIL_TO"] == (
            "new-recipient@example.com"
        )
        audit_row = get_recent_audit_logs(session, event="config_changed")[0]
        audit_metadata = dict(audit_row.metadata_json or {})

    assert audit_metadata == {
        "setting": "ALERT_EMAIL_TO",
        "old_value": MASK,
        "new_value": MASK,
    }


def test_update_rejects_invalid_email_destination(file_session_factory) -> None:
    with pytest.raises(SettingsError):
        update_config_value(
            "ALERT_EMAIL_TO",
            "not-an-email",
            updated_by="admin@example.com",
            session_factory=file_session_factory,
        )
