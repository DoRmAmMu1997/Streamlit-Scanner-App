"""Tests for the Claude Agent SDK technical-analysis agent.

The agentic loop is replaced with a tiny in-process fake `runner` so no live
Claude call (and no CLI subprocess) is made. The fake returns a canned
TechnicalVerdict JSON string — exactly what the real agent's final message
would contain. This mirrors `tests/test_fundamental_agent.py`.
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from backend.fundamentals.fundamental_agent import AgentRunResult
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.technical.technical_agent import (
    TechnicalAnalysisAgent,
    TechnicalVerdict,
)
from backend.technical.tools import SERVER_NAME, TOOL_NAMES, TechnicalToolContext


def _sample_candles(periods: int = 40) -> pd.DataFrame:
    """A small OHLC frame with a `timestamp` column the agent reads for dates."""
    close = [100.0 + i for i in range(periods)]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=periods, freq="D"),
            "open": close,
            "high": [c + 1.0 for c in close],
            "low": [c - 1.0 for c in close],
            "close": close,
            "volume": [1000.0] * periods,
        }
    )


def _sample_levels() -> list[dict]:
    return [
        {"price": 95.0, "touches": 4, "kind": "support"},
        {"price": 140.0, "touches": 3, "kind": "resistance"},
    ]


def _sample_verdict(**overrides) -> TechnicalVerdict:
    base = dict(
        symbol="DEMO",
        pattern="at_support",
        confirmed=True,
        key_levels=[95.0],
        confidence=7,
        reasoning="Price declined into the 95 major support and is basing.",
        signal_date="2026-02-09",
        model_used="test-model",
    )
    base.update(overrides)
    return TechnicalVerdict(**base)


class _FakeRunner:
    """Stand-in for the Claude Agent SDK runner used by TechnicalAnalysisAgent.

    `TechnicalAnalysisAgent.analyze` awaits `runner(prompt, system_prompt=,
    model=, max_turns=)` and parses the returned `AgentRunResult.text` as the
    final TechnicalVerdict JSON. This fake records its calls and returns the
    canned verdict as a JSON string.
    """

    def __init__(self, verdict: TechnicalVerdict, *, cost_usd: float | None = 0.01):
        self.verdict = verdict
        self.cost_usd = cost_usd
        self.calls = 0
        self.last_prompt: str | None = None
        self.last_system_prompt: str | None = None
        self.last_model: str | None = None
        self.last_tool_context: object | None = None

    async def __call__(
        self,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        max_turns: int,
        tool_context: object | None = None,
    ) -> AgentRunResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        self.last_model = model
        self.last_tool_context = tool_context
        return AgentRunResult(
            text=json.dumps(self.verdict.model_dump(mode="json")),
            cost_usd=self.cost_usd,
        )


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_technical_verdict_rejects_confidence_outside_zero_to_ten():
    with pytest.raises(Exception):
        TechnicalVerdict(symbol="DEMO", pattern="none", confidence=11, reasoning="x")


def test_technical_verdict_json_schema_omits_min_max_on_confidence():
    """Regression guard: Claude rejects `minimum`/`maximum` on integers.

    If a future change re-adds `Field(ge=..., le=...)` on `confidence`, this
    catches it before it reaches the model.
    """
    schema = TechnicalVerdict.model_json_schema()
    conf = schema["properties"]["confidence"]
    assert conf["type"] == "integer"
    assert "minimum" not in conf
    assert "maximum" not in conf


# ---------------------------------------------------------------------------
# Agent behaviour (driven by the fake runner)
# ---------------------------------------------------------------------------


def test_agent_returns_parsed_verdict(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.analyze("DEMO", _sample_candles(), _sample_levels())

    assert verdict.symbol == "DEMO"
    assert verdict.pattern == "at_support"
    assert verdict.confirmed is True
    assert runner.calls == 1
    # The runner was handed the configured model and the strict-output prompt.
    assert runner.last_model == "test-model"
    assert "FINAL OUTPUT FORMAT" in (runner.last_system_prompt or "")


def test_agent_passes_per_call_tool_context_to_runner(tmp_path):
    # The agent must build a TechnicalToolContext for THIS stock and hand it to
    # the runner, so the tools (and parallel confirmations) work on the right data.
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    agent.analyze("DEMO", _sample_candles(), _sample_levels())

    ctx = runner.last_tool_context
    assert isinstance(ctx, TechnicalToolContext)
    assert ctx.symbol == "DEMO"
    # The levels passed in were relevance-scored on the way into the context.
    assert ctx.daily_levels and "relevance" in ctx.daily_levels[0]


def test_agent_round_trips_extended_verdict_fields(tmp_path):
    # The new structured fields (trend, htf_alignment, relevant_levels, caution)
    # and a new bullish pattern must survive the JSON round-trip unchanged.
    cache = FundamentalsCache(cache_dir=tmp_path)
    verdict = _sample_verdict(
        pattern="double_bottom",
        confirmed=True,
        trend="uptrend",
        htf_alignment="aligned",
        relevant_levels=[
            {"price": 95.0, "role": "support", "relevance": "high", "why": "fresh bounce zone"}
        ],
        caution="overhead resistance near 140",
    )
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=_FakeRunner(verdict))

    out = agent.analyze("DEMO", _sample_candles(), _sample_levels())

    assert out.pattern == "double_bottom"
    assert out.trend == "uptrend"
    assert out.htf_alignment == "aligned"
    assert out.caution == "overhead resistance near 140"
    assert out.relevant_levels[0].price == 95.0
    assert out.relevant_levels[0].relevance == "high"


def test_agent_caches_verdict_per_symbol_model_candle_date(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()

    # First call writes to disk under the "::technical" key + latest candle date.
    levels = _sample_levels()
    agent.analyze("DEMO", candles, levels)
    latest_date = str(candles.iloc[-1]["timestamp"])[:10]
    assert cache.get_verdict("DEMO", agent._cache_model_key(candles, levels), latest_date) is not None

    # Second call on the same candle date must hit the cache, not the runner.
    runner.calls = 0
    agent.analyze("DEMO", candles, levels)
    assert runner.calls == 0


def test_agent_cache_changes_when_levels_change_on_same_candle_date(tmp_path):
    """Same symbol/date but different technical context must not reuse a verdict.

    Major levels are controlled by user-tunable parameters. If those levels
    change, the old AI verdict can describe a different chart setup, so the
    cache key needs a stable hash of the prompt inputs.
    """
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()

    agent.analyze("DEMO", candles, _sample_levels())
    runner.calls = 0
    changed_levels = [
        {"price": 101.0, "touches": 6, "kind": "support"},
        {"price": 155.0, "touches": 4, "kind": "resistance"},
    ]
    agent.analyze("DEMO", candles, changed_levels)

    assert runner.calls == 1


def test_agent_cache_changes_between_fast_and_thorough_modes(tmp_path):
    """Fast and thorough technical verdicts should not share one cache entry.

    Both modes analyze the same chart facts, but fast mode changes the model's
    reasoning budget. Keeping the cache namespace separate prevents an opt-in
    fast verdict from being shown later as a thorough verdict.
    """
    cache = FundamentalsCache(cache_dir=tmp_path)
    candles = _sample_candles()
    levels = _sample_levels()

    thorough_runner = _FakeRunner(_sample_verdict(confidence=9))
    thorough_agent = TechnicalAnalysisAgent(
        model="test-model",
        cache=cache,
        runner=thorough_runner,
        fast_mode=False,
    )
    assert thorough_agent.analyze("DEMO", candles, levels).confidence == 9

    fast_runner = _FakeRunner(_sample_verdict(confidence=3))
    fast_agent = TechnicalAnalysisAgent(
        model="test-model",
        cache=cache,
        runner=fast_runner,
        fast_mode=True,
    )
    assert fast_agent.analyze("DEMO", candles, levels).confidence == 3
    assert fast_runner.calls == 1

    thorough_runner.calls = 0
    assert thorough_agent.analyze("DEMO", candles, levels).confidence == 9
    assert thorough_runner.calls == 0


def test_agent_force_refresh_bypasses_cache(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()

    agent.analyze("DEMO", candles, _sample_levels())
    runner.calls = 0
    agent.analyze("DEMO", candles, _sample_levels(), force_refresh=True)
    assert runner.calls == 1


def test_agent_parses_verdict_from_fenced_json(tmp_path):
    # Real models sometimes wrap the JSON in a ```json fence; the extractor
    # must still recover it.
    cache = FundamentalsCache(cache_dir=tmp_path)

    class _FencedRunner(_FakeRunner):
        async def __call__(self, prompt, *, system_prompt, model, max_turns, tool_context=None):
            self.calls += 1
            payload = json.dumps(self.verdict.model_dump(mode="json"))
            return AgentRunResult(text=f"Here is my analysis:\n```json\n{payload}\n```")

    runner = _FencedRunner(_sample_verdict(pattern="cup_and_handle", confirmed=True))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.analyze("DEMO", _sample_candles(), _sample_levels())
    assert verdict.pattern == "cup_and_handle"
    assert verdict.confirmed is True


def test_agent_raises_when_no_json_in_final_message(tmp_path):
    from backend.fundamentals.fundamental_agent import FundamentalsAgentError

    cache = FundamentalsCache(cache_dir=tmp_path)

    class _ProseRunner(_FakeRunner):
        async def __call__(self, prompt, *, system_prompt, model, max_turns, tool_context=None):
            self.calls += 1
            return AgentRunResult(text="I could not determine a pattern.")

    agent = TechnicalAnalysisAgent(
        model="test-model", cache=cache, runner=_ProseRunner(_sample_verdict())
    )
    with pytest.raises(FundamentalsAgentError):
        agent.analyze("DEMO", _sample_candles(), _sample_levels())


def test_agent_demotes_confirmed_when_pattern_is_none(tmp_path):
    # Invariant: a "none" verdict can never be a confirmed signal, even if the
    # model mistakenly set confirmed=True.
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict(pattern="none", confirmed=True, key_levels=[]))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.analyze("DEMO", _sample_candles(), _sample_levels())
    assert verdict.pattern == "none"
    assert verdict.confirmed is False


def test_agent_stamps_blank_symbol_model_and_date(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    partial = _sample_verdict(symbol="", model_used="", signal_date="")
    agent = TechnicalAnalysisAgent(
        model="test-model", cache=cache, runner=_FakeRunner(partial)
    )

    verdict = agent.analyze("demo", _sample_candles(), _sample_levels())
    assert verdict.symbol == "DEMO"
    assert verdict.model_used == "test-model"
    # signal_date is stamped from the latest candle (2026-01-01 + 39 days).
    assert verdict.signal_date == "2026-02-09"


def test_agent_overrides_mismatched_symbol_from_model(tmp_path):
    # The tools and prompt are locked to the requested symbol. If the model still
    # emits a different ticker, treat that as bookkeeping drift and stamp the
    # trusted request symbol so downstream cache/UI data cannot drift.
    cache = FundamentalsCache(cache_dir=tmp_path)
    mismatched = _sample_verdict(symbol="OTHER")
    agent = TechnicalAnalysisAgent(
        model="test-model", cache=cache, runner=_FakeRunner(mismatched)
    )

    verdict = agent.analyze("demo", _sample_candles(), _sample_levels())

    assert verdict.symbol == "DEMO"


def test_agent_requires_model():
    with pytest.raises(ValueError):
        TechnicalAnalysisAgent(model="")


# ---------------------------------------------------------------------------
# Fast mode — thinking is disabled in the real SDK options only when requested
# ---------------------------------------------------------------------------


def _install_fake_sdk(monkeypatch, *, include_thinking: bool = True):
    """Install a fake `claude_agent_sdk` that records ClaudeAgentOptions kwargs.

    `_default_run` imports the SDK lazily and immediately runs `query(...)`. The
    fake's `query` is an async generator yielding one ResultMessage carrying a
    canned TechnicalVerdict JSON, so the agent's real option-construction path
    runs end-to-end without a live CLI. Returns a dict the test inspects.
    """
    import sys
    import types

    captured: dict = {}

    class ThinkingConfigDisabled:
        pass

    class ClaudeAgentOptions:
        def __init__(self, **kwargs):
            captured["options"] = kwargs

    class ResultMessage:
        def __init__(self, result):
            self.result = result
            self.total_cost_usd = None
            self.is_error = False

    class AssistantMessage:  # referenced by isinstance checks in _default_run
        pass

    class CLINotFoundError(Exception):
        pass

    class ProcessError(Exception):
        pass

    verdict_json = json.dumps(_sample_verdict().model_dump(mode="json"))

    async def query(*, prompt, options):
        yield ResultMessage(verdict_json)

    # The agent now builds an in-process MCP tool server, so the fake SDK must
    # expose `tool` (a decorator) and `create_sdk_mcp_server`. The tools are never
    # actually invoked here — `query` returns the final JSON immediately — so
    # trivial stand-ins are enough.
    def tool(name, description, schema):
        def _decorator(fn):
            return fn

        return _decorator

    def create_sdk_mcp_server(*, name, version, tools):
        return {"name": name, "version": version, "tools": tools}

    fake = types.ModuleType("claude_agent_sdk")
    fake.ClaudeAgentOptions = ClaudeAgentOptions
    if include_thinking:
        fake.ThinkingConfigDisabled = ThinkingConfigDisabled
    fake.ResultMessage = ResultMessage
    fake.AssistantMessage = AssistantMessage
    fake.CLINotFoundError = CLINotFoundError
    fake.ProcessError = ProcessError
    fake.query = query
    fake.tool = tool
    fake.create_sdk_mcp_server = create_sdk_mcp_server
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", fake)
    return captured, ThinkingConfigDisabled


def test_default_run_sets_thinking_only_in_fast_mode(monkeypatch, tmp_path):
    captured, ThinkingConfigDisabled = _install_fake_sdk(monkeypatch)
    # Isolated cache + force_refresh so `_default_run` always runs (a cache hit
    # would short-circuit before options are ever constructed).
    cache = FundamentalsCache(cache_dir=tmp_path)

    # Fast mode ON → options carry a ThinkingConfigDisabled instance.
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, fast_mode=True)
    agent.analyze("DEMO", _sample_candles(), _sample_levels(), force_refresh=True)
    assert isinstance(captured["options"].get("thinking"), ThinkingConfigDisabled)

    # Fast mode OFF → thinking is None (SDK default extended thinking).
    captured.clear()
    agent_off = TechnicalAnalysisAgent(model="test-model", cache=cache, fast_mode=False)
    agent_off.analyze("DEMO", _sample_candles(), _sample_levels(), force_refresh=True)
    assert captured["options"].get("thinking") is None


def test_default_run_tolerates_sdk_without_thinking_config(monkeypatch, tmp_path, caplog):
    """Older Claude Agent SDK builds may not expose ThinkingConfigDisabled.

    Non-fast mode should still run normally. Fast mode should degrade to the SDK
    default thinking behavior with a clear log message instead of failing at
    import time.
    """
    captured, _ = _install_fake_sdk(monkeypatch, include_thinking=False)
    cache = FundamentalsCache(cache_dir=tmp_path)

    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, fast_mode=False)
    assert agent.analyze("DEMO", _sample_candles(), _sample_levels(), force_refresh=True).symbol == "DEMO"
    assert captured["options"].get("thinking") is None

    captured.clear()
    fast_agent = TechnicalAnalysisAgent(model="test-model", cache=cache, fast_mode=True)
    fast_agent.analyze("DEMO", _sample_candles(), _sample_levels(), force_refresh=True)

    assert captured["options"].get("thinking") is None
    assert "fast mode" in caplog.text.lower()


def test_default_run_registers_only_the_technical_tools(monkeypatch, tmp_path):
    """The real option-construction path must expose exactly our three tools.

    With permission_mode="dontAsk" the agent can ONLY call tools listed in
    allowed_tools, so this also confirms the built-in filesystem/bash tools stay
    out of reach in a headless run.
    """
    captured, _ = _install_fake_sdk(monkeypatch)
    cache = FundamentalsCache(cache_dir=tmp_path)
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache)

    agent.analyze("DEMO", _sample_candles(), _sample_levels(), force_refresh=True)

    options = captured["options"]
    assert SERVER_NAME in options["mcp_servers"]
    assert options["allowed_tools"] == TOOL_NAMES
    assert options["permission_mode"] == "dontAsk"
    assert options["setting_sources"] == []
