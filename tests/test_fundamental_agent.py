from __future__ import annotations

"""Tests for the LangChain fundamental analysis agent.

The LLM is replaced with a tiny in-process fake so no live OpenRouter call
is made. The fake mimics just enough of ChatOpenAI's interface to drive the
agent's two-phase loop (tool-call turn, final narrative turn, structured
output coercion).
"""

from typing import Any

import pytest
from langchain_core.messages import AIMessage

from backend.fundamentals.fundamental_agent import (
    AgentVerdict,
    CriterionResult,
    FundamentalAgent,
    Observation,
)
from backend.fundamentals.fundamentals_cache import FundamentalsCache


def _sample_screener_data() -> dict[str, Any]:
    """The data the agent should "find" via its fetch_company_data tool."""
    return {
        "symbol": "DEMO",
        "company_name": "Demo Industries Ltd.",
        "sector": "Banks",
        "fetched_at": "2026-05-27T12:00:00+00:00",
        "source_url": "http://test/demo",
        "current_price": 1500,
        "market_cap": 150000,
        "pe": 22.5,
        "roce_ttm": 18.5,
        "roe_ttm": 16.0,
        "latest_net_profit": 280,
        "latest_revenue": 62000,
        "latest_eps": 45,
        "latest_debt": 1500,
        "latest_cash_equivalents": 800,
        "latest_equity_capital": 200,
        "latest_reserves": 10000,
        "revenue_history": [40000, 45000, 50000, 58000, 62000],
        "profit_history": [180, 210, 230, 260, 280],
        "eps_history": [30, 34, 37, 42, 45],
    }


def _sample_verdict() -> AgentVerdict:
    """The verdict the fake LLM should return through structured output."""
    return AgentVerdict(
        symbol="DEMO",
        rating=8,
        passed_criteria_count=6,
        total_criteria=7,
        criteria_breakdown=[
            CriterionResult(
                name=f"Criterion {i}",
                passed=(i < 6),
                measured_value="value",
                threshold="threshold",
                reasoning="because",
            )
            for i in range(7)
        ],
        additional_observations=[
            Observation(
                topic="Margin trend",
                finding="Operating margins expanded 200bps over 3 years.",
                sentiment="positive",
                evidence="Profit history shows 50% net profit growth on 55% revenue growth.",
            ),
            Observation(
                topic="Capital allocation",
                finding="Strong free cash conversion.",
                sentiment="positive",
                evidence="Free cash flow positive every year.",
            ),
        ],
        summary_comments=(
            "Demo Industries is a high-quality bank with strong returns and clean "
            "balance sheet. Minor concern on promoter holding decrease but overall "
            "fundamentals remain solid."
        ),
        data_freshness="2026-05-27T12:00:00+00:00",
        model_used="test-model",
    )


class _FakeLLM:
    """Tiny stand-in for `ChatOpenAI` used by FundamentalAgent tests.

    Implements the methods FundamentalAgent actually calls:
      - `bind_tools(tools)` → returns a wrapper with tool-call invoke
      - `with_structured_output(schema)` → returns a wrapper that emits the
        prepared AgentVerdict
    The wrappers track which "turn" they are on so the first invoke yields
    a tool call and the second yields a final assistant message.
    """

    def __init__(self, verdict: AgentVerdict):
        self.verdict = verdict
        self.tool_call_turn: AIMessage | None = None
        self.bound_tool = None

    def bind_tools(self, tools):
        # Just remember the bound tool; the wrapper does the heavy lifting.
        bound = _BoundLLM(self, tools)
        self.bound_tool = bound
        return bound

    def with_structured_output(self, schema):
        return _StructuredLLM(self.verdict)


class _BoundLLM:
    def __init__(self, parent: _FakeLLM, tools):
        self.parent = parent
        self.tools = tools
        self.invocations = 0

    def invoke(self, messages):
        self.invocations += 1
        if self.invocations == 1:
            # First call → request the fetch tool with a symbol.
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": self.tools[0].name,
                        "args": {"symbol": "DEMO"},
                        "id": "tool-call-1",
                    }
                ],
            )
        # Second call → narrative analysis (text only, no further tool calls).
        return AIMessage(
            content=(
                "I have all the data I need to evaluate Demo Industries. "
                "Proceeding to the structured verdict."
            ),
        )


class _StructuredLLM:
    def __init__(self, verdict: AgentVerdict):
        self.verdict = verdict

    def invoke(self, messages):
        return self.verdict


# ---------------------------------------------------------------------------
# Schema unit tests
# ---------------------------------------------------------------------------


def test_agent_verdict_rejects_rating_outside_zero_to_ten():
    with pytest.raises(Exception):
        AgentVerdict(
            symbol="DEMO",
            rating=11,
            passed_criteria_count=7,
            criteria_breakdown=[],
            additional_observations=[],
            summary_comments="x",
            data_freshness="2026-01-01",
            model_used="m",
        )


def test_observation_sentiment_must_be_valid():
    with pytest.raises(Exception):
        Observation(
            topic="Anything",
            finding="x",
            sentiment="bullish",  # invalid; only positive/negative/neutral
            evidence="x",
        )


def test_agent_verdict_json_schema_omits_min_max_on_integer_fields():
    """Regression guard: Anthropic's structured-output API rejects
    `minimum` / `maximum` on `integer` JSON Schema types. If a future
    change re-adds Pydantic `Field(ge=..., le=...)` on the integer fields,
    this test catches it BEFORE a live call hits a 400 from Anthropic.
    """
    schema = AgentVerdict.model_json_schema()

    rating_props = schema["properties"]["rating"]
    assert rating_props["type"] == "integer"
    assert "minimum" not in rating_props, (
        "rating field must not emit 'minimum' — Anthropic rejects it. "
        "Use @field_validator instead of Field(ge=...)."
    )
    assert "maximum" not in rating_props, (
        "rating field must not emit 'maximum' — Anthropic rejects it. "
        "Use @field_validator instead of Field(le=...)."
    )

    pcc_props = schema["properties"]["passed_criteria_count"]
    assert pcc_props["type"] == "integer"
    assert "minimum" not in pcc_props
    assert "maximum" not in pcc_props


def test_agent_verdict_field_validator_still_rejects_out_of_range_rating():
    """The field_validator must keep enforcing 0 <= rating <= 10 at parse time."""
    valid = dict(
        symbol="DEMO",
        rating=8,
        passed_criteria_count=6,
        total_criteria=7,
        criteria_breakdown=[],
        additional_observations=[],
        summary_comments="ok",
        data_freshness="2026-01-01",
        model_used="m",
    )
    # Sanity: a valid verdict still parses.
    AgentVerdict.model_validate(valid)

    # Out-of-range rating raises.
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "rating": 11})
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "rating": -1})

    # Out-of-range passed_criteria_count raises.
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "passed_criteria_count": 8})
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "passed_criteria_count": -1})


# ---------------------------------------------------------------------------
# End-to-end agent loop with the fake LLM
# ---------------------------------------------------------------------------


def test_fundamental_agent_runs_through_tool_then_structured_output(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    # Pre-populate the data cache so the fetch tool returns synthetically
    # without hitting screener.in.
    cache.set_data("DEMO", _sample_screener_data())

    fake_llm = _FakeLLM(_sample_verdict())
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    verdict = agent.check("DEMO")

    assert verdict.symbol == "DEMO"
    assert verdict.rating == 8
    assert verdict.passed_criteria_count == 6
    # The bound LLM must have been invoked at least twice (tool-call turn + final).
    assert fake_llm.bound_tool is not None
    assert fake_llm.bound_tool.invocations >= 2


def test_fundamental_agent_caches_verdict_per_symbol_model_date(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    fake_llm = _FakeLLM(_sample_verdict())
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    # First call writes to disk
    agent.check("DEMO")
    cached = cache.get_verdict("DEMO", "test-model", "2026-05-27")
    assert cached is not None
    assert cached["rating"] == 8

    # Second call should HIT the verdict cache and not invoke the LLM again.
    fake_llm.bound_tool.invocations = 0  # reset counter
    agent.check("DEMO")
    assert fake_llm.bound_tool.invocations == 0, (
        "Second check() should not re-invoke the LLM when the verdict cache is fresh"
    )


def test_fundamental_agent_force_refresh_invalidates_cache_and_reruns(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    fake_llm = _FakeLLM(_sample_verdict())
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    # Seed the verdict cache so we can prove force_refresh wipes it.
    agent.check("DEMO")
    assert cache.get_verdict("DEMO", "test-model", "2026-05-27") is not None

    # force_refresh should invalidate the data cache (which triggers a fetch
    # attempt) — we monkey-patch the fetch by writing fresh data back into
    # the cache via the fake tool path. For this unit test we only need to
    # verify the LLM was re-invoked, which proves the verdict cache was bypassed.
    fake_llm.bound_tool.invocations = 0
    # Refill the data cache the tool would have produced.
    cache.set_data("DEMO", _sample_screener_data())
    agent.check("DEMO", force_refresh=True)
    assert fake_llm.bound_tool.invocations >= 2


def test_fundamental_agent_requires_api_key():
    with pytest.raises(ValueError):
        FundamentalAgent(api_key="", model="test-model")


def test_fundamental_agent_normalize_verdict_fills_blank_fields(tmp_path):
    """If the LLM returns a verdict missing model/symbol, the agent should stamp them."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    partial = _sample_verdict().model_copy(update={"symbol": "", "model_used": ""})
    fake_llm = _FakeLLM(partial)
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    verdict = agent.check("DEMO")
    assert verdict.symbol == "DEMO"
    assert verdict.model_used == "test-model"
