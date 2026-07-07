"""Shared sync→async bridge for the Claude-agent subsystems (REFACTOR-003).

The fundamentals, technical, and 67-Ka-Funda agents each need to run one Agent
SDK coroutine to completion from synchronous (Streamlit) code. All three used
to carry a private copy of the same bridge; the copies drifted once (the
technical agent's lost the context fix below), so the logic now lives here and
the agents delegate. Design rationale and the options weighed are in the ADR:
``docs/architecture/refactor-003-ai-runtime.md``.

Two subtleties are handled, both easy to get wrong:

1. Context propagation (beginner note). An agent's ``check()``/``verify()``
   entry point stashes per-call state — the bound symbol, the force-refresh
   flag, the prompt-injection evidence collector — in module-level
   ``ContextVar``s on the CALLER's thread. A freshly-spawned worker thread
   starts with an EMPTY context, so we snapshot the caller's context with
   ``contextvars.copy_context()`` and run the worker *inside* it
   (``ctx.run(...)``). The tools' ``asyncio.to_thread(...)`` calls then inherit
   those values instead of silently reading the ContextVar defaults — which
   would defeat symbol binding and leave the evidence collector empty (so a
   prompt injection could never fail the check closed; TEST-003 hit exactly
   this). Copying is unconditional: for an agent with no context-bound tools it
   is a harmless no-op, and a future tool inherits the safe behavior for free.

2. Windows event loop. The Agent SDK launches the Claude CLI as a subprocess,
   and only ``ProactorEventLoop`` supports subprocess transports on Windows.
   Streamlit/Tornado install the selector loop policy, and ``asyncio.run()``
   would inherit it and raise ``NotImplementedError`` — so we build the right
   loop explicitly, in a dedicated worker thread with its OWN event loop, and
   never collide with Streamlit/Tornado's running loop.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextvars
import sys
from collections.abc import Awaitable
from typing import TypeVar

T = TypeVar("T")


def run_agent_coroutine(coro: Awaitable[T]) -> T:
    """Run one agent coroutine to completion from sync code and return its result.

    Exceptions raised inside the coroutine propagate unchanged to the caller,
    so each agent's own error taxonomy (usage-limit, parse, evidence errors)
    keeps working exactly as before the extraction.
    """
    # Snapshot on the CALLER thread, where the agent entry point just set its
    # ContextVars (see module docstring, subtlety 1).
    ctx = contextvars.copy_context()

    def _runner() -> T:
        if sys.platform == "win32":
            # ProactorEventLoop supports subprocess transports on Windows;
            # the default selector loop (installed by Tornado) does not.
            loop = asyncio.ProactorEventLoop()
        else:
            loop = asyncio.new_event_loop()
        try:
            asyncio.set_event_loop(loop)
            return loop.run_until_complete(coro)
        finally:
            asyncio.set_event_loop(None)
            loop.close()

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
        # Run the worker INSIDE the captured context so the ContextVars cross
        # the thread boundary.
        return executor.submit(ctx.run, _runner).result()
