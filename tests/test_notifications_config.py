"""ALERT-001 notification config + secret registration."""

from __future__ import annotations

import pytest

from backend.config.settings import SettingsError, secret_values
from backend.notifications.config import (
    NotificationSettings,
    load_notification_settings,
    parse_alert_content,
    parse_alert_enabled,
    parse_email_recipient,
    parse_telegram_chat_id,
)
from backend.security import redact_text


def test_channels_require_all_their_credentials() -> None:
    assert NotificationSettings().any_configured is False
    telegram = NotificationSettings(telegram_bot_token="t", telegram_chat_id="c")
    assert telegram.telegram_configured is True
    # A token with no chat id is not enough to send.
    assert NotificationSettings(telegram_bot_token="t").telegram_configured is False
    email = NotificationSettings(
        smtp_host="smtp.example.com",
        smtp_user="me@example.com",
        smtp_password="pw",
        email_to="you@example.com",
    )
    assert email.email_configured is True
    # Missing recipient disables email.
    assert email.email_configured and not NotificationSettings(
        smtp_host="h", smtp_user="u", smtp_password="p"
    ).email_configured


def test_load_parses_env_and_defaults_port() -> None:
    settings = load_notification_settings(
        env={
            "APP_URL": "https://scanner.example.com",
            "TELEGRAM_BOT_TOKEN": '"123:abc"',  # surrounding quotes stripped
            "TELEGRAM_CHAT_ID": "456",
            "SMTP_PORT": "not-a-port",
        }
    )
    assert settings.app_url == "https://scanner.example.com"
    assert settings.telegram_bot_token == "123:abc"
    assert settings.smtp_port == 587  # bad value falls back to the STARTTLS default


def test_email_from_falls_back_to_user() -> None:
    assert NotificationSettings(smtp_user="me@x.com").email_from == "me@x.com"
    assert NotificationSettings(smtp_user="me@x.com", smtp_from="alerts@x.com").email_from == "alerts@x.com"


def test_safe_dict_hides_secret_values() -> None:
    safe = NotificationSettings(
        telegram_bot_token="super-secret-token", smtp_password="super-secret-pw"
    ).safe_dict()
    assert safe["has_telegram_bot_token"] is True
    assert safe["has_smtp_password"] is True
    assert "super-secret-token" not in repr(safe)
    assert "super-secret-pw" not in repr(safe)


def test_secrets_are_registered_for_redaction(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "11223344:AAtelegramsecret")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-password-secret")
    values = secret_values()
    assert "11223344:AAtelegramsecret" in values
    assert "smtp-password-secret" in values
    # And the shared redactor masks them in arbitrary text.
    leaked = "bot=11223344:AAtelegramsecret pw=smtp-password-secret"
    redacted = redact_text(leaked)
    assert "11223344:AAtelegramsecret" not in redacted
    assert "smtp-password-secret" not in redacted


# --- ALERT-002 preferences --------------------------------------------------


def test_preference_defaults_preserve_alert_001_behaviour() -> None:
    settings = NotificationSettings()
    assert settings.alerts_enabled is True
    assert settings.alert_content == "full"
    assert settings.include_results is True


def test_summary_content_excludes_results() -> None:
    assert NotificationSettings(alert_content="summary").include_results is False


def test_load_reads_enable_and_content_preferences() -> None:
    settings = load_notification_settings(
        env={"ALERT_ENABLED": "false", "ALERT_CONTENT": "summary"}
    )
    assert settings.alerts_enabled is False
    assert settings.alert_content == "summary"


def test_load_is_lenient_on_bad_preference_values() -> None:
    # A typo must not silently disable alerts or drop the results list.
    settings = load_notification_settings(
        env={"ALERT_ENABLED": "maybe", "ALERT_CONTENT": "verbose"}
    )
    assert settings.alerts_enabled is True
    assert settings.alert_content == "full"


def test_parse_alert_enabled_normalizes_and_rejects() -> None:
    assert parse_alert_enabled("yes") == "true"
    assert parse_alert_enabled("OFF") == "false"
    with pytest.raises(SettingsError):
        parse_alert_enabled("maybe")


def test_parse_alert_content_validates_choice() -> None:
    assert parse_alert_content("Summary") == "summary"
    assert parse_alert_content("full") == "full"
    with pytest.raises(SettingsError):
        parse_alert_content("verbose")


def test_parse_telegram_chat_id_accepts_ids_and_handles() -> None:
    assert parse_telegram_chat_id("  123456  ") == "123456"
    assert parse_telegram_chat_id("-1001234567890") == "-1001234567890"
    assert parse_telegram_chat_id("@my_channel") == "@my_channel"
    assert parse_telegram_chat_id("") == ""  # empty clears the destination
    with pytest.raises(SettingsError):
        parse_telegram_chat_id("not a chat id")


def test_parse_email_recipient_validates_and_blocks_header_injection() -> None:
    assert parse_email_recipient("  ops@example.com ") == "ops@example.com"
    assert parse_email_recipient("") == ""  # empty clears the destination
    with pytest.raises(SettingsError):
        parse_email_recipient("not-an-email")
    with pytest.raises(SettingsError):
        parse_email_recipient("ops@example.com\nBcc: evil@example.com")
