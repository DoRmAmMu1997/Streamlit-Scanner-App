"""Send one message via the Telegram Bot API (ALERT-001).

A thin, fixed-endpoint client following the SerpAPI client pattern: one host,
an injected ``requests.Session`` for tests, an enforced timeout, and exceptions
scrubbed through ``redact_text`` with the bot token as an extra secret so a
request URL (which embeds the token) can never leak into logs or errors.
"""

from __future__ import annotations

import requests

from backend.notifications.config import NotificationSettings
from backend.security import redact_text
from backend.url_safety import is_safe_http_url

TELEGRAM_API_HOST = "api.telegram.org"
TELEGRAM_TIMEOUT_SECONDS = 20


class TelegramSendError(RuntimeError):
    """Raised when a Telegram message could not be delivered."""


def send_telegram(
    text: str,
    *,
    settings: NotificationSettings,
    session: requests.Session | None = None,
) -> None:
    """POST ``text`` to the configured chat. Raises ``TelegramSendError`` on failure.

    The token lives only in the request URL (never in ``text``); the host is
    fixed and re-checked with the SSRF allowlist so a tampered base can't redirect
    the call. ``disable_web_page_preview`` keeps the app link from expanding.
    """
    token = settings.telegram_bot_token
    url = f"https://{TELEGRAM_API_HOST}/bot{token}/sendMessage"
    if not is_safe_http_url(url, allowed_hosts={TELEGRAM_API_HOST}):
        raise TelegramSendError("Refusing to call an unsafe Telegram URL.")

    http = session or requests.Session()
    payload = {
        "chat_id": settings.telegram_chat_id,
        "text": text,
        "disable_web_page_preview": True,
    }
    try:
        response = http.post(url, json=payload, timeout=TELEGRAM_TIMEOUT_SECONDS)
        response.raise_for_status()
        body = response.json()
    except requests.RequestException as exc:
        # A requests error can echo the full URL, which contains the bot token.
        detail = redact_text(str(exc), extra_secrets=[token])
        raise TelegramSendError(f"Telegram request failed: {detail}") from exc
    except ValueError as exc:  # response.json() on a non-JSON body
        raise TelegramSendError("Telegram returned a non-JSON response.") from exc

    # Telegram signals API-level problems in an ``ok: false`` body, sometimes with
    # HTTP 200, so check the body even after raise_for_status() passed.
    if not (isinstance(body, dict) and body.get("ok")):
        description = ""
        if isinstance(body, dict):
            description = redact_text(str(body.get("description", "")), extra_secrets=[token])
        raise TelegramSendError(
            f"Telegram API rejected the message: {description}".strip()
        )
