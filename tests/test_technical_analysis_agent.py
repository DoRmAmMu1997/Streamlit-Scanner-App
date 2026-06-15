"""Tests for the Claude Agent SDK technical-analysis agent.

The agentic loop is replaced with a tiny in-process fake `runner` so no live
Claude call (and no CLI subprocess) is made. The fake returns a canned
TechnicalVerdict JSON string — exactly what the real agent's final message
would contain. This mirrors `tests/test_fundamental_agent.py`.
"""

from __future__ import annotations

import hashlib
import json

import pandas as pd
import pytest

from backend.fundamentals.fundamental_agent import AgentRunResult
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.technical import technical_agent as technical_agent_module
from backend.technical.technical_agent import (
    TECHNICAL_PROMPT_VERSION,
    TechnicalAnalysisAgent,
    TechnicalEvaluationResult,
    TechnicalVerdict,
    _build_technical_user_prompt,
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


def test_technical_verdict_rejects_coercion_and_unknown_fields():
    payload = _sample_verdict(
        relevant_levels=[
            {
                "price": 95.0,
                "role": "support",
                "relevance": "high",
                "why": "Repeated reactions.",
            }
        ]
    ).model_dump(mode="json")

    with pytest.raises(Exception):
        TechnicalVerdict.model_validate({**payload, "confirmed": "true"})

    with pytest.raises(Exception):
        TechnicalVerdict.model_validate({**payload, "unexpected": "discarded today"})

    nested = json.loads(json.dumps(payload))
    nested["relevant_levels"][0]["unexpected"] = "discarded today"
    with pytest.raises(Exception):
        TechnicalVerdict.model_validate(nested)


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


def test_agent_evaluate_returns_code_stamped_provenance_receipt(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict(model_used="model-controlled"))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    result = agent.evaluate("DEMO", _sample_candles(), _sample_levels())

    assert isinstance(result, TechnicalEvaluationResult)
    assert result.error_type is None
    assert result.verdict is not None
    assert result.verdict.model_used == "test-model"
    receipt = result.provenance
    assert receipt.model_name == "test-model"
    assert receipt.prompt_version == TECHNICAL_PROMPT_VERSION
    expected_prompt_hash = hashlib.sha256(
        f"{runner.last_system_prompt}\n\n{runner.last_prompt}".encode()
    ).hexdigest()
    assert receipt.prompt_sha256 == expected_prompt_hash
    assert receipt.input_context_hash and len(receipt.input_context_hash) == 64
    assert receipt.generated_at.tzinfo is not None
    assert receipt.cache_hit is False
    assert receipt.verdict == "at_support"
    assert receipt.confidence == 7
    assert receipt.decision_reason == result.verdict.reasoning
    assert {ref.source_label for ref in receipt.evidence_references} == {
        "daily OHLC window",
        "major support/resistance levels",
        "technical detector parameters",
    }
    assert all(len(ref.sha256) == 64 for ref in receipt.evidence_references)
    assert all(ref.sanitized_url is None for ref in receipt.evidence_references)
    assert result.validated_verdict_json["model_used"] == "test-model"


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
    first = agent.evaluate("DEMO", candles, levels)
    latest_date = str(candles.iloc[-1]["timestamp"])[:10]
    cache_key = agent._cache_model_key("DEMO", candles, levels)
    envelope = cache.get_verdict("DEMO", cache_key, latest_date)
    assert envelope is not None
    assert envelope["schema_version"] == 2
    assert len(envelope["integrity_hmac_sha256"]) == 64
    assert envelope["prompt_version"] == TECHNICAL_PROMPT_VERSION
    assert envelope["verdict"] == first.validated_verdict_json
    assert "provenance" in envelope

    # Second call on the same candle date must hit the cache, not the runner.
    runner.calls = 0
    second = agent.evaluate("DEMO", candles, levels)
    assert runner.calls == 0
    assert second.provenance.cache_hit is True
    assert second.provenance.prompt_sha256 == first.provenance.prompt_sha256


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


def test_agent_rejects_tampered_cached_receipt_and_recomputes(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict(confidence=8))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()
    levels = _sample_levels()

    agent.evaluate("DEMO", candles, levels)
    data_date = str(candles.iloc[-1]["timestamp"])[:10]
    cache_key = agent._cache_model_key("DEMO", candles, levels)
    envelope = cache.get_verdict("DEMO", cache_key, data_date)
    envelope["provenance"]["evidence_references"][0]["sha256"] = "not-a-hash"
    cache.set_verdict("DEMO", cache_key, data_date, envelope)
    runner.verdict = _sample_verdict(confidence=4)
    runner.calls = 0

    result = agent.evaluate("DEMO", candles, levels)

    assert runner.calls == 1
    assert result.verdict is not None and result.verdict.confidence == 4
    assert result.provenance.cache_hit is False


def test_agent_recomputes_when_cached_envelope_contains_non_finite_json(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict(confidence=8))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()
    levels = _sample_levels()

    agent.evaluate("DEMO", candles, levels)
    data_date = str(candles.iloc[-1]["timestamp"])[:10]
    cache_key = agent._cache_model_key("DEMO", candles, levels)
    envelope = cache.get_verdict("DEMO", cache_key, data_date)
    envelope["verdict"]["confidence"] = float("nan")
    cache.set_verdict("DEMO", cache_key, data_date, envelope)
    runner.verdict = _sample_verdict(confidence=4)
    runner.calls = 0

    result = agent.evaluate("DEMO", candles, levels)

    assert runner.calls == 1
    assert result.verdict is not None and result.verdict.confidence == 4
    assert result.provenance.cache_hit is False


def test_agent_rejects_forged_cached_verdict_and_recomputes(tmp_path):
    """A valid-shaped cache edit must never become a trusted chart verdict."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict(confidence=8))
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()
    levels = _sample_levels()

    agent.evaluate("DEMO", candles, levels)
    data_date = str(candles.iloc[-1]["timestamp"])[:10]
    cache_key = agent._cache_model_key("DEMO", candles, levels)
    envelope = cache.get_verdict("DEMO", cache_key, data_date)
    envelope["verdict"].update(
        {
            "pattern": "breakout",
            "confidence": 9,
            "reasoning": "Forged cached approval.",
        }
    )
    cache.set_verdict("DEMO", cache_key, data_date, envelope)
    runner.verdict = _sample_verdict(confidence=4)
    runner.calls = 0

    second = agent.evaluate("DEMO", candles, levels)

    assert runner.calls == 1
    assert second.provenance.cache_hit is False
    assert second.verdict is not None
    assert second.verdict.confidence == 4
    assert second.provenance.verdict == second.verdict.pattern
    assert second.provenance.confidence == second.verdict.confidence
    assert second.provenance.decision_reason == second.verdict.reasoning


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


def test_agent_evaluate_turns_malformed_output_into_safe_auditable_error(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)

    class _SecretProseRunner(_FakeRunner):
        async def __call__(
            self, prompt, *, system_prompt, model, max_turns, tool_context=None
        ):
            self.calls += 1
            self.last_prompt = prompt
            self.last_system_prompt = system_prompt
            return AgentRunResult(text="token=raw-model-secret and no JSON")

    agent = TechnicalAnalysisAgent(
        model="test-model",
        cache=cache,
        runner=_SecretProseRunner(_sample_verdict()),
    )

    result = agent.evaluate("DEMO", _sample_candles(), _sample_levels())

    assert result.verdict is None
    # AI-004: malformed output that survives the retry is recorded as the
    # dedicated AIValidationError type (distinct from an SDK/usage-limit failure).
    assert result.error_type == "AIValidationError"
    assert result.provenance.verdict == "error"
    assert result.provenance.confidence is None
    assert "raw-model-secret" not in (result.provenance.decision_reason or "")
    assert result.validated_verdict_json == {}


def test_agent_retries_then_succeeds_on_transient_malformed_output(
    tmp_path, monkeypatch
):
    """A first malformed answer is retried; the second valid answer wins (AI-004)."""
    monkeypatch.setenv("SCANNER_AI_MAX_ATTEMPTS", "2")
    cache = FundamentalsCache(cache_dir=tmp_path)
    valid_json = json.dumps(_sample_verdict().model_dump(mode="json"))

    class _FlakyRunner(_FakeRunner):
        async def __call__(
            self, prompt, *, system_prompt, model, max_turns, tool_context=None
        ):
            self.calls += 1
            text = "sorry, no JSON yet" if self.calls == 1 else valid_json
            return AgentRunResult(text=text, cost_usd=0.01)

    runner = _FlakyRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    result = agent.evaluate("DEMO", _sample_candles(), _sample_levels())

    assert runner.calls == 2
    assert result.error_type is None
    assert result.verdict is not None
    assert result.verdict.pattern == "at_support"


def test_agent_rejects_verdict_missing_required_fields(tmp_path, monkeypatch):
    """A verdict JSON missing a required field exhausts the retry → AIValidationError."""
    monkeypatch.setenv("SCANNER_AI_MAX_ATTEMPTS", "2")
    cache = FundamentalsCache(cache_dir=tmp_path)
    # Valid JSON object, but the required `reasoning` field is absent.
    missing_field_json = json.dumps(
        {"pattern": "at_support", "confirmed": True, "confidence": 7}
    )

    class _MissingFieldRunner(_FakeRunner):
        async def __call__(
            self, prompt, *, system_prompt, model, max_turns, tool_context=None
        ):
            self.calls += 1
            return AgentRunResult(text=missing_field_json, cost_usd=0.01)

    runner = _MissingFieldRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    result = agent.evaluate("DEMO", _sample_candles(), _sample_levels())

    assert runner.calls == 2  # one retry, then give up
    assert result.verdict is None
    assert result.error_type == "AIValidationError"
    assert result.provenance.verdict == "error"


def test_technical_parser_does_not_include_raw_model_text(tmp_path):
    from backend.fundamentals.fundamental_agent import FundamentalsAgentError

    marker = "UNTRUSTED_MARKDOWN_[click](https://attacker.example/leak)"
    agent = TechnicalAnalysisAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
    )

    with pytest.raises(FundamentalsAgentError) as excinfo:
        agent._parse_verdict(marker, symbol="DEMO", signal_date="2026-01-01")

    assert marker not in str(excinfo.value)


def test_agent_does_not_retry_usage_limit(tmp_path):
    """A usage-limit error is infrastructure, not malformed output: tried once."""
    from backend.fundamentals.fundamental_agent import FundamentalsUsageLimitError

    cache = FundamentalsCache(cache_dir=tmp_path)

    class _UsageLimitRunner(_FakeRunner):
        async def __call__(
            self, prompt, *, system_prompt, model, max_turns, tool_context=None
        ):
            self.calls += 1
            raise FundamentalsUsageLimitError()

    runner = _UsageLimitRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)

    result = agent.evaluate("DEMO", _sample_candles(), _sample_levels())

    assert runner.calls == 1  # NOT retried
    assert result.verdict is None
    assert result.error_type == "FundamentalsUsageLimitError"


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


# ---------------------------------------------------------------------------
# Prompt-injection posture (TEST-003) — the "technical AI" path.
#
# Unlike the fundamental/67 agents, this agent ingests NO untrusted free text:
# its only inputs are deterministic OHLC candles + candle-derived levels, and
# its tools are pure functions of those. These tests lock that posture so a
# future news/sentiment/scrape tool can't silently reintroduce injection risk
# without a failing test (and a security review).
# ---------------------------------------------------------------------------


def test_technical_tools_are_deterministic_analyzers_only():
    # Exactly three candle-derived analyzers; no scraped/web/PDF/search tool.
    assert len(TOOL_NAMES) == 3
    forbidden = (
        "fetch",
        "company",
        "concall",
        "transcript",
        "search",
        "news",
        "web",
        "screener",
        "url",
        "pdf",
        "research",
    )
    for tool_name in TOOL_NAMES:
        assert not any(token in tool_name.lower() for token in forbidden)


def test_technical_agent_does_not_import_untrusted_text_fetchers():
    # If someone wires a scraped-text source into this agent, this fails loudly.
    for fetcher in (
        "fetch_company_data",
        "read_recent_concall_text",
        "ScreenerInFetchError",
        "SerpApiClient",
        "contains_injection",
    ):
        assert not hasattr(technical_agent_module, fetcher)


def test_technical_user_prompt_is_pure_function_of_structured_inputs():
    candles = _sample_candles()
    levels = _sample_levels()
    first = _build_technical_user_prompt("test-model", "DEMO", candles, levels)
    second = _build_technical_user_prompt("test-model", "DEMO", candles, levels)

    # Deterministic (no network/scrape I/O) and built only from symbol + numbers.
    assert first == second
    assert "Stock: DEMO" in first
    # No scraped-evidence channel like the fundamental/67 agents carry.
    assert "source_policy" not in first
    assert "snippet" not in first
