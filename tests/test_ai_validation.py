"""Tests for the AI-004 bounded validation-retry helper (`backend.ai_validation`).

These exercise the helper in isolation (no SDK): a small ``run_once`` returns
canned text and ``parse_once`` decides whether it is "valid". The four behaviours
that matter: succeed first try, retry then succeed, exhaust into a single
``AIValidationError``, and never retry a failure that came from ``run_once``
(an SDK / usage-limit error) or a parse error outside ``retry_on``.
"""

from __future__ import annotations

import traceback

import pytest

from backend.ai_validation import AIValidationError, parse_with_retry


class _Recorder:
    """Counts how many times run_once / parse_once were invoked."""

    def __init__(self, texts: list[str]) -> None:
        self._texts = texts
        self.run_calls = 0
        self.parse_calls = 0

    def run_once(self) -> str:
        text = self._texts[min(self.run_calls, len(self._texts) - 1)]
        self.run_calls += 1
        return text


def test_returns_first_parse_without_retrying():
    rec = _Recorder(["good"])

    def parse(text: str) -> str:
        rec.parse_calls += 1
        return text.upper()

    result = parse_with_retry(
        rec.run_once, parse, attempts=2, retry_on=(ValueError,)
    )

    assert result == "GOOD"
    assert rec.run_calls == 1
    assert rec.parse_calls == 1


def test_retries_then_succeeds():
    # First text is rejected, second parses — exactly one retry is used.
    rec = _Recorder(["bad", "good"])

    def parse(text: str) -> str:
        rec.parse_calls += 1
        if text != "good":
            raise ValueError("not good yet")
        return text

    result = parse_with_retry(
        rec.run_once, parse, attempts=2, retry_on=(ValueError,)
    )

    assert result == "good"
    assert rec.run_calls == 2
    assert rec.parse_calls == 2


def test_exhausts_into_a_sanitized_ai_validation_error():
    rec = _Recorder(["always-bad"])
    marker = "UNTRUSTED_MARKDOWN_[click](https://attacker.example/leak)"

    def parse(text: str) -> str:
        rec.parse_calls += 1
        raise ValueError(f"still malformed: {marker}")

    with pytest.raises(AIValidationError) as excinfo:
        parse_with_retry(rec.run_once, parse, attempts=3, retry_on=(ValueError,))

    assert rec.run_calls == 3
    assert rec.parse_calls == 3
    assert excinfo.value.attempts == 3
    assert excinfo.value.last_error_type == "ValueError"
    assert str(excinfo.value) == (
        "AI output failed strict validation after 3 attempts "
        "(last error: ValueError)."
    )
    assert marker not in str(excinfo.value)
    assert excinfo.value.__cause__ is None
    assert marker not in "".join(traceback.format_exception(excinfo.value))


def test_does_not_retry_errors_raised_by_run_once():
    # An SDK / CLI / usage-limit failure surfaces from run_once, which is OUTSIDE
    # the retry guard: it propagates unwrapped and is tried exactly once.
    class _UsageLimit(RuntimeError):
        pass

    calls = {"run": 0, "parse": 0}

    def run_once() -> str:
        calls["run"] += 1
        raise _UsageLimit("plan limit hit")

    def parse(text: str) -> str:  # pragma: no cover - never reached
        calls["parse"] += 1
        return text

    with pytest.raises(_UsageLimit):
        parse_with_retry(run_once, parse, attempts=3, retry_on=(ValueError,))

    assert calls["run"] == 1
    assert calls["parse"] == 0


def test_does_not_wrap_parse_errors_outside_retry_on():
    # A parse error not listed in retry_on is a programming/contract error, not
    # malformed output — it must propagate unwrapped and without a retry.
    rec = _Recorder(["x"])

    def parse(text: str) -> str:
        rec.parse_calls += 1
        raise KeyError("unexpected shape")

    with pytest.raises(KeyError):
        parse_with_retry(rec.run_once, parse, attempts=3, retry_on=(ValueError,))

    assert rec.run_calls == 1
    assert rec.parse_calls == 1


def test_attempts_below_one_still_runs_once():
    rec = _Recorder(["good"])

    result = parse_with_retry(
        rec.run_once, lambda text: text, attempts=0, retry_on=(ValueError,)
    )

    assert result == "good"
    assert rec.run_calls == 1


def test_ai_validation_error_is_a_runtime_error():
    # Subclassing RuntimeError keeps it catchable by the agents' broad handlers
    # and by `pytest.raises(RuntimeError)` callers, without an import cycle.
    assert issubclass(AIValidationError, RuntimeError)
