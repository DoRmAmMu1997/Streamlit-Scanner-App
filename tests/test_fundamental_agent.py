"""Tests for the Claude Agent SDK fundamental analysis agent.

The agentic loop is replaced with a tiny in-process fake `runner` so no live
Claude call (and no CLI subprocess) is made. The fake returns a canned
AgentVerdict JSON string — exactly what the real agent's final message would
contain. The two screener.in tools are tested directly via their plain
`_..._impl` methods, which carry the tool logic without the SDK wrappers.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest

from backend.fundamentals.fundamental_agent import (
    AgentRunResult,
    AgentVerdict,
    CriterionResult,
    ForwardOutlook,
    FundamentalAgent,
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
    Observation,
    _mentions_usage_limit,
    _usage_limit_from_message,
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
    """The verdict the fake runner should return as JSON."""
    return AgentVerdict(
        symbol="DEMO",
        rating=8,
        passed_criteria_count=7,
        total_criteria=9,
        criteria_breakdown=[
            CriterionResult(
                name=f"Criterion {i}",
                passed=(i < 7),
                measured_value="value",
                threshold="threshold",
                reasoning="because",
            )
            for i in range(9)
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


class _FakeRunner:
    """Stand-in for the Claude Agent SDK runner used by FundamentalAgent.

    `FundamentalAgent.check` awaits `runner(prompt, system_prompt=, model=,
    max_turns=)` and parses the returned `AgentRunResult.text` as the final
    AgentVerdict JSON. This fake records its invocations and the arguments it
    was last called with, then returns the canned verdict as a JSON string.
    """

    def __init__(self, verdict: AgentVerdict, *, cost_usd: float | None = 0.02):
        self.verdict = verdict
        self.cost_usd = cost_usd
        self.calls = 0
        self.last_system_prompt: str | None = None
        self.last_model: str | None = None
        self.last_prompt: str | None = None

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
    """Regression guard: Claude's structured-output handling rejects
    `minimum` / `maximum` on `integer` JSON Schema types. If a future
    change re-adds Pydantic `Field(ge=..., le=...)` on the integer fields,
    this test catches it BEFORE a live call hits a 400.
    """
    schema = AgentVerdict.model_json_schema()

    rating_props = schema["properties"]["rating"]
    assert rating_props["type"] == "integer"
    assert "minimum" not in rating_props, (
        "rating field must not emit 'minimum' — Claude rejects it. "
        "Use @field_validator instead of Field(ge=...)."
    )
    assert "maximum" not in rating_props, (
        "rating field must not emit 'maximum' — Claude rejects it. "
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

    # passed_criteria_count now allows up to 9 (the curated nine-criteria set).
    AgentVerdict.model_validate({**valid, "passed_criteria_count": 9, "total_criteria": 9})
    # Out-of-range passed_criteria_count raises.
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "passed_criteria_count": 10})
    with pytest.raises(Exception):
        AgentVerdict.model_validate({**valid, "passed_criteria_count": -1})


def test_agent_verdict_forward_outlook_is_a_nested_object():
    """forward_outlook is a ForwardOutlook object with three string subfields.
    The JSON schema must reflect this so structured-output models emit the
    right shape."""
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
    """Older cached verdicts had forward_outlook as a string. The field
    validator must promote those into the new ForwardOutlook shape by routing
    the string into overall_summary — otherwise existing JSON caches on disk
    would all fail to load."""
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


def test_agent_verdict_accepts_universal_mode_with_seven_criteria():
    """The schema must allow a verdict with mode='universal' carrying the
    seven universal criteria (total_criteria=7) — the standard shape for a
    stock outside the curated universe."""
    verdict = AgentVerdict(
        symbol="OUTSIDE",
        mode="universal",
        rating=7,
        passed_criteria_count=5,
        total_criteria=7,
        criteria_breakdown=[
            CriterionResult(
                name=f"Criterion {i}",
                passed=(i < 5),
                measured_value="value",
                threshold="threshold",
                reasoning="because",
            )
            for i in range(7)
        ],
        additional_observations=[],
        summary_comments="Solid; Business Age and Market Leader not assessed.",
        data_freshness="2026-05-27T00:00:00+00:00",
        model_used="test-model",
    )
    assert verdict.mode == "universal"
    assert len(verdict.criteria_breakdown) == 7
    assert verdict.total_criteria == 7
    assert verdict.passed_criteria_count == 5


def test_agent_verdict_default_mode_is_criteria_for_backward_compat():
    """Verdicts without a `mode` in the dict should validate to 'criteria'."""
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


# ---------------------------------------------------------------------------
# End-to-end agent loop with the fake runner
# ---------------------------------------------------------------------------


def test_fundamental_agent_runs_through_runner_and_parses_verdict(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    # Pre-populate the data cache so a real fetch tool would hit cache; the
    # fake runner does not call tools, so this just mirrors a warm cache.
    cache.set_data("DEMO", _sample_screener_data())

    runner = _FakeRunner(_sample_verdict())
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.check("DEMO")

    assert verdict.symbol == "DEMO"
    assert verdict.rating == 8
    assert verdict.passed_criteria_count == 7
    assert runner.calls == 1
    # The agent must build a system prompt carrying the strict JSON-output
    # contract so the model knows to emit AgentVerdict JSON.
    assert "FINAL OUTPUT FORMAT" in (runner.last_system_prompt or "")
    assert runner.last_model == "test-model"


class _RawJSONRunner:
    """Runner that returns a caller-supplied raw JSON string verbatim.

    Unlike `_FakeRunner` (which serializes a fully-formed AgentVerdict), this
    lets a test emit exactly the JSON a real model produced — including JSON
    that OMITS optional bookkeeping fields.
    """

    def __init__(self, raw_text: str):
        self.raw_text = raw_text
        self.calls = 0

    async def __call__(self, prompt, *, system_prompt, model, max_turns):
        self.calls += 1
        return AgentRunResult(text=self.raw_text, cost_usd=None)


def test_check_backfills_when_model_omits_data_freshness_and_model_used(tmp_path):
    """Regression: the live agent crashed on RELAXO because the model's final
    JSON omitted `data_freshness` and `model_used`. Validation ran before
    `_normalize_verdict` could stamp them, so model_validate raised. The agent
    must tolerate their absence and backfill both."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("RELAXO", _sample_screener_data())

    # A minimal valid verdict body that deliberately leaves OUT data_freshness
    # and model_used — exactly the shape that triggered the crash.
    raw = json.dumps(
        {
            "symbol": "RELAXO",
            "mode": "criteria",
            "rating": 6,
            "passed_criteria_count": 5,
            "total_criteria": 9,
            "criteria_breakdown": [],
            "additional_observations": [],
            "summary_comments": "Decent franchise but valuation limits a premium rating.",
            "forward_outlook": {
                "announcements_conclusion": "",
                "concall_conclusion": "",
                "overall_summary": "Steady volume growth expected.",
            },
        }
    )
    runner = _RawJSONRunner(raw)
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.check("RELAXO")

    assert verdict.symbol == "RELAXO"
    assert verdict.rating == 6
    # Both omitted fields are backfilled by _normalize_verdict.
    assert verdict.model_used == "test-model"
    assert verdict.data_freshness  # non-empty ISO timestamp


def test_fundamental_agent_caches_verdict_per_symbol_model_date(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    runner = _FakeRunner(_sample_verdict())
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    # First call writes to disk.
    agent.check("DEMO")
    cached = cache.get_verdict("DEMO", "test-model::criteria", "2026-05-27")
    assert cached is not None
    assert cached["rating"] == 8

    # Second call should HIT the verdict cache and not invoke the runner again.
    runner.calls = 0
    agent.check("DEMO")
    assert runner.calls == 0, (
        "Second check() should not re-run the agent when the verdict cache is fresh"
    )


def test_fundamental_agent_force_refresh_invalidates_cache_and_reruns(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    runner = _FakeRunner(_sample_verdict())
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    # Seed the verdict cache so we can prove force_refresh wipes it.
    agent.check("DEMO")
    assert cache.get_verdict("DEMO", "test-model::criteria", "2026-05-27") is not None

    # force_refresh bypasses the verdict cache, so the runner runs again.
    runner.calls = 0
    agent.check("DEMO", force_refresh=True)
    assert runner.calls == 1


def test_fundamental_agent_requires_model():
    with pytest.raises(ValueError):
        FundamentalAgent(model="")


def test_fundamental_agent_normalize_verdict_fills_blank_fields(tmp_path):
    """If the agent returns a verdict missing model/symbol, stamp them."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    partial = _sample_verdict().model_copy(update={"symbol": "", "model_used": ""})
    runner = _FakeRunner(partial)
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    verdict = agent.check("DEMO")
    assert verdict.symbol == "DEMO"
    assert verdict.model_used == "test-model"


def test_fundamental_agent_parse_failure_raises_clear_error(tmp_path):
    """A runner that returns non-JSON must raise a clear RuntimeError rather
    than a cryptic validation error."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    async def _bad_runner(prompt, *, system_prompt, model, max_turns):
        return AgentRunResult(text="I could not complete the analysis, sorry.")

    agent = FundamentalAgent(model="test-model", cache=cache, runner=_bad_runner)
    with pytest.raises(RuntimeError, match="parseable AgentVerdict"):
        agent.check("DEMO")


def test_fundamental_agent_tolerates_json_in_markdown_fence(tmp_path):
    """Models sometimes wrap the final JSON in a ```json fence — the agent
    must still parse it."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    verdict_json = json.dumps(_sample_verdict().model_dump(mode="json"))

    async def _fenced_runner(prompt, *, system_prompt, model, max_turns):
        return AgentRunResult(text=f"Here is the verdict:\n```json\n{verdict_json}\n```")

    agent = FundamentalAgent(model="test-model", cache=cache, runner=_fenced_runner)
    verdict = agent.check("DEMO")
    assert verdict.symbol == "DEMO"
    assert verdict.rating == 8


# ---------------------------------------------------------------------------
# Tool implementations (exercised directly, no SDK)
# ---------------------------------------------------------------------------


def test_fetch_company_data_impl_uses_cached_data(tmp_path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())
    agent = FundamentalAgent(model="test-model", cache=cache)

    out = agent._fetch_company_data_impl("demo")  # lowercase on purpose
    payload = json.loads(out)
    assert payload["symbol"] == "DEMO"
    assert payload["sector"] == "Banks"


def test_fetch_company_data_impl_rejects_model_supplied_different_symbol(tmp_path, monkeypatch):
    """Tool calls are bound to the symbol the user actually requested.

    Scraped text is untrusted input to the model. If that text tries to steer
    the model into calling `fetch_company_data(symbol="OTHER")`, the tool layer
    should reject it before cache lookup or network fetch.
    """
    from backend.fundamentals import fundamental_agent as agent_module

    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())
    agent = FundamentalAgent(model="test-model", cache=cache)
    monkeypatch.setattr(
        agent_module,
        "fetch_company_data",
        lambda symbol: pytest.fail(f"unexpected fetch for {symbol}"),
    )

    out = agent._fetch_company_data_impl("OTHER", requested_symbol="DEMO")
    payload = json.loads(out)

    assert "error" in payload
    assert "DEMO" in payload["error"]


def test_fetch_company_data_impl_force_refresh_is_local_to_call(tmp_path, monkeypatch):
    """A force refresh should bypass cache for that call only.

    Streamlit caches the `FundamentalAgent` object. Keeping refresh state on the
    instance can leak one user's rerun choice into the next user's ordinary
    cache lookup, so the tool method accepts the decision as an explicit value.
    """
    from backend.fundamentals import fundamental_agent as agent_module

    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())
    fresh = {**_sample_screener_data(), "sector": "Fresh Banks"}
    calls: list[str] = []

    def fake_fetch(symbol: str):
        calls.append(symbol)
        return fresh

    monkeypatch.setattr(agent_module, "fetch_company_data", fake_fetch)
    agent = FundamentalAgent(model="test-model", cache=cache)

    refreshed = json.loads(agent._fetch_company_data_impl("DEMO", force_refresh=True))
    cached_again = json.loads(agent._fetch_company_data_impl("DEMO"))

    assert calls == ["DEMO"]
    assert refreshed["sector"] == "Fresh Banks"
    assert cached_again["sector"] == "Fresh Banks"


def test_concall_transcript_impl_uses_cached_concalls(tmp_path, monkeypatch):
    """The transcript tool reads concalls from cache + extracts the text."""
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

    # Patch the extractor so we don't hit the network or open a PDF.
    monkeypatch.setattr(
        agent_module,
        "read_recent_concall_text",
        lambda concalls, **_kwargs: "Management called out 12% growth guidance for FY26.",
    )

    agent = FundamentalAgent(model="test-model", cache=cache)
    text = agent._read_concall_impl("DEMO")
    assert "12% growth guidance" in text


def test_concall_transcript_impl_uses_requested_symbol_when_model_args_differ(
    tmp_path,
    monkeypatch,
):
    """The transcript tool should read the requested symbol's cached concalls.

    The model's `symbol` argument is not trusted because transcript text can
    contain prompt-injection attempts. The requested symbol from `check(...)`
    remains the source of truth.
    """
    from backend.fundamentals import fundamental_agent as agent_module

    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data(
        "DEMO",
        {
            **_sample_screener_data(),
            "concalls": [{"month": "Jan 2026", "transcript_url": "https://demo/jan.pdf"}],
        },
    )
    cache.set_data(
        "OTHER",
        {
            **_sample_screener_data(),
            "symbol": "OTHER",
            "concalls": [{"month": "Jan 2026", "transcript_url": "https://other/jan.pdf"}],
        },
    )
    seen: list[list[dict[str, Any]]] = []

    def fake_read(concalls, **_kwargs):
        seen.append(list(concalls))
        return "DEMO transcript"

    monkeypatch.setattr(agent_module, "read_recent_concall_text", fake_read)
    agent = FundamentalAgent(model="test-model", cache=cache)

    text = agent._read_concall_impl("OTHER", requested_symbol="DEMO")

    assert text == "DEMO transcript"
    assert seen[0][0]["transcript_url"] == "https://demo/jan.pdf"


def test_concall_transcript_impl_returns_message_when_no_cache(tmp_path):
    """Calling the transcript tool without first fetching returns a clear hint."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    agent = FundamentalAgent(model="test-model", cache=cache)
    text = agent._read_concall_impl("UNCACHED")
    assert "fetch_company_data" in text


# ---------------------------------------------------------------------------
# universal mode + AgentVerdict.mode field
# ---------------------------------------------------------------------------


def test_fundamental_agent_check_universal_mode_forces_seven_criteria(tmp_path):
    """Running in universal mode forces total_criteria=7, regardless of what
    the model emitted (the curated nine-criteria sample has total_criteria=9)."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("OUTSIDE", _sample_screener_data())

    # The runner returns the curated sample (total_criteria=9); the agent's
    # _normalize_verdict must stamp total_criteria=7 because the call is universal.
    polluted = _sample_verdict()  # total_criteria=9
    runner = _FakeRunner(polluted)

    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)
    verdict = agent.check("OUTSIDE", mode="universal")

    assert verdict.mode == "universal"
    assert verdict.total_criteria == 7
    # Rating and the breakdown survive — only the count denominator is enforced.
    assert verdict.rating == polluted.rating
    assert verdict.criteria_breakdown == polluted.criteria_breakdown


def test_fundamental_agent_check_criteria_mode_forces_nine_criteria(tmp_path):
    """Running in criteria mode stamps total_criteria=9 for the curated universe."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    runner = _FakeRunner(_sample_verdict())
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)
    verdict = agent.check("DEMO", mode="criteria")

    assert verdict.mode == "criteria"
    assert verdict.total_criteria == 9


def test_fundamental_agent_verdict_cache_keys_by_mode(tmp_path):
    """Criteria-mode and universal-mode verdicts for the same symbol must NOT
    overwrite each other in the cache."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("BOTH", _sample_screener_data())

    runner = _FakeRunner(_sample_verdict())
    agent = FundamentalAgent(model="test-model", cache=cache, runner=runner)

    agent.check("BOTH", mode="criteria")
    agent.check("BOTH", mode="universal")

    # Two distinct cache files should exist for the same symbol + model + date.
    cached_criteria = cache.get_verdict("BOTH", "test-model::criteria", "2026-05-27")
    cached_universal = cache.get_verdict("BOTH", "test-model::universal", "2026-05-27")
    assert cached_criteria is not None
    assert cached_universal is not None
    assert cached_criteria["mode"] == "criteria"
    assert cached_universal["mode"] == "universal"
    assert cached_criteria["total_criteria"] == 9
    assert cached_universal["total_criteria"] == 7


# ---------------------------------------------------------------------------
# Usage-limit detection + custom error code
# ---------------------------------------------------------------------------


def test_usage_limit_error_exposes_code_and_message():
    err = FundamentalsUsageLimitError(resets_at=1_780_000_000, rate_limit_type="five_hour")
    assert err.code == "usage_limit_reached"
    assert err.resets_at == 1_780_000_000
    assert err.rate_limit_type == "five_hour"
    text = str(err)
    assert "usage limit" in text.lower()
    assert "Cached verdicts" in text
    assert "resets" in text.lower()  # the reset timestamp is rendered in


def test_usage_limit_error_without_reset_time():
    err = FundamentalsUsageLimitError()
    assert err.code == "usage_limit_reached"
    assert "once your usage limit resets" in str(err)


def test_base_agent_error_has_distinct_code():
    assert FundamentalsAgentError.code == "agent_error"
    assert issubclass(FundamentalsUsageLimitError, FundamentalsAgentError)


def test_detect_usage_limit_from_rate_limit_event():
    event = SimpleNamespace(
        rate_limit_info=SimpleNamespace(
            status="rejected", resets_at=1_780_000_000, rate_limit_type="seven_day"
        )
    )
    err = _usage_limit_from_message(event)
    assert isinstance(err, FundamentalsUsageLimitError)
    assert err.resets_at == 1_780_000_000
    assert err.rate_limit_type == "seven_day"


def test_detect_usage_limit_ignores_allowed_warning():
    # "allowed_warning" means approaching the limit, not hitting it.
    event = SimpleNamespace(rate_limit_info=SimpleNamespace(status="allowed_warning"))
    assert _usage_limit_from_message(event) is None


@pytest.mark.parametrize("error_kind", ["rate_limit", "billing_error"])
def test_detect_usage_limit_from_assistant_error(error_kind):
    message = SimpleNamespace(error=error_kind)
    assert isinstance(_usage_limit_from_message(message), FundamentalsUsageLimitError)


def test_detect_usage_limit_ignores_normal_message():
    assert _usage_limit_from_message(SimpleNamespace()) is None
    assert _usage_limit_from_message(SimpleNamespace(error=None)) is None
    assert _usage_limit_from_message(SimpleNamespace(error="server_error")) is None


def test_mentions_usage_limit_text_fallback():
    assert _mentions_usage_limit("Error: rate limit exceeded")
    assert _mentions_usage_limit(None, "you are OUT OF CREDIT")
    assert not _mentions_usage_limit("connection reset by peer")
    assert not _mentions_usage_limit(None, None)


def test_check_propagates_usage_limit_error(tmp_path):
    """A usage-limit error raised inside the run must propagate unchanged so the
    UI can catch FundamentalsUsageLimitError specifically, not a generic error."""
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("DEMO", _sample_screener_data())

    async def _limit_runner(prompt, *, system_prompt, model, max_turns):
        raise FundamentalsUsageLimitError(resets_at=1_780_000_000)

    agent = FundamentalAgent(model="test-model", cache=cache, runner=_limit_runner)
    with pytest.raises(FundamentalsUsageLimitError) as excinfo:
        agent.check("DEMO")
    assert excinfo.value.code == "usage_limit_reached"
