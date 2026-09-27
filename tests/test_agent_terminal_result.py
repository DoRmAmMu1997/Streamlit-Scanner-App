"""Every Claude Agent SDK runner must require a terminal result before trusting text.

Beginner note:
    An SDK stream can deliver polished JSON in an ``AssistantMessage`` and then
    end (CLI crash, dropped connection) without the terminal ``ResultMessage``
    that confirms the run finished. IPO extraction already rejects that shape;
    these regressions pin the same rule for the Fundamentals, Technical and
    67-ka-Funda runners so an unconfirmed partial answer is never persisted.
"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace
from typing import Any

import pytest

from backend.fundamentals.fundamental_agent import FundamentalAgent, FundamentalsAgentError
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.sixty_seven.agent import SixtySevenAgent
from backend.technical.technical_agent import TechnicalAnalysisAgent


def _install_sdk_without_terminal_result(monkeypatch) -> None:
    """Fake SDK whose stream yields assistant JSON and then simply ends."""

    class ClaudeAgentOptions:
        def __init__(self, **_kwargs: Any) -> None:
            pass

    class ResultMessage:
        pass

    class AssistantMessage:
        def __init__(self, text: str) -> None:
            self.content = [SimpleNamespace(text=text)]

    class CLINotFoundError(Exception):
        pass

    class ProcessError(Exception):
        pass

    async def query(*, prompt: str, options: object):
        del prompt, options
        yield AssistantMessage('{"looks": "complete"}')

    def tool(_name: str, _description: str, _schema: dict[str, type]):
        return lambda function: function

    def create_sdk_mcp_server(*, name: str, version: str, tools: list[object]):
        return {"name": name, "version": version, "tools": tools}

    fake_sdk = types.ModuleType("claude_agent_sdk")
    fake_sdk.__dict__.update({
        "ClaudeAgentOptions": ClaudeAgentOptions,
        "ResultMessage": ResultMessage,
        "AssistantMessage": AssistantMessage,
        "CLINotFoundError": CLINotFoundError,
        "ProcessError": ProcessError,
        "query": query,
        "tool": tool,
        "create_sdk_mcp_server": create_sdk_mcp_server,
    })
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake_sdk)


@pytest.mark.parametrize("agent_class", [FundamentalAgent, TechnicalAnalysisAgent, SixtySevenAgent])
def test_stream_without_terminal_result_is_rejected(monkeypatch, tmp_path, agent_class):
    _install_sdk_without_terminal_result(monkeypatch)
    agent = agent_class(model="test-model", cache=FundamentalsCache(cache_dir=tmp_path))

    with pytest.raises(FundamentalsAgentError, match="terminal result"):
        agent._run_sync(agent._default_run("prompt", system_prompt="system", model="test-model", max_turns=2))


def test_usage_limit_classifier_is_shared_by_every_agent():
    """One marker list: a billing refusal must be recognized everywhere."""
    from backend.agent_usage_limits import mentions_usage_limit
    from backend.fundamentals import fundamental_agent
    from backend.ipo.agents import financial_extractor

    assert mentions_usage_limit("Your credit balance is too low for billing")
    assert fundamental_agent._mentions_usage_limit("billing refused this request")
    assert financial_extractor._mentions_usage_limit is mentions_usage_limit
    assert fundamental_agent._mentions_usage_limit is mentions_usage_limit
