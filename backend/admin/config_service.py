"""OBS-003 — runtime configuration overrides for the admin settings form.

Beginner note:
The app reads its settings from environment variables (see
``backend.config.settings``). There is normally no way to change them while the
app is running. This module adds a *small, safe* runtime-config capability so the
``config_changed`` audit event has a real trigger:

- An admin edits a whitelisted operational setting (``LOG_LEVEL``, ``LOG_FORMAT``,
  and the ALERT-002 daily-alert preferences) in the UI.
- ``update_config_value`` validates the new value with the setting's canonical
  strict admin validator, stores it in the ``app_config`` table, writes it into
  ``os.environ`` so it takes effect immediately (``get_settings()`` reads the
  environment live), and records a ``config_changed`` audit entry.
- ``apply_config_overrides`` replays stored overrides into ``os.environ`` on
  startup, so a change persists across restarts.

Scope is deliberately narrow. Only non-secret operational keys are editable:
credentials and auth/infra settings (``AUTH_REQUIRED``, ``ALLOWED_EMAILS``,
``DATABASE_URL``, ``DATA_DIR``, ``APP_ENV``, API tokens, ...) are intentionally
out of scope, so this never becomes an auth-bypass lever or a secret store. The
ALERT-002 keys follow the same rule: the editable alert *destinations*
(``TELEGRAM_CHAT_ID``, ``ALERT_EMAIL_TO``) are non-secret recipients, while the
channel credentials (``TELEGRAM_BOT_TOKEN``, ``SMTP_PASSWORD``) stay env-only.
Destinations remain plaintext operational config but are privacy-sensitive, so
their values are masked in audit/log metadata and post-save UI feedback.

Design note: ``backend`` never imports Streamlit. This module exposes plain
functions and data; the admin page in ``ui/`` renders them.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.audit import record_audit_event
from backend.config import get_settings
from backend.config.settings import (
    SettingsError,
    _parse_log_format,
    _parse_log_level,
)
from backend.notifications.config import (
    ALERT_CONTENT_CHOICES,
    load_notification_settings,
    parse_alert_content,
    parse_alert_enabled,
    parse_email_recipient,
    parse_telegram_chat_id,
)
from backend.observability import EVENT_CONFIG_CHANGED
from backend.security import MASK
from backend.storage import get_config_overrides, session_scope, set_config_override

logger = logging.getLogger(__name__)

SessionFactory = Any


@dataclass(frozen=True)
class EditableSetting:
    """One runtime-editable environment setting the admin form may change.

    ``parse`` validates and normalizes a raw string for an admin save (raising
    ``SettingsError`` on bad input); ``current`` reads the effective
    value now so the form can pre-fill it and the audit entry can record the
    real "before" value. ``choices`` drives a select box in the UI; leave it empty
    (the default) for a validated free-text input (e.g. an alert destination).
    ``redact_value`` marks privacy-sensitive values that may be stored as runtime
    config but must not be copied into audit/log/UI feedback.
    """

    key: str
    label: str
    help: str
    parse: Callable[[str], str]
    current: Callable[[], str]
    choices: tuple[str, ...] = ()
    redact_value: bool = False


# The whitelist. Keep it small and non-secret on purpose (see module docstring).
EDITABLE_CONFIG_KEYS: dict[str, EditableSetting] = {
    "LOG_LEVEL": EditableSetting(
        key="LOG_LEVEL",
        label="Log level",
        help="Minimum severity emitted to logs. INFO shows the full event stream.",
        choices=("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"),
        parse=_parse_log_level,
        current=lambda: get_settings().log_level,
    ),
    "LOG_FORMAT": EditableSetting(
        key="LOG_FORMAT",
        label="Log format",
        help="auto = JSON in production / text in development; or force json/text.",
        choices=("auto", "json", "text"),
        parse=_parse_log_format,
        current=lambda: get_settings().log_format,
    ),
    # ALERT-002 daily-alert preferences. These read/write the opt-in notification
    # settings (env-backed, applied via apply_config_overrides). Only the two
    # NON-secret destinations are editable here; the bot token and SMTP password
    # stay environment-only (see module docstring).
    "ALERT_ENABLED": EditableSetting(
        key="ALERT_ENABLED",
        label="Daily alerts enabled",
        help="Master on/off switch for the daily-scan alert. Off keeps credentials but sends nothing.",
        choices=("true", "false"),
        parse=parse_alert_enabled,
        current=lambda: "true" if load_notification_settings().alerts_enabled else "false",
    ),
    "ALERT_CONTENT": EditableSetting(
        key="ALERT_CONTENT",
        label="Alert content",
        help="summary = status + counts only; full = also include the top-ranked results.",
        choices=ALERT_CONTENT_CHOICES,
        parse=parse_alert_content,
        current=lambda: load_notification_settings().alert_content,
    ),
    "TELEGRAM_CHAT_ID": EditableSetting(
        key="TELEGRAM_CHAT_ID",
        label="Telegram chat id",
        help="Destination chat/group id or @channel. Empty disables Telegram. The bot token stays in the environment.",
        parse=parse_telegram_chat_id,
        current=lambda: load_notification_settings().telegram_chat_id,
        redact_value=True,
    ),
    "ALERT_EMAIL_TO": EditableSetting(
        key="ALERT_EMAIL_TO",
        label="Alert email recipient",
        help="Destination email address. Empty disables email. SMTP credentials stay in the environment.",
        parse=parse_email_recipient,
        current=lambda: load_notification_settings().email_to,
        redact_value=True,
    ),
}


@dataclass(frozen=True)
class ConfigUpdateResult:
    """Outcome of an attempted config change (used for UI feedback)."""

    key: str
    old_value: str
    new_value: str
    changed: bool


def apply_config_overrides(*, session_factory: SessionFactory = session_scope) -> dict[str, str]:
    """Replay stored runtime overrides into ``os.environ``. Best-effort.

    Called once on startup (and harmlessly again on each Streamlit rerun). Reads
    the ``app_config`` table and, for every *whitelisted* key with a value that
    still validates, sets ``os.environ`` so the rest of the run sees it. Unknown
    or now-invalid stored keys are skipped rather than crashing startup (for
    example if the whitelist shrank in a later release). A database error (table
    not yet migrated) is logged and ignored.

    Returns the ``{key: value}`` actually applied (useful for tests/diagnostics).
    """
    try:
        with session_factory() as session:
            overrides = get_config_overrides(session)
    except Exception:  # noqa: BLE001 - config overrides are best-effort at startup.
        logger.warning(
            "Could not load runtime config overrides; using environment defaults.",
            exc_info=True,
        )
        return {}

    applied: dict[str, str] = {}
    for key, raw_value in overrides.items():
        setting = EDITABLE_CONFIG_KEYS.get(key)
        if setting is None:
            continue
        try:
            parsed = setting.parse(raw_value)
        except SettingsError:
            logger.warning("Ignoring invalid stored override for %s.", key)
            continue
        os.environ[key] = parsed
        applied[key] = parsed
    return applied


def update_config_value(
    key: str,
    raw_value: str,
    *,
    updated_by: str | None,
    session_factory: SessionFactory = session_scope,
) -> ConfigUpdateResult:
    """Validate, persist, apply, and audit one runtime config change.

    Raises ``SettingsError`` if ``key`` is not editable or ``raw_value`` is
    invalid (using the setting's strict admin validator, so the form cannot store
    a value replay would reject on the next boot). When the value is unchanged,
    this is a no-op that records nothing. Otherwise it persists the override, updates
    ``os.environ`` for the live process, and records a ``config_changed`` audit
    event with the old and new values.
    """
    setting = EDITABLE_CONFIG_KEYS.get(key)
    if setting is None:
        raise SettingsError(f"{key!r} is not an editable runtime setting.")

    new_value = setting.parse(raw_value)
    old_value = setting.current()
    if new_value == old_value:
        return ConfigUpdateResult(key=key, old_value=old_value, new_value=new_value, changed=False)

    with session_factory() as session:
        set_config_override(session, key=key, value=new_value, updated_by=updated_by)

    # Apply to the live process so the change takes effect on this run too;
    # get_settings() re-reads os.environ on every call.
    os.environ[key] = new_value

    audit_old_value = MASK if setting.redact_value else old_value
    audit_new_value = MASK if setting.redact_value else new_value
    record_audit_event(
        event=EVENT_CONFIG_CHANGED,
        user_email=updated_by,
        metadata={
            "setting": key,
            "old_value": audit_old_value,
            "new_value": audit_new_value,
        },
        # Reuse the injected factory so both writes target the same database. The
        # audit recorder intentionally remains a separate best-effort transaction.
        session_factory=session_factory,
    )
    return ConfigUpdateResult(key=key, old_value=old_value, new_value=new_value, changed=True)
