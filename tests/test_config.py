"""Tests for environment-backed scanner configuration helpers."""

from __future__ import annotations

from backend.config import (
    dhan_rate_limit_retry_delays,
    dhan_request_delay_seconds,
    get_agent_fast_mode,
    get_ai_max_attempts,
    secret_values,
)


def test_dhan_throttle_config_uses_safe_defaults(monkeypatch):
    # Defaults should be conservative enough for first runs when .env does not
    # specify custom Dhan throttling values.
    monkeypatch.delenv("SCANNER_DHAN_REQUEST_DELAY_SECONDS", raising=False)
    monkeypatch.delenv("SCANNER_DHAN_RATE_LIMIT_RETRY_DELAYS", raising=False)

    assert dhan_request_delay_seconds() == 0.5
    assert dhan_rate_limit_retry_delays() == [2.0, 5.0, 10.0]


def test_dhan_throttle_config_falls_back_for_invalid_values(monkeypatch):
    # Bad local .env edits should not accidentally disable the safety throttle.
    monkeypatch.setenv("SCANNER_DHAN_REQUEST_DELAY_SECONDS", "not-a-number")
    monkeypatch.setenv("SCANNER_DHAN_RATE_LIMIT_RETRY_DELAYS", "2,bad,10")

    assert dhan_request_delay_seconds() == 0.5
    assert dhan_rate_limit_retry_delays() == [2.0, 5.0, 10.0]


def test_dhan_throttle_config_accepts_valid_values(monkeypatch):
    monkeypatch.setenv("SCANNER_DHAN_REQUEST_DELAY_SECONDS", "1.25")
    monkeypatch.setenv("SCANNER_DHAN_RATE_LIMIT_RETRY_DELAYS", "3, 6.5")

    assert dhan_request_delay_seconds() == 1.25
    assert dhan_rate_limit_retry_delays() == [3.0, 6.5]


def test_agent_fast_mode_defaults_off(monkeypatch):
    # Unset → thorough (current) behavior is the default.
    monkeypatch.delenv("SCANNER_AGENT_FAST_MODE", raising=False)
    assert get_agent_fast_mode() is False


def test_agent_fast_mode_accepts_truthy_values(monkeypatch):
    for value in ("1", "true", "TRUE", "yes", "On"):
        monkeypatch.setenv("SCANNER_AGENT_FAST_MODE", value)
        assert get_agent_fast_mode() is True, value


def test_agent_fast_mode_rejects_other_values(monkeypatch):
    for value in ("0", "false", "no", "", "maybe"):
        monkeypatch.setenv("SCANNER_AGENT_FAST_MODE", value)
        assert get_agent_fast_mode() is False, value


def test_ai_max_attempts_defaults_to_one_retry(monkeypatch):
    # Unset → 2 total tries (one retry): bounded + cost-aware (AI-004).
    monkeypatch.delenv("SCANNER_AI_MAX_ATTEMPTS", raising=False)
    assert get_ai_max_attempts() == 2


def test_ai_max_attempts_falls_back_for_invalid_values(monkeypatch):
    # A bad .env edit must not disable retry or make it unbounded.
    for value in ("not-a-number", "", "  ", "1.5"):
        monkeypatch.setenv("SCANNER_AI_MAX_ATTEMPTS", value)
        assert get_ai_max_attempts() == 2, value


def test_ai_max_attempts_is_clamped_to_one_through_three(monkeypatch):
    # At least one try; capped at three so a typo can't burn Agent SDK credit.
    for raw, expected in (("1", 1), ("2", 2), ("3", 3), ("4", 3), ("0", 1), ("-5", 1)):
        monkeypatch.setenv("SCANNER_AI_MAX_ATTEMPTS", raw)
        assert get_ai_max_attempts() == expected, raw


def test_secret_values_include_ai_cache_signing_key(monkeypatch):
    monkeypatch.setenv(
        "SCANNER_AI_CACHE_SIGNING_KEY",
        "cache-signing-secret-value",
    )

    assert "cache-signing-secret-value" in secret_values()
