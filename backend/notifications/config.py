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
import re
from collections.abc import Mapping
from dataclasses import dataclass
from email.errors import HeaderParseError
from email.headerregistry import Address

from backend.config.settings import SettingsError, _parse_bool, load_environment


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


# ALERT-002 content levels. "full" keeps the ALERT-001 behaviour (counts plus the
# top-ranked results); "summary" trims the body to status + counts only.
ALERT_CONTENT_CHOICES = ("summary", "full")


def _content_level(raw: str) -> str:
    """Normalize the alert content level; anything but ``summary`` means ``full``.

    Lenient on purpose: env/config typos fall back to the detailed ``full`` alert
    rather than silently dropping the results list. The admin form uses a stricter
    validator (it rejects unknown values) since the choices there are fixed.
    """
    return "summary" if raw.strip().lower() == "summary" else "full"


def _enabled_flag(raw: str) -> bool:
    """Read ``ALERT_ENABLED`` leniently: default on, and never raise from config load.

    Reuses the canonical bool vocabulary, but an unparseable hand-typed value
    falls back to "on" so a typo cannot silently disable alerts. (The admin form
    uses the strict parser, which rejects typos, because its input is controlled.)
    """
    try:
        return _parse_bool(raw, default=True)
    except SettingsError:
        return True


# --- Strict validators for the admin runtime-config form (ALERT-002) ----------
# These RAISE on bad input (so an admin can never store a value the app would
# reject), unlike the lenient env readers above. The admin form edits the two
# non-secret destinations (chat id, email recipient) plus the enable/content
# toggles; the channel CREDENTIALS stay environment-only.

_TELEGRAM_ID_RE = re.compile(r"^-?\d+$")
_TELEGRAM_HANDLE_RE = re.compile(r"^@[A-Za-z0-9_]+$")
_MAX_EMAIL_ADDRESS_LENGTH = 254


def parse_alert_enabled(raw: str) -> str:
    """Validate ``ALERT_ENABLED`` for the admin form; normalize to "true"/"false"."""
    value = raw.strip().lower()
    if value in {"true", "1", "yes", "on"}:
        return "true"
    if value in {"false", "0", "no", "off"}:
        return "false"
    raise SettingsError(
        "ALERT_ENABLED must be one of true/false, yes/no, on/off, or 1/0."
    )


def parse_alert_content(raw: str) -> str:
    """Validate ``ALERT_CONTENT`` for the admin form; return "summary"/"full"."""
    value = raw.strip().lower()
    if value not in ALERT_CONTENT_CHOICES:
        raise SettingsError(
            f"Invalid ALERT_CONTENT {raw!r}. Expected one of: "
            f"{', '.join(ALERT_CONTENT_CHOICES)}."
        )
    return value


def parse_telegram_chat_id(raw: str) -> str:
    """Validate an optional Telegram destination (numeric chat id or @channel).

    Empty clears the destination (disables Telegram). A non-empty value must be a
    numeric id (optionally negative, for groups) or an ``@channel`` handle.
    """
    value = raw.strip()
    if not value:
        return ""
    if _TELEGRAM_ID_RE.match(value) or _TELEGRAM_HANDLE_RE.match(value):
        return value
    raise SettingsError(
        "TELEGRAM_CHAT_ID must be a numeric chat id or an @channel handle."
    )


def parse_email_recipient(raw: str) -> str:
    """Validate an optional single email recipient for the admin form.

    Empty clears the destination (disables email). A non-empty value must be one
    bounded Internet-style mailbox (not a display name or recipient list) and
    must not contain CR/LF — defense in depth against SMTP header injection (the
    email channel guards this independently too).
    """
    value = raw.strip()
    if not value:
        return ""
    if "\r" in value or "\n" in value:
        raise SettingsError("ALERT_EMAIL_TO must not contain line breaks.")
    if len(value) > _MAX_EMAIL_ADDRESS_LENGTH:
        raise SettingsError(
            f"ALERT_EMAIL_TO must be at most {_MAX_EMAIL_ADDRESS_LENGTH} characters."
        )
    try:
        address = Address(addr_spec=value)
    except (HeaderParseError, ValueError) as exc:
        raise SettingsError(f"Invalid email address: {raw!r}.") from exc
    if not address.username or not address.domain or "." not in address.domain:
        raise SettingsError(f"Invalid email address: {raw!r}.")
    return address.addr_spec


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
    # ALERT-002 preferences. Defaults preserve ALERT-001 behaviour: alerts on, and
    # the detailed ("full") body. An admin can change these at runtime (OBS-003
    # config page) without touching credentials.
    alerts_enabled: bool = True
    alert_content: str = "full"

    @property
    def include_results(self) -> bool:
        """True when the alert body should include the per-stock results list.

        ``full`` (the default) shows status + counts + the top-ranked results;
        ``summary`` trims the body to status + counts only. Returning a bool keeps
        the report builder and renderer from re-parsing the string everywhere.
        """
        return self.alert_content == "full"

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
        """Return log-safe status without credentials or recipient destinations."""
        return {
            "app_url": self.app_url,
            "alerts_enabled": self.alerts_enabled,
            "alert_content": self.alert_content,
            "telegram_configured": self.telegram_configured,
            "email_configured": self.email_configured,
            "smtp_host": self.smtp_host,
            "smtp_port": self.smtp_port,
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
        alerts_enabled=_enabled_flag(pick("ALERT_ENABLED")),
        alert_content=_content_level(pick("ALERT_CONTENT")),
    )
