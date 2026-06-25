"""ALERT-001 service orchestration: opt-in, non-fatal, multi-channel."""

from __future__ import annotations

from dataclasses import dataclass, field

from backend.notifications.config import NotificationSettings
from backend.notifications.service import notify_daily_scan

BOTH_CHANNELS = NotificationSettings(
    telegram_bot_token="t",
    telegram_chat_id="c",
    smtp_host="smtp.example.com",
    smtp_user="me@example.com",
    smtp_password="pw",
    email_to="you@example.com",
)


@dataclass
class _Outcome:
    screener_key: str = "s"
    universe_key: str | None = "fno"
    status: object = None
    run_id: int | None = None
    row_count: int = 0
    fatal: bool = False
    message: str = ""


@dataclass
class _Summary:
    outcomes: list[_Outcome] = field(default_factory=list)
    _exit: int = 0

    @property
    def exit_code(self) -> int:
        return self._exit


def test_no_channel_configured_is_a_logged_noop() -> None:
    calls: list[str] = []

    def telegram(_text: str, *, settings: NotificationSettings) -> None:
        calls.append("telegram")

    result = notify_daily_scan(
        _Summary(), settings=NotificationSettings(), telegram_sender=telegram
    )
    assert result.skipped is True
    assert calls == []


def test_one_channel_failure_does_not_block_the_other_or_raise() -> None:
    emails: list[tuple[str, str]] = []

    def telegram(_text: str, *, settings: NotificationSettings) -> None:
        raise RuntimeError("telegram down")

    def email(subject: str, body: str, *, settings: NotificationSettings) -> None:
        emails.append((subject, body))

    result = notify_daily_scan(
        _Summary(), settings=BOTH_CHANNELS, telegram_sender=telegram, email_sender=email
    )

    by_channel = {channel.channel: channel for channel in result.channels}
    assert by_channel["telegram"].sent is False
    assert by_channel["telegram"].error  # captured, not raised
    assert by_channel["email"].sent is True
    assert result.any_sent is True
    assert len(emails) == 1


def test_unexpected_sender_errors_redact_channel_secrets() -> None:
    settings = NotificationSettings(
        telegram_bot_token="telegram-secret-token",
        telegram_chat_id="c",
        smtp_host="smtp.example.com",
        smtp_user="me@example.com",
        smtp_password="smtp-secret-password",
        email_to="you@example.com",
    )

    def telegram(_text: str, *, settings: NotificationSettings) -> None:
        raise RuntimeError(f"boom {settings.telegram_bot_token}")

    def email(_subject: str, _body: str, *, settings: NotificationSettings) -> None:
        raise RuntimeError(f"boom {settings.smtp_password}")

    result = notify_daily_scan(
        _Summary(),
        settings=settings,
        telegram_sender=telegram,
        email_sender=email,
    )

    errors = {channel.channel: channel.error or "" for channel in result.channels}
    assert "telegram-secret-token" not in errors["telegram"]
    assert "smtp-secret-password" not in errors["email"]
    assert "***REDACTED***" in errors["telegram"]
    assert "***REDACTED***" in errors["email"]


def test_failed_run_renders_a_failure_alert() -> None:
    sent_text: list[str] = []

    def telegram(text: str, *, settings: NotificationSettings) -> None:
        sent_text.append(text)

    notify_daily_scan(
        _Summary(_exit=1),
        settings=NotificationSettings(telegram_bot_token="t", telegram_chat_id="c"),
        telegram_sender=telegram,
    )
    assert "Daily scan FAILED" in sent_text[0]


def test_disabled_alert_sends_nothing_even_when_configured() -> None:
    # ALERT-002: an admin can switch alerts off without removing credentials.
    calls: list[str] = []

    def telegram(_text: str, *, settings: NotificationSettings) -> None:
        calls.append("telegram")

    disabled = NotificationSettings(
        telegram_bot_token="t", telegram_chat_id="c", alerts_enabled=False
    )
    result = notify_daily_scan(_Summary(), settings=disabled, telegram_sender=telegram)

    assert result.skipped is True
    assert calls == []
