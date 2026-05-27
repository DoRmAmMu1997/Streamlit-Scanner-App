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
from pydantic import BaseModel, Field

from backend.fundamentals.fundamentals_cache import FundamentalsCache
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
    """Structured verdict returned by the agent for one stock."""

    symbol: str
    rating: int = Field(
        ge=0,
        le=10,
        description=(
            "Holistic 0-10 fundamental rating reflecting the agent's expert "
            "weighted judgment. NOT a count of passed criteria."
        ),
    )
    passed_criteria_count: int = Field(
        ge=0,
        le=7,
        description="How many of the seven user-defined criteria the stock passes.",
    )
    total_criteria: int = Field(default=7)
    criteria_breakdown: list[CriterionResult] = Field(
        description="One CriterionResult per user-defined criterion (all seven)."
    )
    additional_observations: list[Observation] = Field(
        description=(
            "4-8 additional fundamental dimensions the agent chose to analyse, "
            "with positive/negative/neutral sentiment per observation."
        ),
    )
    summary_comments: str = Field(
        description="3-6 sentence plain-English explanation of the rating."
    )
    data_freshness: str = Field(
        description="ISO timestamp of when the underlying screener.in data was fetched."
    )
    model_used: str = Field(description="Which LLM produced this verdict.")


# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------


SYSTEM_PROMPT = """\
You are a senior fundamental equity analyst with deep experience in Indian
listed companies. You think like Warren Buffett and Charlie Munger:
business quality, durable competitive advantages, capital allocation
discipline, balance-sheet integrity, and long-term earnings power — not
short-term price action.

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
   Examples (pick what fits — don't be exhaustive, and don't repeat the
   seven):
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

5. Write a 3-6 sentence summary_comments field that explains the rating
   in plain English. Mention both the strongest positives and the most
   important concerns.

When you are ready, return your answer as a single AgentVerdict object.
Never write free-form text in your final answer — only the structured
schema.

Note on tool calls: this agent is configured for tool-calling models. The
default OpenRouter model (anthropic/claude-sonnet-4.5) supports this. If
you cannot call tools, abort and explain the constraint to the user."""


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

    MAX_TURNS = 4  # one fetch, one reasoning pass, one final structuring
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
            and a free-form text dump (about, pros, cons). Call this tool
            exactly ONCE per analysis.
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

    def check(self, symbol: str, *, force_refresh: bool = False) -> AgentVerdict:
        """Run the agent and return a verdict, hitting the cache when possible."""
        if not symbol or not str(symbol).strip():
            raise ValueError("FundamentalAgent.check: symbol must be a non-empty string")
        normalized = str(symbol).strip().upper()

        # Toggle force_refresh BEFORE building the tool so it sees the flag.
        self._state["force_refresh"] = bool(force_refresh)
        if force_refresh:
            self._cache.invalidate(normalized)

        # 1. Try the verdict cache first (free re-clicks on the same day).
        if not force_refresh:
            data_for_key = self._cache.get_data(normalized)
            if data_for_key is not None:
                data_date = _data_date_from_payload(data_for_key)
                cached_verdict = self._cache.get_verdict(normalized, self._model, data_date)
                if cached_verdict is not None:
                    return AgentVerdict.model_validate(cached_verdict)

        # 2. Run the tool-calling loop with the LLM.
        fetch_tool = self._build_fetch_tool()
        llm_with_tools = self._llm.bind_tools([fetch_tool])
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(
                content=(
                    f"Evaluate the fundamentals of NSE stock '{normalized}'. "
                    "Call the fetch tool exactly once, then produce the "
                    "AgentVerdict per the system prompt."
                )
            ),
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
                try:
                    tool_output = fetch_tool.invoke(call["args"])
                except Exception as exc:  # noqa: BLE001
                    tool_output = json.dumps({"error": f"Tool failed: {exc}"})
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
                    f"'{normalized}' and the model is '{self._model}'. "
                    "Populate data_freshness from the screener.in fetched_at "
                    "field if available."
                )
            )
        ]
        verdict_raw = structuring_llm.invoke(structuring_messages)

        verdict = self._normalize_verdict(verdict_raw, symbol=normalized)

        # 4. Persist the verdict to the cache so the next click is instant.
        data_payload = self._cache.get_data(normalized)
        data_date = _data_date_from_payload(data_payload or {})
        try:
            self._cache.set_verdict(
                normalized,
                self._model,
                data_date,
                verdict.model_dump(mode="json"),
            )
        except OSError:
            logger.warning("Could not write verdict cache for %s", normalized, exc_info=True)

        return verdict

    def _normalize_verdict(self, raw: Any, *, symbol: str) -> AgentVerdict:
        """Ensure the model's structured output is a valid AgentVerdict.

        `with_structured_output` typically returns the Pydantic instance
        directly, but if a custom or older model returns a dict (or the
        structuring failed), this helper coerces it sensibly.
        """
        if isinstance(raw, AgentVerdict):
            verdict = raw
        elif isinstance(raw, dict):
            verdict = AgentVerdict.model_validate(raw)
        else:
            raise RuntimeError(
                f"Agent returned an unexpected output type: {type(raw).__name__}"
            )

        # Stamp model + symbol defensively — some models forget to fill them.
        updates: dict[str, Any] = {}
        if not verdict.symbol:
            updates["symbol"] = symbol
        if not verdict.model_used:
            updates["model_used"] = self._model
        if not verdict.data_freshness:
            updates["data_freshness"] = datetime.now(UTC).isoformat()
        if updates:
            verdict = verdict.model_copy(update=updates)
        return verdict
