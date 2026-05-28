from __future__ import annotations

"""LangChain agent for the per-stock Check Fundamentals button.

How it works:
1. The Streamlit UI selects a row from any screener's results table and
   passes the symbol to `FundamentalAgent.check(symbol)`.
2. The agent is wired with a single tool, `fetch_company_data`, that wraps
   `backend.fundamentals.screener_in_client.fetch_company_data` (with cache).
3. A senior-analyst system prompt instructs the LLM to:
   - apply the seven user-defined criteria exactly,
   - add 4-8 additional fundamental observations of its choosing
     (margins, capital allocation, governance, etc.),
   - synthesize a HOLISTIC 0-10 rating (not a simple count),
   - return the `AgentVerdict` Pydantic schema.
4. The LLM runs a short tool-calling loop (max 3 turns: one fetch, one
   reasoning pass, one final structured answer).
5. The verdict is cached on disk per (symbol, model, data_date) so
   re-clicks are free; the "Re-run analysis" button bypasses the cache.

OpenRouter compatibility note:
`ChatOpenAI(base_url="https://openrouter.ai/api/v1")` works because
OpenRouter exposes the OpenAI-compatible chat completions surface. The
default model (`anthropic/claude-sonnet-4.5`) supports OpenAI-style tool
calls. If the user swaps to a model that does NOT support tool calling,
the agent will raise — the system prompt comment below flags this.
"""

import json
import logging
from datetime import UTC, datetime
from typing import Any, Literal

from langchain_core.messages import (
    AIMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.tools import tool
from langchain_openai import ChatOpenAI
from pydantic import BaseModel, Field, field_validator

from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.fundamentals.pdf_reader import read_recent_concall_text
from backend.fundamentals.screener_in_client import (
    ScreenerInFetchError,
    fetch_company_data,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic schemas (the agent's structured output)
# ---------------------------------------------------------------------------


class CriterionResult(BaseModel):
    """One of the seven user-defined criteria with the agent's verdict."""

    name: str = Field(
        description="Short label for the criterion, e.g. 'Net Debt/Equity < 0.2'."
    )
    passed: bool = Field(description="True if the company meets this criterion.")
    measured_value: str = Field(
        description="The actual value found for this stock, human-readable."
    )
    threshold: str = Field(description="The threshold/rule expressed in plain English.")
    reasoning: str = Field(
        description="1-2 sentences explaining how the measured value was derived."
    )


class ForwardOutlook(BaseModel):
    """Three-part structured forward outlook produced by the agent.

    Each subsection corresponds to a specific data source so the UI can
    render the analysis with clear provenance:

    - `announcements_conclusion` is sourced from the recent corporate
      Announcements (always available in the base payload).
    - `concall_conclusion` is sourced from the most recent quarterly
      Concall transcript and stays empty when the agent did not call the
      `read_recent_concall_transcript` tool — the agent must NOT speculate
      about transcript contents it never read.
    - `overall_summary` is the agent's integrated view that ties both
      signals plus broader sector knowledge into a forward projection.
    """

    announcements_conclusion: str = Field(
        default="",
        description=(
            "One medium-length paragraph on what the recent corporate "
            "Announcements signal about the company's direction. Cite "
            "specific announcement items where useful. Empty only when the "
            "`announcements` array in the source data is genuinely empty."
        ),
    )
    concall_conclusion: str = Field(
        default="",
        description=(
            "One medium-length paragraph on what the most recent quarterly "
            "Concall transcript revealed about management commentary, "
            "guidance, deal pipeline, capex plans, or sector outlook. Leave "
            "empty if the read_recent_concall_transcript tool was not called "
            "for this evaluation."
        ),
    )
    overall_summary: str = Field(
        default="",
        description=(
            "One medium-length paragraph giving the integrated forward view "
            "for the next 1-4 quarters, combining the announcements signal, "
            "the concall signal, and the agent's broader sector knowledge."
        ),
    )


class Observation(BaseModel):
    """A fundamental dimension the agent chose to analyse beyond the seven criteria."""

    topic: str = Field(
        description="What is being observed, e.g. 'Margin trend', 'Promoter pledging'."
    )
    finding: str = Field(description="The agent's specific finding on this topic.")
    sentiment: Literal["positive", "negative", "neutral"] = Field(
        description="Whether this finding is good, bad, or neutral for the business."
    )
    evidence: str = Field(
        description="Which numbers / data points support the finding."
    )


class AgentVerdict(BaseModel):
    """Structured verdict returned by the agent for one stock.

    Note on integer ranges:
    Anthropic's structured-output API (which OpenRouter forwards to when the
    user picks `anthropic/claude-*`) does NOT accept `minimum` / `maximum`
    properties on `integer` JSON Schema types. So we deliberately avoid the
    Pydantic `Field(ge=..., le=...)` shorthand for `rating` and
    `passed_criteria_count` — those would emit those properties into the
    schema and trigger a 400. Instead, the `@field_validator` decorators
    below run at parse time without polluting the JSON schema.

    Note on `mode`:
    The agent runs in one of two modes depending on whether the stock is
    in the user's curated universe:
    - `criteria` (Hemant Super 45 ∪ Nifty 100): apply the seven user-defined
      criteria + observations + forward outlook + holistic rating.
    - `insights_only` (every other stock): skip the criteria checklist,
      leave `criteria_breakdown` empty and `passed_criteria_count=0`. Still
      produce additional observations, forward outlook, summary, and the
      same 0-10 holistic rating.
    """

    symbol: str
    mode: Literal["criteria", "insights_only"] = Field(
        default="criteria",
        description=(
            "Which evaluation mode was used. 'criteria' fills the criteria "
            "breakdown; 'insights_only' leaves it empty because the stock is "
            "outside the user's curated universe."
        ),
    )
    rating: int = Field(
        description=(
            "Holistic 0-10 fundamental rating reflecting the agent's expert "
            "weighted judgment. NOT a count of passed criteria."
        ),
    )
    passed_criteria_count: int = Field(
        default=0,
        description=(
            "How many of the seven user-defined criteria the stock passes. "
            "Always 0 in insights_only mode (the criteria are not evaluated)."
        ),
    )
    total_criteria: int = Field(default=7)
    criteria_breakdown: list[CriterionResult] = Field(
        default_factory=list,
        description=(
            "One CriterionResult per user-defined criterion in 'criteria' "
            "mode. Empty list in 'insights_only' mode."
        ),
    )
    additional_observations: list[Observation] = Field(
        description=(
            "4-8 additional fundamental dimensions the agent chose to analyse, "
            "with positive/negative/neutral sentiment per observation."
        ),
    )
    summary_comments: str = Field(
        description="One medium-length paragraph, in plain English, explaining the rating."
    )
    forward_outlook: ForwardOutlook = Field(
        default_factory=ForwardOutlook,
        description=(
            "Three-part forward-looking view: announcements_conclusion, "
            "concall_conclusion, overall_summary. Empty subsections are "
            "acceptable when the underlying source data is not available "
            "(e.g. empty concall_conclusion when the transcript tool was "
            "not invoked). Distinct from criterion (e) which is pass/fail."
        ),
    )
    data_freshness: str = Field(
        description="ISO timestamp of when the underlying screener.in data was fetched."
    )
    model_used: str = Field(description="Which LLM produced this verdict.")

    @field_validator("rating")
    @classmethod
    def _validate_rating_range(cls, value: int) -> int:
        # Validation runs at parse time so a malformed LLM output still
        # raises, but the JSON schema we send to Anthropic stays clean of
        # `minimum` / `maximum` (which Anthropic rejects on integer types).
        if not 0 <= value <= 10:
            raise ValueError(
                f"rating must be between 0 and 10 inclusive, got {value}"
            )
        return value

    @field_validator("passed_criteria_count")
    @classmethod
    def _validate_passed_criteria_count(cls, value: int) -> int:
        if not 0 <= value <= 7:
            raise ValueError(
                f"passed_criteria_count must be between 0 and 7 inclusive, got {value}"
            )
        return value

    @field_validator("forward_outlook", mode="before")
    @classmethod
    def _migrate_legacy_string_outlook(cls, value: Any) -> Any:
        """Promote pre-Job-6 string verdicts into the new ForwardOutlook shape.

        Before this revision, `forward_outlook` was a free-form string. Cached
        verdicts on disk still carry that shape — if we changed the schema
        without this shim, every old JSON file would fail validation and the
        UI would discard the entire cached verdict. Putting the legacy string
        into `overall_summary` keeps existing caches readable while the new
        three-part shape becomes the default going forward.
        """
        if isinstance(value, str):
            return {"overall_summary": value}
        return value


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are a senior fundamental equity analyst with deep experience in Indian
listed companies. You think like Warren Buffett and Charlie Munger:
business quality, durable competitive advantages, capital allocation
discipline, balance-sheet integrity, and long-term earnings power — not
short-term price action.

You have access to TWO tools:
- `fetch_company_data(symbol)` — returns the structured screener.in
  snapshot for the stock (ratios, history tables, peer comparison, the
  most recent corporate announcements, and metadata for the most recent
  concall transcripts). Call this ONCE per evaluation.
- `read_recent_concall_transcript(symbol)` — downloads and returns the
  PLAIN TEXT of the most recent quarterly concall transcript. The text is
  large (~8-15K tokens). Call it ONLY when you genuinely need management
  commentary to (a) judge criterion (e) "Future growth prospects" or
  (b) write the `forward_outlook` field. Skip it for obvious cases where
  the structured data is already decisive. Returns "" if no transcript
  is available — in that case fall back to announcements + your sector
  knowledge.

When asked to evaluate a stock:

1. Call the `fetch_company_data` tool ONCE with the stock's symbol to
   obtain a screener.in snapshot. Do not call the tool more than once for
   the same symbol — re-reading the same data wastes tokens and time.

2. Apply these SEVEN user-defined criteria EXACTLY. For each, return one
   CriterionResult with the actual measured value, the threshold, a clear
   pass/fail, and your reasoning:

   a. Net Debt to Equity ratio < 0.2.
      Formula: (latest_debt - latest_cash_equivalents) /
      (latest_equity_capital + latest_reserves).
      If equity + reserves is non-positive, mark this criterion failed.

   b. Return on Capital Employed (ROCE) > 12% (or > 10% if this is a
      bank — judge from sector/industry/company name). Higher is better.

   c. Sales, Profits, AND EPS each at or near (within ~10% of) their
      all-time high. Use revenue_history, profit_history, eps_history.
      All three must qualify for the criterion to pass.

   d. Latest annual Net Profit > Rs. 200 crore.

   e. Future growth prospects look favourable. This is your QUALITATIVE
      judgment based on sales/profit/EPS trends, sector outlook, peer
      position, and the raw_text / pros_cons fields. Be specific in your
      reasoning.

   f. Business age >= 15 years. Use the about text or other clues. If you
      can find an incorporation/listing year >= 15 years ago, the
      criterion passes.

   g. Market leader by BOTH market cap AND profit within sector. Use the
      `peers` table. The stock must be in the top 1-3 of its peer set by
      both market cap and net profit.

3. BEYOND the seven criteria, identify 4-8 ADDITIONAL fundamental
   dimensions you consider most relevant for THIS specific company.

   ONE of these observations MUST be a Valuation observation. When
   forming it, ALWAYS compare the current P/E (`pe` field) to:
     a. The stock's own median P/E (`median_pe` field) if present, OR
     b. The industry P/E (`industry_pe` field) as a fallback.
   State the premium or discount in plain terms — e.g., "Trading at a
   22% premium to its 5-year median P/E of 18.3", or "Trading at a 30%
   discount to industry P/E of 25". Mark the sentiment as positive when
   the stock is cheap relative to its own history (or industry), negative
   when stretched, neutral when broadly in line. If neither median_pe
   nor industry_pe is available, say so explicitly and explain the
   limitation.

   Examples for the OTHER additional observations (pick what fits —
   don't be exhaustive, and don't repeat the seven):
   - Margin trend (operating / net) over 3-5 years
   - Capital allocation: dividend payout, buybacks, capex intensity
   - Working-capital quality: receivables, inventory days
   - Balance-sheet integrity: contingent liabilities, off-balance items
   - Shareholding stability: promoter pledging, change in promoter %
   - Valuation vs peers AND vs the company's own 5-year history
   - Governance signals: related-party transactions, auditor changes
   - Business moat / competitive position
   - Cyclical exposure / resilience to downturns
   Report each as one Observation with positive/negative/neutral sentiment
   and the specific evidence behind it.

4. Synthesize ONE holistic rating from 0-10 reflecting how strong this
   business is FUNDAMENTALLY. This rating is your weighted expert
   judgment, NOT a count of passed criteria. A company passing 7/7
   criteria but with deteriorating margins and pledged promoter shares
   may still rate 5/10. A company failing 2/7 (perhaps net debt slightly
   above 0.2) but with a dominant moat, clean governance, and strong
   capital allocation may rate 8/10. Reserve 9-10 for genuinely
   best-in-class compounders; 0-3 for businesses with serious red flags;
   4-6 for average; 7-8 for high-quality with minor concerns.

5. Write a `summary_comments` field — one medium-length paragraph, in
   plain English, that explains the rating. Mention both the strongest
   positives and the most important concerns.

6. Write a STRUCTURED `forward_outlook` object with THREE string
   subfields. Treat them as three short paragraphs that appear in this
   exact order in the rendered verdict:

   a. `announcements_conclusion` — one medium-length paragraph. What do
      the recent corporate Announcements tell you about the company's
      direction? Cite specific items where useful (e.g. "Won a $200M
      cloud modernization deal in Apr 2026 — confirms enterprise demand
      strength"). Leave empty only if the `announcements` array is
      genuinely empty in the source data.

   b. `concall_conclusion` — one medium-length paragraph. What did the
      most recent quarterly concall transcript reveal? Management
      guidance, deal pipeline, capex plans, sector commentary. If you
      did NOT call the `read_recent_concall_transcript` tool, leave this
      empty — never speculate about transcript contents you have not read.

   c. `overall_summary` — one medium-length paragraph. Integrate both
      subsections above plus your broader sector knowledge to project
      the next 1-4 quarters. This is the standalone analyst view on
      where the company is headed.

   Each subsection should be SPECIFIC to this company. Avoid generic
   sector commentary that could apply to any peer.

When you are ready, return your answer as a single AgentVerdict object.
Never write free-form text in your final answer — only the structured
schema.

Note on tool calls: this agent is configured for tool-calling models. The
default OpenRouter model (anthropic/claude-sonnet-4.5) supports this. If
you cannot call tools, abort and explain the constraint to the user."""


# Appended to the system prompt when the agent is invoked in insights-only
# mode. The base prompt above tells the agent to apply the seven criteria;
# this addendum overrides step 2 and adjusts the AgentVerdict requirements
# so an insights-only stock never gets a misleading 0/7 criteria score.
_INSIGHTS_ONLY_PROMPT_ADDENDUM = """\

============================================================
MODE OVERRIDE: insights_only
============================================================

This stock is OUTSIDE the user's curated universe (Hemant Super 45 +
Nifty 100), so the seven user-defined criteria DO NOT apply.

What changes:
- SKIP step 2 entirely. Do not evaluate any of the seven criteria.
- In your AgentVerdict, set `mode = "insights_only"`,
  `criteria_breakdown = []` (empty list), and `passed_criteria_count = 0`.

What stays the same:
- Steps 1, 3, 4, 5, 6 still apply. Fetch the data once, do 4-8
  additional observations (including the mandatory Valuation comparison),
  synthesize a holistic 0-10 rating from screener.in data alone, write
  a 3-6 sentence summary, and produce the three-part `forward_outlook`
  object (announcements_conclusion, concall_conclusion, overall_summary).

The rating is your standalone analyst judgment based on what the
fundamentals look like — there is no checklist anchor. Mention in
`summary_comments` that this is an insights-only assessment because the
stock is outside the curated universe."""


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
_DEFAULT_HEADERS = {
    # OpenRouter requests these headers for usage attribution.
    "HTTP-Referer": "https://github.com/DoRmAmMu1997/Streamlit-Scanner-App",
    "X-Title": "Hemant Scanner - Fundamental Check",
}


def _data_date_from_payload(data: dict[str, Any]) -> str:
    """Return YYYY-MM-DD of when the data was fetched, for the verdict cache key."""
    raw = data.get("fetched_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).date().isoformat()


class FundamentalAgent:
    """Per-stock LangChain agent backed by OpenRouter.

    One instance can be reused across many `check(...)` calls in a session.
    The agent is constructed lazily on first use so unit tests can pass a
    mock LLM via the `llm` constructor argument.
    """

    # Up to two tool calls (fetch + optional transcript) + reasoning + final
    # structuring pass leaves enough headroom without letting the loop run
    # away on a misbehaving model.
    MAX_TURNS = 6
    TEMPERATURE = 0.2

    def __init__(
        self,
        api_key: str,
        model: str,
        cache: FundamentalsCache | None = None,
        *,
        llm: ChatOpenAI | None = None,
    ) -> None:
        if not api_key:
            raise ValueError("FundamentalAgent: api_key is required.")
        self._api_key = api_key
        self._model = model
        self._cache = cache or FundamentalsCache()
        # The `llm` injection point lets tests supply a fake chat model
        # (FakeListChatModel, etc.) without hitting OpenRouter.
        self._llm = llm or ChatOpenAI(
            model=model,
            base_url=_OPENROUTER_BASE_URL,
            api_key=api_key,
            temperature=self.TEMPERATURE,
            default_headers=_DEFAULT_HEADERS,
            timeout=60,
            max_retries=2,
        )

    # ------------------------------------------------------------------
    # Tool
    # ------------------------------------------------------------------

    def _build_fetch_tool(self):
        """Return the LangChain Tool that the agent will call.

        Closes over `self._cache` so cache hits are transparent to the LLM.
        The agent does not need to know about caching; it just calls the
        tool and the helper decides whether to scrape or hit cache.
        """
        cache = self._cache
        # Force-refresh state is per-check, not per-tool-instance. We use a
        # mutable container so `check(force_refresh=True)` can flip it for
        # the duration of one call without rebuilding the tool.
        agent_state = self._state

        @tool
        def fetch_company_data_tool(symbol: str) -> str:
            """Fetch a screener.in snapshot for one NSE stock symbol.

            Returns a JSON string with valuation ratios, ROCE/ROE, the
            full annual and quarterly tables, peer comparison, shareholding,
            recent announcements, concall metadata, and a free-form text
            dump (about, pros, cons). Call this tool exactly ONCE per
            analysis.
            """
            normalized = (symbol or "").strip().upper()
            if not normalized:
                return json.dumps({"error": "Empty symbol"})

            if not agent_state["force_refresh"]:
                cached = cache.get_data(normalized)
                if cached is not None:
                    return json.dumps(cached, default=str)

            try:
                fresh = fetch_company_data(normalized)
            except ScreenerInFetchError as exc:
                return json.dumps({"error": str(exc)})
            cache.set_data(normalized, fresh)
            return json.dumps(fresh, default=str)

        return fetch_company_data_tool

    def _build_transcript_tool(self):
        """Return the LangChain Tool that downloads + reads the latest concall PDF.

        The tool reads the cached company data (set by `fetch_company_data_tool`)
        to find the `concalls` metadata, then downloads + extracts the most
        recent transcript's text. Returns an empty string when no transcript
        is available so the model can gracefully fall back to announcements
        + structured data for its forward outlook.
        """
        cache = self._cache

        @tool
        def read_recent_concall_transcript(symbol: str) -> str:
            """Fetch and return the plain text of the most recent quarterly
            concall transcript for one NSE stock. Use this when forming your
            forward outlook or when criterion (e) Future growth prospects is
            unclear from the structured data alone. The transcript is large
            (~8-15K tokens), so only call this when it will materially change
            your judgment. Returns "" if no transcript is available."""
            normalized = (symbol or "").strip().upper()
            if not normalized:
                return ""

            data = cache.get_data(normalized)
            if data is None:
                # The model called the transcript tool before fetch_company_data —
                # signal that with a short message so it knows to call fetch first.
                return (
                    "[no company data cached yet; call fetch_company_data first]"
                )

            concalls = data.get("concalls") or []
            try:
                return read_recent_concall_text(concalls) or ""
            except Exception:  # noqa: BLE001
                logger.warning(
                    "Concall transcript fetch failed for %s", normalized, exc_info=True
                )
                return ""

        return read_recent_concall_transcript

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    @property
    def _state(self) -> dict[str, Any]:
        """Lazily allocate the per-instance mutable state container."""
        state = getattr(self, "_mutable_state", None)
        if state is None:
            state = {"force_refresh": False}
            self._mutable_state = state
        return state

    def check(
        self,
        symbol: str,
        *,
        force_refresh: bool = False,
        mode: Literal["criteria", "insights_only"] = "criteria",
    ) -> AgentVerdict:
        """Run the agent and return a verdict, hitting the cache when possible.

        Pass `mode="insights_only"` for stocks outside the user's curated
        universe (Hemant Super 45 ∪ Nifty 100). In that mode the agent
        skips the 7-criteria checklist and produces observations + forward
        outlook + holistic rating only.
        """
        if not symbol or not str(symbol).strip():
            raise ValueError("FundamentalAgent.check: symbol must be a non-empty string")
        normalized = str(symbol).strip().upper()

        # Toggle force_refresh BEFORE building the tool so it sees the flag.
        self._state["force_refresh"] = bool(force_refresh)
        if force_refresh:
            self._cache.invalidate(normalized)

        # 1. Try the verdict cache first (free re-clicks on the same day).
        # The cache key includes the mode so a criteria-mode cached verdict
        # never gets returned for an insights-only request (and vice versa).
        if not force_refresh:
            data_for_key = self._cache.get_data(normalized)
            if data_for_key is not None:
                data_date = _data_date_from_payload(data_for_key)
                cache_key_model = f"{self._model}::{mode}"
                cached_verdict = self._cache.get_verdict(normalized, cache_key_model, data_date)
                if cached_verdict is not None:
                    return AgentVerdict.model_validate(cached_verdict)

        # 2. Run the tool-calling loop with the LLM. Two tools are bound:
        # the always-needed company-data fetch, and the optional concall
        # transcript reader the model can invoke when it needs management
        # commentary for the forward outlook.
        fetch_tool = self._build_fetch_tool()
        transcript_tool = self._build_transcript_tool()
        tools_by_name = {
            fetch_tool.name: fetch_tool,
            transcript_tool.name: transcript_tool,
        }
        llm_with_tools = self._llm.bind_tools(list(tools_by_name.values()))
        # The system prompt is mode-aware: insights_only appends an override
        # that tells the agent to skip the seven-criteria checklist while
        # still producing observations, rating, summary, and forward outlook.
        system_prompt = SYSTEM_PROMPT
        if mode == "insights_only":
            system_prompt = SYSTEM_PROMPT + _INSIGHTS_ONLY_PROMPT_ADDENDUM
        user_intent = (
            f"Evaluate the fundamentals of NSE stock '{normalized}'. "
            f"You are running in mode='{mode}'. Call fetch_company_data once. "
            "If the structured data and recent announcements are sufficient, "
            "skip the concall transcript tool; otherwise call it once to inform "
            "your forward outlook. Then produce the AgentVerdict per the system "
            "prompt."
        )
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_intent),
        ]

        for turn in range(self.MAX_TURNS):
            response = llm_with_tools.invoke(messages)
            messages.append(response)
            tool_calls = getattr(response, "tool_calls", None) or []
            if not tool_calls:
                # Model produced a final assistant message; break the loop
                # and structure that message into AgentVerdict below.
                break
            for call in tool_calls:
                # `call` shape from LangChain: {"name", "args", "id"}.
                tool_name = call.get("name", fetch_tool.name)
                target = tools_by_name.get(tool_name, fetch_tool)
                try:
                    tool_output = target.invoke(call["args"])
                except Exception as exc:  # noqa: BLE001
                    tool_output = json.dumps({"error": f"Tool {tool_name} failed: {exc}"})
                messages.append(
                    ToolMessage(
                        content=str(tool_output),
                        tool_call_id=call.get("id", ""),
                    )
                )
        else:
            logger.warning(
                "FundamentalAgent reached MAX_TURNS (%s) without a final answer for %s",
                self.MAX_TURNS,
                normalized,
            )

        # 3. Coerce the assistant's last narrative answer into the schema
        #    via a second LLM pass with `with_structured_output`.
        structuring_llm = self._llm.with_structured_output(AgentVerdict)
        # Re-use the conversation context (system prompt + fetched data +
        # the model's reasoning) so the structuring step has full evidence
        # without paying for another tool call.
        structuring_messages = messages + [
            HumanMessage(
                content=(
                    "Now output the final AgentVerdict JSON. The symbol is "
                    f"'{normalized}', the model is '{self._model}', and the "
                    f"mode is '{mode}'. Populate data_freshness from the "
                    "screener.in fetched_at field if available. "
                    + (
                        "In insights_only mode you MUST leave criteria_breakdown "
                        "as [] and passed_criteria_count as 0."
                        if mode == "insights_only"
                        else "In criteria mode populate criteria_breakdown with "
                        "all seven CriterionResult entries."
                    )
                )
            )
        ]
        verdict_raw = structuring_llm.invoke(structuring_messages)

        verdict = self._normalize_verdict(verdict_raw, symbol=normalized, mode=mode)

        # 4. Persist the verdict to the cache so the next click is instant.
        # Cache key includes the mode so criteria-mode and insights-only runs
        # for the same symbol do not collide.
        data_payload = self._cache.get_data(normalized)
        data_date = _data_date_from_payload(data_payload or {})
        try:
            self._cache.set_verdict(
                normalized,
                f"{self._model}::{mode}",
                data_date,
                verdict.model_dump(mode="json"),
            )
        except OSError:
            logger.warning("Could not write verdict cache for %s", normalized, exc_info=True)

        return verdict

    def _normalize_verdict(
        self,
        raw: Any,
        *,
        symbol: str,
        mode: Literal["criteria", "insights_only"] = "criteria",
    ) -> AgentVerdict:
        """Ensure the model's structured output is a valid AgentVerdict.

        `with_structured_output` typically returns the Pydantic instance
        directly, but if a custom or older model returns a dict (or the
        structuring failed), this helper coerces it sensibly. It also
        enforces the mode invariants — insights_only verdicts always have
        empty criteria + zero passed count, regardless of what the LLM
        emitted, so a misbehaving model can't pollute the UI.
        """
        if isinstance(raw, AgentVerdict):
            verdict = raw
        elif isinstance(raw, dict):
            verdict = AgentVerdict.model_validate(raw)
        else:
            raise RuntimeError(
                f"Agent returned an unexpected output type: {type(raw).__name__}"
            )

        # Stamp model + symbol + mode defensively — some models forget to fill them.
        updates: dict[str, Any] = {"mode": mode}
        if not verdict.symbol:
            updates["symbol"] = symbol
        if not verdict.model_used:
            updates["model_used"] = self._model
        if not verdict.data_freshness:
            updates["data_freshness"] = datetime.now(UTC).isoformat()
        # Enforce mode invariants: insights_only never carries criteria data.
        if mode == "insights_only":
            updates["criteria_breakdown"] = []
            updates["passed_criteria_count"] = 0
        verdict = verdict.model_copy(update=updates)
        return verdict
