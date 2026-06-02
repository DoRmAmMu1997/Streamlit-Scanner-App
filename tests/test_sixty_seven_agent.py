from __future__ import annotations

import json
from typing import Any

import pandas as pd
import pytest

from backend.fundamentals.fundamental_agent import AgentRunResult
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.sixty_seven.agent import (
    EvidenceItem,
    FallReasonCategory,
    SixtySevenAgent,
    SixtySevenVerdict,
)
from backend.sixty_seven.shortlister import shortlist_candidate


def _candidate():
    candles = pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=3, freq="D"),
            "open": [250.0, 150.0, 90.0],
            "high": [300.0, 250.0, 120.0],
            "low": [240.0, 140.0, 89.0],
            "close": [250.0, 150.0, 90.0],
        }
    )
    candidate = shortlist_candidate("DEMO", candles)
    assert candidate is not None
    return candidate


def _verdict(**overrides: Any) -> SixtySevenVerdict:
    base = dict(
        symbol="DEMO",
        approved=True,
        fall_reason_category="business",
        fall_reason_clear=True,
        fall_reason_no_longer_exists=True,
        proven_profit_record=True,
        future_growth_prospects=True,
        quarterly_improvement=True,
        minimum_upside_100pct=True,
        confidence=8,
        evidence=[
            EvidenceItem(
                source="Screener.in",
                title="Quarterly profit trend",
                link="https://www.screener.in/company/DEMO/",
                snippet="Net profit improved for the latest quarter.",
            )
        ],
        rejection_reason="",
        summary="The earlier business pressure appears to have faded.",
        model_used="test-model",
    )
    base.update(overrides)
    return SixtySevenVerdict(**base)


class _FakeRunner:
    def __init__(self, verdict: SixtySevenVerdict, *, fenced: bool = False):
        self.verdict = verdict
        self.fenced = fenced
        self.calls = 0
        self.last_prompt = ""
        self.last_system_prompt = ""
        self.last_model = ""

    async def __call__(self, prompt, *, system_prompt, model, max_turns):
        self.calls += 1
        self.last_prompt = prompt
        self.last_system_prompt = system_prompt
        self.last_model = model
        payload = json.dumps(self.verdict.model_dump(mode="json"))
        if self.fenced:
            payload = f"Analysis:\n```json\n{payload}\n```"
        return AgentRunResult(text=payload, cost_usd=0.01)


def test_sixty_seven_verdict_rejects_confidence_outside_zero_to_ten():
    with pytest.raises(Exception):
        _verdict(confidence=11)


def test_sixty_seven_verdict_uses_known_fall_categories():
    verdict = _verdict(fall_reason_category="sentiment")
    assert verdict.fall_reason_category == "sentiment"
    with pytest.raises(Exception):
        _verdict(fall_reason_category="random")


def test_agent_returns_parsed_verdict_and_prompt_contains_drawdown_facts(tmp_path):
    runner = _FakeRunner(_verdict())
    agent = SixtySevenAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
        runner=runner,
    )

    verdict = agent.verify("demo", _candidate())

    assert verdict.symbol == "DEMO"
    assert verdict.approved is True
    assert runner.calls == 1
    assert runner.last_model == "test-model"
    assert "research_company" in runner.last_system_prompt
    assert "drawdown_pct" in runner.last_prompt


def test_agent_parses_fenced_json(tmp_path):
    runner = _FakeRunner(_verdict(fall_reason_category="fundamental"), fenced=True)
    agent = SixtySevenAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
        runner=runner,
    )

    assert agent.verify("DEMO", _candidate()).fall_reason_category == "fundamental"


def test_agent_caches_verdict_per_symbol_model_and_candidate_context(tmp_path):
    runner = _FakeRunner(_verdict())
    agent = SixtySevenAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
        runner=runner,
    )
    candidate = _candidate()

    first = agent.verify("DEMO", candidate)
    second = agent.verify("DEMO", candidate)

    assert first.summary == second.summary
    assert runner.calls == 1


def test_agent_force_refresh_bypasses_verdict_cache(tmp_path):
    runner = _FakeRunner(_verdict())
    agent = SixtySevenAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
        runner=runner,
    )
    candidate = _candidate()

    agent.verify("DEMO", candidate)
    agent.verify("DEMO", candidate, force_refresh=True)

    assert runner.calls == 2


def test_research_tool_rejects_model_supplied_different_symbol(tmp_path):
    agent = SixtySevenAgent(
        model="test-model",
        cache=FundamentalsCache(cache_dir=tmp_path),
        runner=_FakeRunner(_verdict()),
    )

    payload = json.loads(agent._research_company_impl("OTHER", requested_symbol="DEMO"))

    assert "rejected" in payload["error"].lower()


def test_fall_reason_category_type_alias_is_public():
    assert "sentiment" in FallReasonCategory.__args__
