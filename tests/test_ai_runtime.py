"""Tests for the shared Claude-agent runtime helpers (REFACTOR-003, AI-006).

``backend.ai_runtime.run_agent_coroutine`` is the one place that bridges sync
(Streamlit) code into the Agent SDK's async world. These tests lock the two
behaviors the three agents depend on and that were historically bug-prone:

1. The caller's ``contextvars`` context must reach the worker thread (TEST-003
   found the fundamentals agent losing its symbol binding and prompt-injection
   evidence collector without this — a fail-open hazard).
2. The coroutine runs on a fresh event loop that supports subprocess transports
   on Windows (the Agent SDK spawns the Claude CLI as a subprocess, which
   Tornado's selector loop cannot do).

``extract_json_object`` (AI-006) is the shared tolerant verdict-JSON extractor
all three agents import under their old private ``_extract_json_object`` name;
its tests below lock the tolerance behaviors the agents rely on.
"""

from __future__ import annotations

import asyncio
import contextvars
import sys

import pytest

from backend.ai_runtime import extract_json_object, run_agent_coroutine

_PROBE: contextvars.ContextVar[str] = contextvars.ContextVar("ai_runtime_probe", default="unset")


def test_returns_coroutine_result_unchanged():
    sentinel = object()

    async def _produce() -> object:
        return sentinel

    assert run_agent_coroutine(_produce()) is sentinel


def test_propagates_caller_contextvars_into_worker_thread():
    """The agents' tools read ContextVars via ``asyncio.to_thread`` workers.

    A freshly-spawned worker thread starts with an EMPTY context; without the
    ``contextvars.copy_context()`` snapshot in the bridge, this reads the
    default ("unset") and the per-call symbol binding silently disappears.
    """
    token = _PROBE.set("bound-by-caller")
    try:

        async def _read_via_thread() -> str:
            return await asyncio.to_thread(_PROBE.get)

        assert run_agent_coroutine(_read_via_thread()) == "bound-by-caller"
    finally:
        _PROBE.reset(token)


def test_propagates_coroutine_exceptions_to_caller():
    async def _explode() -> None:
        raise ValueError("agent failure surfaces unchanged")

    with pytest.raises(ValueError, match="agent failure surfaces unchanged"):
        run_agent_coroutine(_explode())


def test_sequential_calls_each_get_a_working_fresh_loop():
    # The bridge closes its loop after every call; a second call must not
    # trip over the first call's closed loop.
    async def _loop_name() -> str:
        return type(asyncio.get_running_loop()).__name__

    first = run_agent_coroutine(_loop_name())
    second = run_agent_coroutine(_loop_name())

    assert first == second
    if sys.platform == "win32":
        # Subprocess transports (the Claude CLI) need the proactor loop on
        # Windows; Tornado installs the selector policy, so the bridge must
        # build the right loop explicitly rather than inherit the policy.
        assert first == "ProactorEventLoop"


# ---------------------------------------------------------------------------
# extract_json_object (AI-006) — the shared tolerant verdict extractor
# ---------------------------------------------------------------------------


def test_extracts_bare_json_object():
    assert extract_json_object('{"approved": true, "confidence": 8}') == {
        "approved": True,
        "confidence": 8,
    }


def test_extracts_from_json_fence():
    text = 'Here is my verdict:\n```json\n{"rating": "BUY"}\n```\nDone.'
    assert extract_json_object(text) == {"rating": "BUY"}


def test_extracts_from_fence_without_language_tag():
    assert extract_json_object('```\n{"rating": "SELL"}\n```') == {"rating": "SELL"}


def test_extracts_outermost_span_despite_surrounding_prose():
    text = 'Sure! The verdict is {"rating": "HOLD", "nested": {"depth": 2}} — hope that helps.'
    assert extract_json_object(text) == {"rating": "HOLD", "nested": {"depth": 2}}


def test_returns_none_for_empty_text():
    assert extract_json_object("") is None


def test_returns_none_when_no_braces_present():
    assert extract_json_object("The model rambled and produced no JSON at all.") is None


def test_returns_none_when_braces_do_not_parse():
    assert extract_json_object("{not: valid json}") is None


@pytest.mark.parametrize("constant", ["NaN", "Infinity", "-Infinity"])
def test_returns_none_for_non_finite_json_numbers(constant):
    """Model-only numeric extensions must not enter strict cache payloads.

    Beginner note:
    Python's JSON decoder accepts these JavaScript-style constants by default,
    even though the JSON standard does not.  Returning ``None`` here makes the
    normal agent retry path handle them like any other malformed response.
    """
    assert extract_json_object(f'{{"confidence": {constant}}}') is None


def test_returns_none_when_json_nesting_exceeds_decoder_limit():
    """An excessively nested model response is a parse miss, not an app crash."""
    deeply_nested = '{"value":' + "[" * 5_000 + "0" + "]" * 5_000 + "}"

    assert extract_json_object(deeply_nested) is None


def test_returns_none_for_reversed_braces():
    # rfind("}") lands BEFORE find("{"): the span is invalid, not an exception.
    assert extract_json_object("} stray close then open {") is None
