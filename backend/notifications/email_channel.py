"""Send one summary email over SMTP with STARTTLS (ALERT-001).

Stdlib only (``smtplib`` + ``email.message``) — no new dependency. The SMTP
factory is injectable so tests assert the STARTTLS -> login -> send order without
a real server, and SMTP exceptions are scrubbed (with the password as an extra
secret) before they leave this boundary.
"""

from __future__ import annotations

import smtplib
import ssl
from collections.abc import Callable
from email.message import EmailMessage

from backend.notifications.config import NotificationSettings
from backend.security import redact_text

EMAIL_TIMEOUT_SECONDS = 20

# Builds an SMTP connection for a host/port. Injectable so tests pass a
# duck-typed fake; production uses ``smtplib.SMTP`` (STARTTLS on port 587).
SmtpFactory = Callable[..., smtplib.SMTP]


class EmailSendError(RuntimeError):
    """Raised when the summary email could not be sent."""


def _default_smtp_factory(host: str, port: int, *, timeout: float) -> smtplib.SMTP:
    """Build a real ``smtplib.SMTP`` connection (the production factory)."""
    return smtplib.SMTP(host, port, timeout=timeout)


def send_email(
    subject: str,
    body: str,
    *,
    settings: NotificationSettings,
    smtp_factory: SmtpFactory = _default_smtp_factory,
) -> None:
    """Send the summary email. Raises ``EmailSendError`` on any SMTP/network fault."""
    try:
        message = EmailMessage()
        # Header assignment validates CR/LF injection before any SMTP connection
        # is opened. Keep it inside this try block so malformed operator env
        # values become the same non-fatal channel error as network failures.
        message["Subject"] = subject
        message["From"] = settings.email_from
        message["To"] = settings.email_to
        message.set_content(body)

        with smtp_factory(
            settings.smtp_host, settings.smtp_port, timeout=EMAIL_TIMEOUT_SECONDS
        ) as server:
            # Pass a verifying context explicitly: bare starttls() uses a context
            # that does NOT check the certificate/hostname, which would allow a
            # MITM to read the credentials. create_default_context() verifies both.
            server.starttls(context=ssl.create_default_context())
            server.login(settings.smtp_user, settings.smtp_password)
            server.send_message(message)
    except (smtplib.SMTPException, OSError, ValueError) as exc:
        # SMTP/network errors can echo credentials or the server banner.
        detail = redact_text(str(exc), extra_secrets=[settings.smtp_password])
        raise EmailSendError(f"SMTP send failed: {detail}") from exc
