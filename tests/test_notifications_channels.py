"""ALERT-001 channel senders (Telegram over fake HTTP, email over fake SMTP)."""

from __future__ import annotations

import pytest
import requests

from backend.notifications import telegram_channel
from backend.notifications.config import NotificationSettings
from backend.notifications.email_channel import EmailSendError, send_email
from backend.notifications.telegram_channel import TelegramSendError, send_telegram

TELEGRAM_SETTINGS = NotificationSettings(
    telegram_bot_token="11223344:AAtelegramsecret", telegram_chat_id="999"
)
EMAIL_SETTINGS = NotificationSettings(
    smtp_host="smtp.example.com",
    smtp_user="me@example.com",
    smtp_password="smtp-password-secret",
    email_to="you@example.com",
)


class _FakeResponse:
    def __init__(self, payload: object, *, raise_exc: Exception | None = None) -> None:
        self._payload = payload
        self._raise_exc = raise_exc

    def raise_for_status(self) -> None:
        if self._raise_exc is not None:
            raise self._raise_exc

    def json(self) -> object:
        return self._payload


class _FakeSession:
    def __init__(self, response: _FakeResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def post(self, url: str, *, json: dict[str, object], timeout: float) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        return self.response


def test_telegram_posts_to_fixed_host_without_token_in_body() -> None:
    session = _FakeSession(_FakeResponse({"ok": True, "result": {}}))
    send_telegram("hello world", settings=TELEGRAM_SETTINGS, session=session)
    call = session.calls[0]
    assert call["url"].startswith("https://api.telegram.org/bot")
    assert call["json"]["chat_id"] == "999"
    assert call["json"]["text"] == "hello world"
    # The bot token rides only in the URL path, never in the message body.
    assert TELEGRAM_SETTINGS.telegram_bot_token not in call["json"]["text"]


def test_telegram_api_level_failure_raises() -> None:
    session = _FakeSession(_FakeResponse({"ok": False, "description": "Unauthorized"}))
    with pytest.raises(TelegramSendError, match="Unauthorized"):
        send_telegram("hi", settings=TELEGRAM_SETTINGS, session=session)


def test_telegram_request_error_is_wrapped_and_token_redacted() -> None:
    token = TELEGRAM_SETTINGS.telegram_bot_token
    boom = requests.RequestException(f"failed calling https://api.telegram.org/bot{token}/x")
    session = _FakeSession(_FakeResponse({"ok": True}, raise_exc=boom))
    with pytest.raises(TelegramSendError) as excinfo:
        send_telegram("hi", settings=TELEGRAM_SETTINGS, session=session)
    assert token not in str(excinfo.value)


def test_telegram_rejects_unsafe_host(monkeypatch) -> None:
    # A non-public host must be refused by the SSRF guard before any send.
    monkeypatch.setattr(telegram_channel, "TELEGRAM_API_HOST", "127.0.0.1")
    session = _FakeSession(_FakeResponse({"ok": True}))
    with pytest.raises(TelegramSendError, match="unsafe"):
        send_telegram("hi", settings=TELEGRAM_SETTINGS, session=session)
    assert session.calls == []


class _FakeSMTP:
    def __init__(self, host: str, port: int, *, timeout: float) -> None:
        self.host = host
        self.port = port
        self.timeout = timeout
        self.events: list[str] = []
        self.sent_message: object = None

    def __enter__(self) -> _FakeSMTP:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def starttls(self, *, context: object | None = None) -> None:
        # A verifying TLS context must be supplied (no bare, unverified STARTTLS).
        assert context is not None
        self.events.append("starttls")

    def login(self, user: str, password: str) -> None:
        self.events.append(f"login:{user}")

    def send_message(self, msg: object) -> None:
        self.events.append("send")
        self.sent_message = msg


def test_email_sends_with_starttls_then_login_then_send() -> None:
    captured: dict[str, _FakeSMTP] = {}

    def factory(host: str, port: int, *, timeout: float) -> _FakeSMTP:
        server = _FakeSMTP(host, port, timeout=timeout)
        captured["server"] = server
        return server

    send_email("subject", "body text", settings=EMAIL_SETTINGS, smtp_factory=factory)
    server = captured["server"]
    assert server.events == ["starttls", "login:me@example.com", "send"]
    # The password must never appear in the composed message.
    assert EMAIL_SETTINGS.smtp_password not in str(server.sent_message)


def test_email_smtp_error_is_wrapped_and_password_redacted() -> None:
    import smtplib

    def factory(host: str, port: int, *, timeout: float) -> _FakeSMTP:
        raise smtplib.SMTPException(
            f"auth failed for password {EMAIL_SETTINGS.smtp_password}"
        )

    with pytest.raises(EmailSendError) as excinfo:
        send_email("s", "b", settings=EMAIL_SETTINGS, smtp_factory=factory)
    assert EMAIL_SETTINGS.smtp_password not in str(excinfo.value)
