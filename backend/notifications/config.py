"""ALERT-001 notification settings (Telegram + email), read from the environment.

Kept out of the central ``AppSettings`` on purpose: these are an optional,
opt-in feature, not production-required runtime config. A channel is only
"configured" when *all* of its credentials are present, which is how the service
decides whether to send (and how the daily job stays silent on a fresh checkout
with no alert credentials).

Security note:
``TELEGRAM_BOT_TOKEN`` and ``SMTP_PASSWORD`` are the two secrets here. They are
also registered in ``backend.config.settings.secret_values`` so the shared
SEC-002 redaction filter masks them everywhere (logs, UI errors, persisted
messages) — exactly like the existing Dhan/SerpAPI secrets.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass

from backend.config.settings import load_environment


def _clean(value: object) -> str:
    """Strip whitespace and one matching surrounding quote pair (env hygiene)."""
    cleaned = str(value or "").strip()
    if len(cleaned) >= 2 and cleaned[0] in "\"'" and cleaned[-1] == cleaned[0]:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _port(raw: str, *, default: int = 587) -> int:
    """Parse an SMTP port, falling back to the STARTTLS default on bad input."""
    try:
        port = int(raw)
    except (TypeError, ValueError):
        return default
    return port if 1 <= port <= 65535 else default


@dataclass(frozen=True)
class NotificationSettings:
    """Typed, opt-in notification configuration.

    All fields default to empty so an absent credential simply disables the
    matching channel rather than raising. ``smtp_port`` defaults to 587 (STARTTLS).
    """

    app_url: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""
    email_to: str = ""

    @property
    def telegram_configured(self) -> bool:
        """True only when both the bot token and the target chat id are set."""
        return bool(self.telegram_bot_token and self.telegram_chat_id)

    @property
    def email_configured(self) -> bool:
        """True only when host, user, password, and a recipient are all set."""
        return bool(
            self.smtp_host and self.smtp_user and self.smtp_password and self.email_to
        )

    @property
    def any_configured(self) -> bool:
        """True when at least one channel can actually send."""
        return self.telegram_configured or self.email_configured

    @property
    def email_from(self) -> str:
        """The From header — explicit ``SMTP_FROM`` or the login user as fallback."""
        return self.smtp_from or self.smtp_user

    def safe_dict(self) -> dict[str, object]:
        """A log/debug-safe summary that never includes the two secret values."""
        return {
            "app_url": self.app_url,
            "telegram_configured": self.telegram_configured,
            "email_configured": self.email_configured,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
            "email_to": self.email_to,
            "has_telegram_bot_token": bool(self.telegram_bot_token),
            "has_smtp_password": bool(self.smtp_password),
        }


def load_notification_settings(
    env: Mapping[str, str] | None = None,
) -> NotificationSettings:
    """Read notification settings from ``env`` (default: dotenv + process env).

    Tests pass an explicit ``env`` mapping; runtime code passes nothing so the
    local ``Dependencies/.env`` and the real process environment are read.
    """
    if env is None:
        load_environment()
        source: Mapping[str, str] = os.environ
    else:
        source = env

    def pick(name: str) -> str:
        return _clean(source.get(name))

    return NotificationSettings(
        app_url=pick("APP_URL"),
        telegram_bot_token=pick("TELEGRAM_BOT_TOKEN"),
        telegram_chat_id=pick("TELEGRAM_CHAT_ID"),
        smtp_host=pick("SMTP_HOST"),
        smtp_port=_port(pick("SMTP_PORT")),
        smtp_user=pick("SMTP_USER"),
        smtp_password=pick("SMTP_PASSWORD"),
        smtp_from=pick("SMTP_FROM"),
        email_to=pick("ALERT_EMAIL_TO"),
    )
