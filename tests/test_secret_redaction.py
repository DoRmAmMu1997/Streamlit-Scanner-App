"""Tests for the shared secret-redaction utility.

These tests intentionally use loud fake secrets such as ``broker-token-secret``.
That makes regressions easy to spot while keeping the test suite safe to run in
CI, on a laptop, or in PR logs.
"""

from __future__ import annotations

import io
import logging

from backend.security.redaction import (
    MASK,
    SecretRedactionFilter,
    install_secret_redaction_filter,
    redact_exception,
    redact_text,
)


def test_redact_text_masks_configured_settings_secrets(monkeypatch):
    """Secrets loaded from DEPLOY-004 settings should never be echoed back."""
    # monkeypatch.setenv keeps the test isolated from the developer machine. If
    # someone has real Dhan/SerpAPI values in their shell, these fake values win
    # for the duration of this test and disappear afterward.
    monkeypatch.setenv("DATABASE_URL", "postgresql://scanner:db-secret@db/scanner")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-secret")
    monkeypatch.setenv("DHAN_CLIENT_ID", "client-secret")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "broker-token-secret")

    redacted = redact_text(
        "postgresql://scanner:db-secret@db/scanner anthropic-secret "
        "serp-secret client-secret broker-token-secret still-visible"
    )

    for secret in (
        "db-secret",
        "anthropic-secret",
        "serp-secret",
        "client-secret",
        "broker-token-secret",
    ):
        assert secret not in redacted
    assert "still-visible" in redacted
    assert redacted.count(MASK) >= 5


def test_redact_text_masks_extra_secrets_for_streamlit_auth():
    """Callers can add secrets that live outside process environment settings."""
    redacted = redact_text(
        "cookie-secret google-client google-secret still-visible",
        extra_secrets=["cookie-secret", "google-client", "google-secret"],
    )

    assert "cookie-secret" not in redacted
    assert "google-client" not in redacted
    assert "google-secret" not in redacted
    assert "still-visible" in redacted


def test_redact_text_masks_common_secret_formats_without_hiding_normal_errors():
    """Pattern masking catches secrets we do not already know from settings."""
    # This raw string intentionally mixes several shapes from real error text:
    # URL query params, HTTP auth headers, database URLs, and plain key/value
    # pairs. The final "Invalid API key" phrase is the negative control: it is
    # useful operator text and should not be hidden just because it says "API".
    raw = (
        "api_key=api-key-secret "
        "access_token=access-token-secret "
        "token=token-secret "
        "client_secret=client-secret "
        "password=password-secret "
        "Authorization: Bearer bearer-secret "
        "https://example.test/search?q=demo&api_key=query-secret&safe=yes "
        "postgresql://scanner:db-url-secret@db/scanner "
        "Invalid API key"
    )

    redacted = redact_text(raw)

    for secret in (
        "api-key-secret",
        "access-token-secret",
        "token-secret",
        "client-secret",
        "password-secret",
        "bearer-secret",
        "query-secret",
        "db-url-secret",
    ):
        assert secret not in redacted
    assert "Invalid API key" in redacted
    assert "safe=yes" in redacted
    assert redacted.count(MASK) >= 8


def test_redact_exception_keeps_type_but_removes_raw_message_secret():
    """Exception summaries are useful only if they identify the kind of failure."""
    exc = RuntimeError("token=broker-token-secret")

    summary = redact_exception(exc)

    assert "RuntimeError" in summary
    assert "broker-token-secret" not in summary
    assert MASK in summary


def test_secret_redaction_filter_masks_messages_args_and_tracebacks():
    """The logging filter is the last safety net before text reaches a sink."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s:%(message)s"))

    # Use a private logger instead of root logging so this test cannot affect
    # other tests. Clearing handlers/filters makes the setup deterministic.
    logger = logging.getLogger("tests.secret_redaction")
    logger.handlers = [handler]
    logger.filters = []
    logger.propagate = False
    logger.setLevel(logging.INFO)
    install_secret_redaction_filter(logger)

    # The first log exercises format-string arguments. The second exercises
    # exception tracebacks, where the secret lives inside the exception text.
    logger.warning("request failed with api_key=%s", "argument-secret")
    try:
        raise RuntimeError("Authorization: Bearer traceback-secret")
    except RuntimeError:
        logger.exception("scanner failed with password=%s", "message-secret")

    output = stream.getvalue()

    assert isinstance(handler.filters[0], SecretRedactionFilter)
    for secret in ("argument-secret", "traceback-secret", "message-secret"):
        assert secret not in output
    assert output.count(MASK) >= 3
    assert "RuntimeError" in output
