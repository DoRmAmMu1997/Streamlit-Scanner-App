"""ALERT-001 daily-scan notification service: build -> render -> send (non-fatal).

The single entry point the daily job calls. It is opt-in (a no-op when no channel
is configured) and never raises: report-building and each channel send are guarded
so a notification problem can never change the scan job's exit code.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.orm import Session

from backend.notifications.config import (
    NotificationSettings,
    load_notification_settings,
)
from backend.notifications.email_channel import send_email
from backend.notifications.render import render_email, render_telegram
from backend.notifications.report import DailyScanReport, build_daily_scan_report
from backend.notifications.telegram_channel import send_telegram
from backend.observability import (
    EVENT_NOTIFICATION_FAILED,
    EVENT_NOTIFICATION_SENT,
    EVENT_NOTIFICATION_SKIPPED,
    log_event,
)
from backend.security import redact_exception
from backend.storage import session_scope

if TYPE_CHECKING:
    from backend.jobs.run_daily_scan import DailyScanSummary

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]
TelegramSender = Callable[..., None]
EmailSender = Callable[..., None]


@dataclass(frozen=True)
class ChannelResult:
    """The outcome of one channel send."""

    channel: str
    sent: bool
    error: str | None = None


@dataclass(frozen=True)
class NotificationResult:
    """All channel outcomes for one notification call."""

    channels: tuple[ChannelResult, ...] = ()

    @property
    def skipped(self) -> bool:
        """True when nothing was attempted (no channel configured)."""
        return not self.channels

    @property
    def any_sent(self) -> bool:
        """True when at least one channel delivered."""
        return any(result.sent for result in self.channels)


def notify_daily_scan(
    summary: DailyScanSummary,
    *,
    settings: NotificationSettings | None = None,
    session_factory: SessionFactory = session_scope,
    telegram_sender: TelegramSender = send_telegram,
    email_sender: EmailSender = send_email,
) -> NotificationResult:
    """Send the daily-scan summary to every configured channel. Never raises.

    Opt-in: with no channel configured the call is a logged no-op. Each channel is
    attempted independently; a send failure is logged, not raised, so a
    notification problem can never change the scan job's exit code (ALERT-001).
    """
    settings = settings or load_notification_settings()
    if not settings.alerts_enabled:
        # ALERT-002: an admin can switch alerts off without removing credentials.
        log_event(logger, EVENT_NOTIFICATION_SKIPPED, reason="disabled")
        return NotificationResult()
    if not settings.any_configured:
        log_event(logger, EVENT_NOTIFICATION_SKIPPED, reason="no_channel_configured")
        return NotificationResult()

    try:
        report = build_daily_scan_report(
            summary, settings=settings, session_factory=session_factory
        )
    except Exception:  # noqa: BLE001 - building the report must not raise into the job
        logger.warning("daily-scan notification report build failed", exc_info=True)
        return NotificationResult()

    results: list[ChannelResult] = []
    if settings.telegram_configured:
        text = render_telegram(report)
        results.append(
            _attempt(
                "telegram",
                report,
                lambda: telegram_sender(text, settings=settings),
                extra_secrets=[settings.telegram_bot_token],
            )
        )
    if settings.email_configured:
        subject, body = render_email(report)
        results.append(
            _attempt(
                "email",
                report,
                lambda: email_sender(subject, body, settings=settings),
                extra_secrets=[settings.smtp_password],
            )
        )
    return NotificationResult(channels=tuple(results))


def _attempt(
    channel: str,
    report: DailyScanReport,
    send: Callable[[], None],
    *,
    extra_secrets: Iterable[str] = (),
) -> ChannelResult:
    """Run one channel send, converting any failure into a logged ``ChannelResult``."""
    try:
        send()
    except Exception as exc:  # noqa: BLE001 - one channel must not break others/the job
        # Channel implementations already scrub their own known secret values,
        # but tests inject arbitrary senders too. Passing the channel secret here
        # gives the service boundary one final redaction chance before logging.
        detail = redact_exception(exc, extra_secrets=extra_secrets)
        log_event(
            logger,
            EVENT_NOTIFICATION_FAILED,
            level=logging.WARNING,
            channel=channel,
            error_type=type(exc).__name__,
            reason=detail,
        )
        return ChannelResult(channel=channel, sent=False, error=detail)
    log_event(
        logger,
        EVENT_NOTIFICATION_SENT,
        channel=channel,
        ok=report.ok,
        shortlisted=report.total_shortlisted,
    )
    return ChannelResult(channel=channel, sent=True)
