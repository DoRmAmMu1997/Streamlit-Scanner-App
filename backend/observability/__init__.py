"""OBS-001 - structured, secret-safe logging for the scanner.

Beginner note:
"Observability" just means: when something breaks in production, can you tell
*what* happened from the logs alone? A human reading a terminal is fine with
free-text messages, but a deployed app usually ships its logs to a tool that can
only help if each line is machine-readable. This module gives the app:

1. **Named events.** Instead of ad-hoc messages, important moments get a stable
   name like ``scan_started`` or ``external_api_failed``. You can search or alert
   on the name without parsing English sentences.
2. **Two renderings of the same event.** In development you get a readable line;
   in production you get one JSON object per line. ``LOG_FORMAT`` (via settings)
   chooses which - ``auto`` means JSON when ``APP_ENV`` is production.

Two hard rules this module enforces:

- **No secrets ever reach a log.** Every rendered line is passed through SEC-002's
  ``redact_text`` before it leaves, so a token accidentally placed in a field is
  masked.
- **Context travels with the event.** Callers attach fields such as ``run_id`` and
  ``symbol`` so a failure can be tied back to the exact scan or stock.

Design note (why this file imports so little):
This is a *leaf* utility. It imports only the standard library, the settings
reader, and the redaction helper. It must never import the scan/loader/app modules
that use it - that keeps the dependency direction one-way and avoids import cycles.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from types import TracebackType
from typing import Any, TextIO

from backend.config import get_settings
from backend.config.settings import AppSettings
from backend.security import install_secret_redaction_filter, redact_text

# ---------------------------------------------------------------------------
# Event names
# ---------------------------------------------------------------------------
# One constant per OBS-001 event. Referencing a constant at the call site (instead
# of typing the bare string) means a typo becomes an ImportError instead of a
# silently un-searchable log line.
EVENT_SCAN_STARTED = "scan_started"
EVENT_SCAN_COMPLETED = "scan_completed"
EVENT_SCAN_PARTIAL = "scan_partial"
EVENT_SCAN_FAILED = "scan_failed"
# RANK-002 scoring is intentionally non-fatal. This warning event means "the
# shortlist still exists, but ranking metadata could not be produced", which is
# more precise than overloading the terminal scan_failed lifecycle event.
EVENT_SCAN_SCORING_FAILED = "scan_scoring_failed"
EVENT_SYMBOL_SCAN_FAILED = "symbol_scan_failed"
EVENT_DAILY_JOB_STARTED = "daily_job_started"
EVENT_DAILY_JOB_CONFIG_LOADED = "daily_job_config_loaded"
EVENT_DAILY_JOB_CONFIG_INVALID = "daily_job_config_invalid"
EVENT_DAILY_JOB_COMPLETED = "daily_job_completed"
# ALERT-001 daily-scan notification lifecycle events. ``_skipped`` = no channel
# configured (opt-in); ``_sent``/``_failed`` are per-channel and never affect the
# job's exit code.
EVENT_NOTIFICATION_SENT = "notification_sent"
EVENT_NOTIFICATION_FAILED = "notification_failed"
EVENT_NOTIFICATION_SKIPPED = "notification_skipped"
# VALID-004 headless forward-return compute job lifecycle events.
EVENT_FORWARD_RETURNS_JOB_STARTED = "forward_returns_job_started"
EVENT_FORWARD_RETURNS_JOB_COMPLETED = "forward_returns_job_completed"
EVENT_FORWARD_RETURNS_JOB_FAILED = "forward_returns_job_failed"
EVENT_IPO_FILING_SCAN_STARTED = "ipo_filing_scan_started"
EVENT_IPO_FILING_CATEGORY_COMPLETED = "ipo_filing_category_completed"
EVENT_IPO_FILING_CATEGORY_FAILED = "ipo_filing_category_failed"
EVENT_IPO_FILING_SCAN_COMPLETED = "ipo_filing_scan_completed"
EVENT_IPO_DOCUMENT_DOWNLOAD_COMPLETED = "ipo_document_download_completed"
EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED = "ipo_document_download_failed"
EVENT_IPO_MANUAL_EXTRACTION_SUBMITTED = "ipo_manual_extraction_submitted"
# IPO-009 web-enrichment lifecycle. ``_skipped`` = SERPAPI_API_KEY absent (the
# screener stays fully functional); ``_failed`` carries only exception type
# names and counts, never snippet text.
EVENT_IPO_ENRICHMENT_COMPLETED = "ipo_enrichment_completed"
EVENT_IPO_ENRICHMENT_FAILED = "ipo_enrichment_failed"
EVENT_IPO_ENRICHMENT_SKIPPED = "ipo_enrichment_skipped"
# IPO-010 AI extraction-proposal lifecycle. Events carry ids, counts, codes,
# and exception type names only — never proposed values or prospectus text.
EVENT_IPO_EXTRACTION_PROPOSED = "ipo_extraction_proposed"
EVENT_IPO_EXTRACTION_PROPOSAL_FAILED = "ipo_extraction_proposal_failed"
EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED = "ipo_extraction_proposal_reviewed"
EVENT_EXTERNAL_API_FAILED = "external_api_failed"
# DATA-001 candle-quality events. ``_warning`` = a usable frame with suspicious
# data; ``_failed`` = a frame quarantined before scanning. Both log finding
# *codes* only, never raw prices.
EVENT_CANDLE_DATA_QUALITY_WARNING = "candle_data_quality_warning"
EVENT_CANDLE_DATA_QUALITY_FAILED = "candle_data_quality_failed"
EVENT_AUTH_DENIED = "auth_denied"
EVENT_DATA_REFRESH_STARTED = "data_refresh_started"
EVENT_DATA_REFRESH_COMPLETED = "data_refresh_completed"
# OBS-003 audit-trail events. These name the user actions persisted to the
# ``audit_logs`` table (and also emitted here so they appear in normal logs).
# ``login_denied`` is the audit-trail name for a rejected sign-in; the existing
# ``auth_denied`` log event above stays for log-only diagnostics.
EVENT_LOGIN_SUCCESS = "login_success"
EVENT_LOGIN_DENIED = "login_denied"
EVENT_MANUAL_SCAN_STARTED = "manual_scan_started"
EVENT_CONFIG_CHANGED = "config_changed"
EVENT_EXPORT_DOWNLOADED = "export_downloaded"
EVENT_ADMIN_PAGE_ACCESSED = "admin_page_accessed"
# AUTH-003 role-model events. ``role_denied`` records an attempt to use a feature
# above the actor's role (logged AND audited, like ``auth_denied``/``login_denied``);
# ``role_changed`` records an admin assigning or revoking a role (old -> new).
EVENT_ROLE_DENIED = "role_denied"
EVENT_ROLE_CHANGED = "role_changed"

# The custom attributes we attach to each ``logging.LogRecord``. Kept as private
# constants so ``log_event`` and both formatters agree on the exact names. Neither
# collides with a built-in LogRecord attribute (which would make logging raise).
_EVENT_ATTR = "event"
_FIELDS_ATTR = "structured_fields"

# The human-readable format used in development (matches the app's prior format).
_TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"

# ``logging`` accepts either ``True`` (use the exception currently being handled)
# or the explicit three-item tuple returned by ``sys.exc_info()``. The explicit
# tuple lets a service finish persistence before it emits the terminal event while
# still preserving the original, redacted traceback for operators.
ExceptionInfo = tuple[type[BaseException], BaseException, TracebackType]

__all__ = [
    "EVENT_ADMIN_PAGE_ACCESSED",
    "EVENT_AUTH_DENIED",
    "EVENT_CANDLE_DATA_QUALITY_FAILED",
    "EVENT_CANDLE_DATA_QUALITY_WARNING",
    "EVENT_CONFIG_CHANGED",
    "EVENT_DAILY_JOB_COMPLETED",
    "EVENT_DAILY_JOB_CONFIG_INVALID",
    "EVENT_DAILY_JOB_CONFIG_LOADED",
    "EVENT_DAILY_JOB_STARTED",
    "EVENT_DATA_REFRESH_COMPLETED",
    "EVENT_DATA_REFRESH_STARTED",
    "EVENT_EXPORT_DOWNLOADED",
    "EVENT_EXTERNAL_API_FAILED",
    "EVENT_FORWARD_RETURNS_JOB_COMPLETED",
    "EVENT_FORWARD_RETURNS_JOB_FAILED",
    "EVENT_FORWARD_RETURNS_JOB_STARTED",
    "EVENT_IPO_DOCUMENT_DOWNLOAD_COMPLETED",
    "EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED",
    "EVENT_IPO_ENRICHMENT_COMPLETED",
    "EVENT_IPO_ENRICHMENT_FAILED",
    "EVENT_IPO_ENRICHMENT_SKIPPED",
    "EVENT_IPO_EXTRACTION_PROPOSAL_FAILED",
    "EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED",
    "EVENT_IPO_EXTRACTION_PROPOSED",
    "EVENT_IPO_FILING_CATEGORY_COMPLETED",
    "EVENT_IPO_FILING_CATEGORY_FAILED",
    "EVENT_IPO_FILING_SCAN_COMPLETED",
    "EVENT_IPO_FILING_SCAN_STARTED",
    "EVENT_IPO_MANUAL_EXTRACTION_SUBMITTED",
    "EVENT_LOGIN_DENIED",
    "EVENT_LOGIN_SUCCESS",
    "EVENT_MANUAL_SCAN_STARTED",
    "EVENT_NOTIFICATION_FAILED",
    "EVENT_NOTIFICATION_SENT",
    "EVENT_NOTIFICATION_SKIPPED",
    "EVENT_SCAN_COMPLETED",
    "EVENT_SCAN_FAILED",
    "EVENT_SCAN_PARTIAL",
    "EVENT_SCAN_SCORING_FAILED",
    "EVENT_SCAN_STARTED",
    "EVENT_SYMBOL_SCAN_FAILED",
    "ExceptionInfo",
    "JsonEventFormatter",
    "TextEventFormatter",
    "configure_logging",
    "log_event",
]


def log_event(
    logger: logging.Logger,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool | ExceptionInfo | None = False,
    **fields: Any,
) -> None:
    """Emit one named, structured log event.

    Beginner note:
    Think of this as ``logger.info("scan_started")`` but with searchable context
    bolted on. ``event`` is the stable name; ``fields`` are the key/value details
    (for example ``run_id=42, symbol="RELIANCE"``). We attach both to the log
    *record* via ``extra=`` so the formatter can render them from a single source
    of truth - as ``key=value`` pairs in development, or as JSON keys in production.

    Choosing ``level``:
    - routine lifecycle events (``scan_started``/``scan_completed``,
      ``data_refresh_*``) use ``logging.INFO``;
    - failures (``scan_failed``, ``symbol_scan_failed``, ``external_api_failed``,
      ``auth_denied``) use ``logging.WARNING``/``ERROR`` so they remain visible at
      the default ``LOG_LEVEL=WARNING``.

    Set ``exc_info=True`` inside an ``except`` block to capture a traceback; the
    JSON formatter records the exception type and a redacted traceback.
    """
    logger.log(
        level,
        event,
        exc_info=exc_info,
        # These keys become attributes on the LogRecord. The formatters read them
        # back by the same private names.
        extra={_EVENT_ATTR: event, _FIELDS_ATTR: dict(fields)},
    )


class TextEventFormatter(logging.Formatter):
    """Human-readable formatter for development.

    Renders the normal ``time LEVEL logger: message`` line, then appends any
    structured fields as ``key=value`` pairs so a developer watching the terminal
    sees the same context the production JSON logs carry. The whole line is passed
    through ``redact_text`` so a secret in a field can never reach the screen.
    """

    def __init__(self) -> None:
        super().__init__(_TEXT_FORMAT)

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            extras = " ".join(f"{key}={value}" for key, value in fields.items())
            base = f"{base} | {extras}"
        # Redact the fully-rendered line. Fields are appended after the logging
        # filter runs, so redacting here is what guarantees they stay secret-safe.
        return redact_text(base)


class JsonEventFormatter(logging.Formatter):
    """Machine-readable JSON formatter for production.

    Emits exactly one JSON object per log line: timestamp, level, logger name, the
    event name (when present), the human message, and every structured field as a
    top-level key. Exceptions add ``error_type`` and a ``traceback``. The finished
    JSON string is passed through ``redact_text`` as a final safety net, so no
    field value (or traceback) can leak a secret.

    Beginner note:
    This formatter also works for ordinary third-party log records (for example
    SQLAlchemy warnings). Those simply have no ``event``/fields, so they serialize
    as a plain ``{timestamp, level, logger, message}`` object.
    """

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created, tz=UTC
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
        }

        event = getattr(record, _EVENT_ATTR, None)
        if event is not None:
            payload["event"] = event
        payload["message"] = record.getMessage()

        fields = getattr(record, _FIELDS_ATTR, None)
        if fields:
            for key, value in fields.items():
                # Never let a field overwrite a reserved key above; namespace the
                # rare collision rather than silently dropping context.
                payload[key if key not in payload else f"field_{key}"] = value

        if record.exc_info:
            exc_type = record.exc_info[0]
            payload["error_type"] = exc_type.__name__ if exc_type else "Exception"
            payload["traceback"] = self.formatException(record.exc_info)

        serialized = json.dumps(payload, default=str)
        return redact_text(serialized)


def _use_json(settings: AppSettings) -> bool:
    """Decide JSON vs text rendering for the given settings.

    ``LOG_FORMAT=json``/``text`` force one rendering; the default ``auto`` follows
    the environment (JSON in production, readable text in development).
    """
    mode = settings.log_format
    if mode == "json":
        return True
    if mode == "text":
        return False
    return settings.is_production  # "auto"


def configure_logging(
    *,
    settings: AppSettings | None = None,
    extra_secrets: list[str] | None = None,
    stream: TextIO | None = None,
) -> None:
    """Configure root logging once: leveled, secret-safe, JSON in production.

    Beginner note:
    Every entrypoint - the Streamlit app, the CLI prefetch, and the headless
    daily-scan job - calls this instead of ``logging.basicConfig`` so they all log
    the same way. It is *idempotent*: calling it again (for example on each
    Streamlit rerun) refreshes the level and formatter but never stacks duplicate
    handlers.

    Steps:
    1. Pick the formatter - JSON in production, readable text in development
       (``LOG_FORMAT`` overrides via settings).
    2. Ensure the root logger has exactly one stderr handler using that formatter.
    3. Set the level from ``LOG_LEVEL``.
    4. Install SEC-002's redaction filter (plus any ``extra_secrets`` the caller
       knows about, such as Streamlit OIDC cookie values) so nothing secret leaks.
    """
    settings = settings or get_settings()
    formatter: logging.Formatter = (
        JsonEventFormatter() if _use_json(settings) else TextEventFormatter()
    )

    root = logging.getLogger()
    if root.handlers:
        # Another entrypoint already attached handlers (e.g. the CLI prefetch ran
        # before Streamlit took over). Make our formatter authoritative so
        # production still gets JSON, but do not add a second handler.
        for handler in root.handlers:
            handler.setFormatter(formatter)
    else:
        handler = logging.StreamHandler(stream or sys.stderr)
        handler.setFormatter(formatter)
        root.addHandler(handler)

    # get_settings() validates LOG_LEVEL, so getattr is mostly defensive: if Python
    # ever lacks a named level we keep the app quiet at WARNING rather than crash.
    root.setLevel(getattr(logging, settings.log_level, logging.WARNING))
    install_secret_redaction_filter(root, extra_secrets=extra_secrets)
