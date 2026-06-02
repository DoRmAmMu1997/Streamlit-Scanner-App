"""Tests for environment-backed scanner configuration helpers."""

from __future__ import annotations

from backend.config import dhan_rate_limit_retry_delays, dhan_request_delay_seconds


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
