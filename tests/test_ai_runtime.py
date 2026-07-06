"""Tests for the shared Claude-agent sync bridge (REFACTOR-003).

``backend.ai_runtime.run_agent_coroutine`` is the one place that bridges sync
(Streamlit) code into the Agent SDK's async world. These tests lock the two
behaviors the three agents depend on and that were historically bug-prone:

1. The caller's ``contextvars`` context must reach the worker thread (TEST-003
   found the fundamentals agent losing its symbol binding and prompt-injection
   evidence collector without this — a fail-open hazard).
2. The coroutine runs on a fresh event loop that supports subprocess transports
   on Windows (the Agent SDK spawns the Claude CLI as a subprocess, which
   Tornado's selector loop cannot do).
"""

from __future__ import annotations

import asyncio
import contextvars
import sys

import pytest

from backend.ai_runtime import run_agent_coroutine

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
