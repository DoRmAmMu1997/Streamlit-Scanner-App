from __future__ import annotations

"""Tests for the Claude Agent SDK technical-analysis agent.

The agentic loop is replaced with a tiny in-process fake `runner` so no live
Claude call (and no CLI subprocess) is made. The fake returns a canned
TechnicalVerdict JSON string — exactly what the real agent's final message
would contain. This mirrors `tests/test_fundamental_agent.py`.
"""

import json

import pandas as pd
import pytest

from backend.fundamentals.fundamental_agent import AgentRunResult
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.technical.technical_agent import (
    TechnicalAnalysisAgent,
    TechnicalVerdict,
)


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

    async def __call__(
        self,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        max_turns: int,
    ) -> AgentRunResult:
        self.calls += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        self.last_model = model
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


def test_agent_caches_verdict_per_symbol_model_candle_date(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    runner = _FakeRunner(_sample_verdict())
    agent = TechnicalAnalysisAgent(model="test-model", cache=cache, runner=runner)
    candles = _sample_candles()

    # First call writes to disk under the "::technical" key + latest candle date.
    agent.analyze("DEMO", candles, _sample_levels())
    latest_date = str(candles.iloc[-1]["timestamp"])[:10]
    assert cache.get_verdict("DEMO", "test-model::technical", latest_date) is not None

    # Second call on the same candle date must hit the cache, not the runner.
    runner.calls = 0
    agent.analyze("DEMO", candles, _sample_levels())
    assert runner.calls == 0


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
        async def __call__(self, prompt, *, system_prompt, model, max_turns):
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
        async def __call__(self, prompt, *, system_prompt, model, max_turns):
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


def test_agent_requires_model():
    with pytest.raises(ValueError):
        TechnicalAnalysisAgent(model="")
