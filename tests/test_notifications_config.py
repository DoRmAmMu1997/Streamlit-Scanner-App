"""ALERT-001 notification config + secret registration."""

from __future__ import annotations

from backend.config.settings import secret_values
from backend.notifications.config import (
    NotificationSettings,
    load_notification_settings,
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
