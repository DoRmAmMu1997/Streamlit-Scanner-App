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
    _MAX_OUTPUT_TOKENS,
    AgentVerdict,
    CriterionResult,
    ForwardOutlook,
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
            # First call → request the fetch_company_data tool with a symbol.
            # Find the company-data tool by name so this still works after
            # Job 4 added a second tool (read_recent_concall_transcript).
            fetch_tool_name = next(
                tool.name
                for tool in self.tools
                if tool.name == "fetch_company_data_tool"
            )
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": fetch_tool_name,
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
    cached = cache.get_verdict("DEMO", "test-model::criteria", "2026-05-27")
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
    assert cache.get_verdict("DEMO", "test-model::criteria", "2026-05-27") is not None

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


def test_fundamental_agent_caps_output_tokens(tmp_path):
    """The real ChatOpenAI client must be built with a bounded max_tokens.

    Without this cap OpenRouter pre-authorizes credits for the model's full
    output capacity (64K for Claude Sonnet 4.5) and returns HTTP 402 once the
    key balance dips below that worst-case reservation. Building the agent
    WITHOUT an injected llm constructs the real client; we assert the cap is
    wired through. (Construction makes no network call.)
    """
    assert _MAX_OUTPUT_TOKENS == 16000

    agent = FundamentalAgent(
        api_key="test-key",
        model="anthropic/claude-sonnet-4.5",
        cache=FundamentalsCache(cache_dir=tmp_path),
    )
    assert agent._llm.max_tokens == _MAX_OUTPUT_TOKENS


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


# ---------------------------------------------------------------------------
# Job 4 schema + tool-binding tests
# ---------------------------------------------------------------------------


def test_agent_verdict_forward_outlook_is_a_nested_object():
    """Job 6: forward_outlook is no longer a string — it's a ForwardOutlook
    object with three string subfields. The JSON schema must reflect this
    so structured-output models emit the right shape."""
    schema = AgentVerdict.model_json_schema()
    assert "forward_outlook" in schema["properties"]
    field_schema = schema["properties"]["forward_outlook"]
    # Pydantic emits nested models either inline (type=object) or as a $ref
    # into the schema's `$defs`. Both shapes are acceptable.
    if "$ref" in field_schema:
        ref_name = field_schema["$ref"].rsplit("/", 1)[-1]
        defs = schema.get("$defs", {})
        assert ref_name in defs, f"$ref target {ref_name} missing from schema $defs"
        target = defs[ref_name]
    else:
        target = field_schema
    assert target.get("type") == "object"
    subfields = target.get("properties", {})
    for required_subfield in (
        "announcements_conclusion",
        "concall_conclusion",
        "overall_summary",
    ):
        assert required_subfield in subfields, (
            f"ForwardOutlook missing subfield {required_subfield}"
        )
        assert subfields[required_subfield].get("type") == "string"


def test_agent_verdict_accepts_legacy_string_forward_outlook():
    """Pre-Job-6 cached verdicts had forward_outlook as a string. The
    field validator must promote those into the new ForwardOutlook shape
    by routing the string into overall_summary — otherwise existing JSON
    caches on disk would all fail to load."""
    legacy_payload = {
        "symbol": "LEGACY",
        "rating": 8,
        "passed_criteria_count": 7,
        "criteria_breakdown": [],
        "additional_observations": [],
        "summary_comments": "ok",
        "forward_outlook": "Demand environment looks supportive over FY26.",
        "data_freshness": "2026-01-01",
        "model_used": "legacy-model",
    }
    verdict = AgentVerdict.model_validate(legacy_payload)
    assert isinstance(verdict.forward_outlook, ForwardOutlook)
    assert verdict.forward_outlook.overall_summary == (
        "Demand environment looks supportive over FY26."
    )
    # Other subfields default to empty.
    assert verdict.forward_outlook.announcements_conclusion == ""
    assert verdict.forward_outlook.concall_conclusion == ""


def test_fundamental_agent_binds_both_tools(tmp_path):
    """The agent should now bind two tools: fetch + concall transcript reader."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    verdict = _sample_verdict().model_copy(
        update={
            "forward_outlook": ForwardOutlook(
                announcements_conclusion="Recent contract wins point to strong enterprise demand.",
                concall_conclusion="Management reaffirmed 12% FY26 revenue growth guidance.",
                overall_summary="Demand environment looks supportive over the next 1-4 quarters.",
            ),
        }
    )
    fake_llm = _FakeLLM(verdict)
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    agent.check("DEMO")

    # The fake records the bound tools list for inspection.
    assert fake_llm.bound_tool is not None
    tool_names = {tool.name for tool in fake_llm.bound_tool.tools}
    assert "fetch_company_data_tool" in tool_names
    assert "read_recent_concall_transcript" in tool_names


def test_concall_transcript_tool_uses_cached_concalls(tmp_path, monkeypatch):
    """When invoked directly, the transcript tool reads concalls from cache + downloads."""
    from backend.fundamentals import fundamental_agent as agent_module

    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data(
        "DEMO",
        {
            **_sample_screener_data(),
            "concalls": [
                {"month": "Jan 2026", "transcript_url": "https://example.com/jan.pdf"},
            ],
        },
    )

    # Patch the orchestrator so we don't actually hit the network or open a PDF.
    monkeypatch.setattr(
        agent_module,
        "read_recent_concall_text",
        lambda concalls, **_kwargs: "Management called out 12% growth guidance for FY26.",
    )

    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=_FakeLLM(_sample_verdict()),  # type: ignore[arg-type]
    )
    tool = agent._build_transcript_tool()
    text = tool.invoke({"symbol": "DEMO"})
    assert "12% growth guidance" in text


def test_concall_transcript_tool_returns_message_when_no_cache(tmp_path):
    """Calling the transcript tool without first calling fetch should return a clear hint."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=_FakeLLM(_sample_verdict()),  # type: ignore[arg-type]
    )
    tool = agent._build_transcript_tool()
    text = tool.invoke({"symbol": "UNCACHED"})
    assert "fetch_company_data" in text


# ---------------------------------------------------------------------------
# Job 5: insights-only mode + AgentVerdict.mode field
# ---------------------------------------------------------------------------


def test_agent_verdict_accepts_insights_only_mode_with_empty_criteria():
    """The schema must allow a verdict with mode='insights_only' and an empty
    criteria breakdown — that's the standard insights-only output shape."""
    verdict = AgentVerdict(
        symbol="OUTSIDE",
        mode="insights_only",
        rating=7,
        # Defaults: passed_criteria_count=0, criteria_breakdown=[].
        additional_observations=[],
        summary_comments="Strong fundamentals; insights-only assessment.",
        data_freshness="2026-05-27T00:00:00+00:00",
        model_used="test-model",
    )
    assert verdict.mode == "insights_only"
    assert verdict.criteria_breakdown == []
    assert verdict.passed_criteria_count == 0


def test_agent_verdict_default_mode_is_criteria_for_backward_compat():
    """Pre-Job-5 verdicts (no `mode` in the dict) should validate to 'criteria'."""
    verdict = AgentVerdict.model_validate(
        {
            "symbol": "LEGACY",
            "rating": 8,
            "passed_criteria_count": 7,
            "criteria_breakdown": [],
            "additional_observations": [],
            "summary_comments": "ok",
            "data_freshness": "2026-01-01",
            "model_used": "m",
        }
    )
    assert verdict.mode == "criteria"


def test_fundamental_agent_check_insights_mode_enforces_invariants(tmp_path):
    """Running in insights_only mode produces a verdict with empty criteria
    breakdown AND passed_criteria_count=0, even if the LLM returns otherwise."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("OUTSIDE", _sample_screener_data())

    # The LLM returns a verdict that DOES include criteria — the agent's
    # _normalize_verdict must override it because the call is insights-only.
    polluted = _sample_verdict()  # has criteria_breakdown with 7 entries
    fake_llm = _FakeLLM(polluted)

    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )
    verdict = agent.check("OUTSIDE", mode="insights_only")

    assert verdict.mode == "insights_only"
    assert verdict.criteria_breakdown == []
    assert verdict.passed_criteria_count == 0
    # Rating and observations survive — they're independent of the mode override.
    assert verdict.rating == polluted.rating


def test_fundamental_agent_verdict_cache_keys_by_mode(tmp_path):
    """Criteria-mode and insights-only verdicts for the same symbol must NOT
    overwrite each other in the cache."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("BOTH", _sample_screener_data())

    fake_llm = _FakeLLM(_sample_verdict())
    agent = FundamentalAgent(
        api_key="test-key",
        model="test-model",
        cache=cache,
        llm=fake_llm,  # type: ignore[arg-type]
    )

    criteria_verdict = agent.check("BOTH", mode="criteria")
    insights_verdict = agent.check("BOTH", mode="insights_only")

    # Two distinct cache files should exist for the same symbol + model + date.
    cached_criteria = cache.get_verdict("BOTH", "test-model::criteria", "2026-05-27")
    cached_insights = cache.get_verdict("BOTH", "test-model::insights_only", "2026-05-27")
    assert cached_criteria is not None
    assert cached_insights is not None
    assert cached_criteria["mode"] == "criteria"
    assert cached_insights["mode"] == "insights_only"
    assert cached_insights["criteria_breakdown"] == []
