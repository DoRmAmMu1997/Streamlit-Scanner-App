"""Tests for OBS-001 structured logging (``backend.observability``).

What this file proves
---------------------
- Events render as one parseable JSON object in production mode and as readable
  ``key=value`` lines in development.
- ``run_id`` / ``symbol`` context rides along on every event.
- Secrets never survive into a log line (JSON or text), including inside tracebacks.
- ``configure_logging`` picks the right formatter, is idempotent, and installs the
  SEC-002 redaction filter.
- The ``LOG_FORMAT`` setting parses/validates like the other settings.

Beginner note:
These tests are fully offline. They render records through a formatter into an
``io.StringIO`` buffer (the same trick ``tests/test_secret_redaction.py`` uses),
so nothing touches real stderr, the database, or the network.
"""

from __future__ import annotations

import io
import json
import logging

import pytest

from backend.config import get_settings
from backend.config.settings import SettingsError
from backend.observability import (
    EVENT_DAILY_JOB_COMPLETED,
    EVENT_DAILY_JOB_CONFIG_INVALID,
    EVENT_DAILY_JOB_CONFIG_LOADED,
    EVENT_DAILY_JOB_STARTED,
    EVENT_EXTERNAL_API_FAILED,
    EVENT_SCAN_FAILED,
    EVENT_SCAN_PARTIAL,
    EVENT_SCAN_STARTED,
    JsonEventFormatter,
    TextEventFormatter,
    _use_json,
    configure_logging,
    log_event,
)


def test_required_daily_job_event_names_are_stable():
    """The tech-lead event catalog should be importable without string literals.

    Operators and log queries depend on these exact names. Keeping the assertion
    close to the public constants makes an accidental rename fail loudly before
    it silently breaks a production dashboard or alert.
    """
    assert EVENT_DAILY_JOB_STARTED == "daily_job_started"
    assert EVENT_DAILY_JOB_CONFIG_LOADED == "daily_job_config_loaded"
    assert EVENT_DAILY_JOB_CONFIG_INVALID == "daily_job_config_invalid"
    assert EVENT_SCAN_PARTIAL == "scan_partial"
    assert EVENT_DAILY_JOB_COMPLETED == "daily_job_completed"


def _emit(
    formatter: logging.Formatter,
    event: str,
    *,
    level: int = logging.INFO,
    exc_info: bool = False,
    **fields: object,
) -> str:
    """Render one ``log_event`` through ``formatter`` and return the output line.

    Each call uses a private, non-propagating logger so tests stay isolated from
    each other and from the root logger.
    """
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    handler.setFormatter(formatter)
    logger = logging.getLogger(f"obs.test.{id(buffer)}")
    logger.handlers = [handler]
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    log_event(logger, event, level=level, exc_info=exc_info, **fields)
    return buffer.getvalue().strip()


# ---------------------------------------------------------------------------
# JSON formatter (production)
# ---------------------------------------------------------------------------


def test_json_formatter_emits_one_parseable_object_with_event_and_context():
    line = _emit(JsonEventFormatter(), EVENT_SCAN_STARTED, run_id=42, symbol="RELIANCE")

    obj = json.loads(line)  # must be a single, valid JSON object
    assert obj["event"] == "scan_started"
    assert obj["run_id"] == 42
    assert obj["symbol"] == "RELIANCE"
    assert obj["level"] == "INFO"
    assert obj["logger"].startswith("obs.test.")
    assert "timestamp" in obj


def test_json_formatter_redacts_secret_field_values():
    line = _emit(
        JsonEventFormatter(),
        EVENT_EXTERNAL_API_FAILED,
        symbol="X",
        error="Authorization: Bearer sk-LEAKME123",
    )

    assert "sk-LEAKME123" not in line
    json.loads(line)  # still valid JSON after redaction


def test_json_formatter_records_redacted_traceback_on_exception():
    try:
        raise RuntimeError("token=LEAKME boom")
    except RuntimeError:
        line = _emit(
            JsonEventFormatter(),
            EVENT_SCAN_FAILED,
            level=logging.ERROR,
            exc_info=True,
            run_id=1,
        )

    obj = json.loads(line)
    assert obj["error_type"] == "RuntimeError"
    assert "traceback" in obj
    assert "LEAKME" not in line


# ---------------------------------------------------------------------------
# Text formatter (development)
# ---------------------------------------------------------------------------


def test_text_formatter_appends_readable_fields():
    line = _emit(TextEventFormatter(), EVENT_SCAN_STARTED, run_id=42, symbol="RELIANCE")

    assert "scan_started" in line
    assert "run_id=42" in line
    assert "symbol=RELIANCE" in line


def test_text_formatter_redacts_secret_fields():
    line = _emit(
        TextEventFormatter(),
        EVENT_EXTERNAL_API_FAILED,
        error="Authorization: Bearer sk-LEAKME123",
    )

    assert "sk-LEAKME123" not in line


# ---------------------------------------------------------------------------
# JSON-vs-text decision + settings
# ---------------------------------------------------------------------------


def test_use_json_auto_follows_environment():
    assert _use_json(get_settings(env={"APP_ENV": "production"})) is True
    assert _use_json(get_settings(env={})) is False


def test_use_json_explicit_format_overrides_environment():
    assert _use_json(get_settings(env={"LOG_FORMAT": "json"})) is True
    forced_text_prod = get_settings(env={"APP_ENV": "production", "LOG_FORMAT": "text"})
    assert _use_json(forced_text_prod) is False


def test_invalid_log_format_fails_clearly():
    with pytest.raises(SettingsError, match="LOG_FORMAT"):
        get_settings(env={"LOG_FORMAT": "yaml"})


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_root_logger():
    """Snapshot and restore the root logger so these tests don't leak global state."""
    root = logging.getLogger()
    saved_handlers = root.handlers[:]
    saved_level = root.level
    saved_filters = root.filters[:]
    try:
        yield root
    finally:
        root.handlers = saved_handlers
        root.level = saved_level
        root.filters = saved_filters


def test_configure_logging_uses_json_in_production(restore_root_logger):
    root = restore_root_logger
    root.handlers = []
    configure_logging(
        settings=get_settings(env={"APP_ENV": "production", "LOG_LEVEL": "INFO"})
    )

    assert len(root.handlers) == 1
    assert isinstance(root.handlers[0].formatter, JsonEventFormatter)
    assert root.level == logging.INFO


def test_configure_logging_uses_text_in_development(restore_root_logger):
    root = restore_root_logger
    root.handlers = []
    configure_logging(settings=get_settings(env={}))

    assert isinstance(root.handlers[0].formatter, TextEventFormatter)


def test_configure_logging_is_idempotent(restore_root_logger):
    root = restore_root_logger
    root.handlers = []
    settings = get_settings(env={})
    configure_logging(settings=settings)
    configure_logging(settings=settings)

    assert len(root.handlers) == 1  # second call must not stack a duplicate handler


def test_configure_logging_redacts_secrets_end_to_end(restore_root_logger):
    root = restore_root_logger
    root.handlers = []
    root.filters = []
    buffer = io.StringIO()
    configure_logging(
        settings=get_settings(env={"LOG_FORMAT": "json", "LOG_LEVEL": "INFO"}),
        stream=buffer,
    )

    # A child logger propagates to the root handler, where the redaction filter
    # must mask the secret before it is written.
    logging.getLogger("obs.e2e").info("Authorization: Bearer sk-LEAKME999")

    assert "sk-LEAKME999" not in buffer.getvalue()
