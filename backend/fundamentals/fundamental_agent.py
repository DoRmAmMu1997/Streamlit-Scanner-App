from __future__ import annotations

"""Claude Agent SDK agent for the per-stock Check Fundamentals button.

How it works:
1. The Streamlit UI selects a row from any screener's results table and
   passes the symbol to `FundamentalAgent.check(symbol)`.
2. The agent runs on the **Claude Agent SDK** (`claude_agent_sdk`),
   authenticated through your **Claude subscription** via the bundled Claude
   CLI login. No API key is needed, and usage draws on your plan's monthly
   Agent SDK credit instead of per-token API billing.

   IMPORTANT: if `ANTHROPIC_API_KEY` is set in the environment, the SDK
   authenticates with that key and bills your API account instead of your
   subscription. Keep it unset for plan-based usage.
3. The agent is wired with two in-process SDK tools:
   - `fetch_company_data` — wraps
     `backend.fundamentals.screener_in_client.fetch_company_data` (with cache),
   - `read_recent_concall_transcript` — downloads + extracts the most recent
     quarterly concall transcript text.
4. A senior-analyst system prompt instructs the LLM to:
   - apply the seven user-defined criteria exactly,
   - add 4-8 additional fundamental observations of its choosing
     (margins, capital allocation, governance, etc.),
   - synthesize a HOLISTIC 0-10 rating (not a simple count),
   - end the run by emitting a single `AgentVerdict` JSON object.
5. We validate that final JSON against the `AgentVerdict` Pydantic schema and
   cache the verdict on disk per (symbol, model, mode, data_date) so re-clicks
   are free; the "Re-run analysis" button bypasses the cache.

Testing seam:
`FundamentalAgent` accepts an optional `runner=` callable so unit tests can
drive the agentic loop without spawning the CLI or hitting the network. The
real runner (`_default_run`) imports `claude_agent_sdk` lazily, so importing
this module does NOT require the SDK to be installed.
"""

import asyncio
import concurrent.futures
import json
import logging
import re
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field, field_validator

from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.fundamentals.pdf_reader import read_recent_concall_text
from backend.fundamentals.screener_in_client import (
    ScreenerInFetchError,
    fetch_company_data,
)


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors surfaced to the UI
# ---------------------------------------------------------------------------


class FundamentalsAgentError(RuntimeError):
    """Base class for Check Fundamentals failures meant to be shown to the user.

    Using a dedicated type (instead of a bare RuntimeError) lets the Streamlit
    layer tell *expected* conditions — like an exhausted plan limit — apart
    from genuine bugs, and render the right message for each.
    """

    # Stable, machine-readable code so callers and logs can branch on the
    # failure kind without parsing the human-readable message.
    code = "agent_error"


class FundamentalsUsageLimitError(FundamentalsAgentError):
    """Raised when the Claude plan's usage limit / Agent SDK credit is exhausted.

    This is an *expected* operational state, not a bug: the agent simply has to
    wait until the user's plan limit resets. The UI shows it as a gentle notice
    rather than a red error, and cached verdicts keep working in the meantime.
    """

    code = "usage_limit_reached"

    def __init__(
        self,
        message: str | None = None,
        *,
        resets_at: int | None = None,
        rate_limit_type: str | None = None,
    ) -> None:
        # `resets_at` is the Unix timestamp the CLI reports for when the limit
        # window reopens; `rate_limit_type` names the window (e.g. "five_hour").
        self.resets_at = resets_at
        self.rate_limit_type = rate_limit_type
        super().__init__(message or _format_usage_limit_message(resets_at))


# Substrings that mark a usage/limit failure in *unstructured* CLI error text.
# The structured signals (RateLimitEvent / AssistantMessage.error) are checked
# first; this list is only the fallback for raw process-error messages.
_USAGE_LIMIT_MARKERS = (
    "rate limit",
    "usage limit",
    "limit reached",
    "out of credit",
    "credit balance",
    "quota",
)


def _mentions_usage_limit(*texts: str | None) -> bool:
    """True if any of `texts` reads like a usage/credit-limit message."""
    haystack = " ".join(text for text in texts if text).lower()
    return any(marker in haystack for marker in _USAGE_LIMIT_MARKERS)


def _format_usage_limit_message(resets_at: int | None) -> str:
    """Build the user-facing message for an exhausted plan limit."""
    message = (
        "Your Claude plan's usage limit for the Agent SDK has been reached, so "
        "the Check Fundamentals agent is paused."
    )
    if resets_at:
        # Local time is friendlier than UTC for a desktop tool.
        when = datetime.fromtimestamp(resets_at).strftime("%Y-%m-%d %H:%M")
        message += f" It should work again after the limit resets (around {when})."
    else:
        message += " It will work again once your usage limit resets."
    return message + " Cached verdicts are still available in the meantime."


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
    Claude's structured-output handling does NOT accept `minimum` / `maximum`
    properties on `integer` JSON Schema types, and we also feed this schema's
    field list to the model in the prompt. So we deliberately avoid the
    Pydantic `Field(ge=..., le=...)` shorthand for `rating` and
    `passed_criteria_count` — those would emit those properties into the
    schema. Instead, the `@field_validator` decorators below run at parse time
    without polluting the JSON schema.

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
        # raises, but the JSON schema we feed the model stays clean of
        # `minimum` / `maximum` (which Claude rejects on integer types).
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

Note on tool calls: you must use the two tools above to gather data — you
cannot evaluate a stock from memory alone. Always call `fetch_company_data`
first. If a tool returns an error payload, surface that limitation honestly
in your reasoning rather than inventing numbers."""


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


# Appended LAST to the system prompt. The Claude Agent SDK has no
# `with_structured_output` equivalent, so we steer the model to emit a single
# JSON object as its final message and validate it ourselves with Pydantic.
_FINAL_OUTPUT_INSTRUCTION = """\

============================================================
FINAL OUTPUT FORMAT (STRICT)
============================================================

When your analysis is complete, your FINAL message must be a SINGLE JSON
object and NOTHING else — no prose before or after it, and no markdown code
fences. The object must contain exactly these keys:

- "symbol": string
- "mode": "criteria" or "insights_only"
- "rating": integer 0-10
- "passed_criteria_count": integer 0-7 (0 in insights_only mode)
- "total_criteria": integer (normally 7)
- "criteria_breakdown": array of objects, each with keys
  "name", "passed" (boolean), "measured_value", "threshold", "reasoning".
  Empty array [] in insights_only mode.
- "additional_observations": array of objects, each with keys
  "topic", "finding", "sentiment" ("positive"|"negative"|"neutral"),
  "evidence".
- "summary_comments": string
- "forward_outlook": object with keys "announcements_conclusion",
  "concall_conclusion", "overall_summary" (each a string; use "" when a
  subsection does not apply)
- "data_freshness": string (ISO timestamp from the screener.in fetched_at
  field, if available)
- "model_used": string

Emit ONLY this JSON object as your final answer."""


# ---------------------------------------------------------------------------
# Runner contract
# ---------------------------------------------------------------------------


@dataclass
class AgentRunResult:
    """Result of one agentic run: the final text plus optional reported cost.

    `text` is the model's final message, expected to contain the AgentVerdict
    JSON. `cost_usd` is the SDK-reported `total_cost_usd` when available (used
    only for logging / telemetry).
    """

    text: str
    cost_usd: float | None = None


# A runner drives one full agentic loop and returns the final message text.
# The default runner (`FundamentalAgent._default_run`) uses the Claude Agent
# SDK; tests inject a fake to avoid spawning the CLI.
RunnerFn = Callable[..., Awaitable[AgentRunResult]]


def _data_date_from_payload(data: dict[str, Any]) -> str:
    """Return YYYY-MM-DD of when the data was fetched, for the verdict cache key."""
    raw = data.get("fetched_at")
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw).date().isoformat()
        except ValueError:
            pass
    return datetime.now(UTC).date().isoformat()


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the AgentVerdict JSON object out of the model's final message.

    The model is instructed to emit ONLY a JSON object, but real models
    occasionally wrap it in a ```json fence or add a stray sentence. This
    helper is tolerant: it first looks for a fenced block, then falls back to
    the outermost {...} span. Returns None when nothing parses.
    """
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end < start:
            return None
        candidate = text[start : end + 1]

    try:
        parsed = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _build_user_prompt(symbol: str, mode: str, model: str) -> str:
    """Build the per-stock kickoff message for the agent."""
    return (
        f"Evaluate the fundamentals of NSE stock '{symbol}'. "
        f"You are running in mode='{mode}'. Call fetch_company_data once. "
        "If the structured data and recent announcements are sufficient, "
        "skip the concall transcript tool; otherwise call "
        "read_recent_concall_transcript once to inform your forward outlook. "
        f"Set model_used to '{model}'. Then output the final AgentVerdict JSON "
        "exactly per the FINAL OUTPUT FORMAT instructions."
    )


def _usage_limit_from_message(message: Any) -> FundamentalsUsageLimitError | None:
    """Detect an exhausted-plan signal in one Agent SDK stream message.

    Duck-typed (no `isinstance`) so it needs no SDK imports and stays trivially
    unit-testable. Two structured signals from the CLI mean the limit is hit:
    - a `RateLimitEvent` carries `rate_limit_info` with `status == "rejected"`
      (plus a `resets_at` timestamp),
    - an `AssistantMessage` carries `error == "rate_limit"` / `"billing_error"`
      when generation is refused for limit/billing reasons.
    Returns a ready-to-raise error, or None if this message is unremarkable.
    """
    info = getattr(message, "rate_limit_info", None)
    if info is not None and getattr(info, "status", None) == "rejected":
        return FundamentalsUsageLimitError(
            resets_at=getattr(info, "resets_at", None),
            rate_limit_type=getattr(info, "rate_limit_type", None),
        )
    if getattr(message, "error", None) in ("rate_limit", "billing_error"):
        return FundamentalsUsageLimitError()
    return None


def _describe_result_error(result: Any) -> str:
    """Build a readable message from an errored `ResultMessage`.

    Keeps the cause distinct from the "no parseable JSON" path so a genuine
    server-side failure doesn't masquerade as a formatting problem.
    """
    errors = getattr(result, "errors", None)
    if errors:
        detail = "; ".join(str(error) for error in errors)
    else:
        detail = str(getattr(result, "result", "") or "")[:300]
    subtype = getattr(result, "subtype", "") or "unknown"
    status = getattr(result, "api_error_status", None)
    status_part = f" (HTTP {status})" if status else ""
    return f"The Check Fundamentals agent run failed [{subtype}]{status_part}. {detail}".strip()


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class FundamentalAgent:
    """Per-stock agent backed by the Claude Agent SDK + your Claude subscription.

    One instance can be reused across many `check(...)` calls in a session.
    The agentic loop is driven by an injectable `runner` so unit tests can
    avoid spawning the CLI; production uses `_default_run`, which lazily
    imports `claude_agent_sdk`.
    """

    # Up to two tool calls (fetch + optional transcript) plus reasoning and a
    # final structuring turn leaves enough headroom without letting a
    # misbehaving model loop forever.
    MAX_TURNS = 8

    def __init__(
        self,
        model: str,
        cache: FundamentalsCache | None = None,
        *,
        runner: RunnerFn | None = None,
    ) -> None:
        if not model:
            raise ValueError("FundamentalAgent: model is required.")
        self._model = model
        self._cache = cache or FundamentalsCache()
        # `runner` injection lets tests drive the loop without the SDK/CLI.
        self._runner = runner
        # Per-instance mutable state. `force_refresh` is flipped per `check`
        # call so the fetch tool knows whether to bypass the data cache.
        self._mutable_state: dict[str, Any] = {"force_refresh": False}

    # ------------------------------------------------------------------
    # Tool implementations (plain, SDK-free, unit-testable)
    #
    # The real SDK tools (built in `_default_run`) are thin async wrappers
    # around these. Keeping the logic here means tests can exercise the tool
    # behaviour directly without importing claude_agent_sdk.
    # ------------------------------------------------------------------

    def _fetch_company_data_impl(self, symbol: str) -> str:
        """Fetch a screener.in snapshot (cache-aware). Returns a JSON string."""
        normalized = (symbol or "").strip().upper()
        if not normalized:
            return json.dumps({"error": "Empty symbol"})

        if not self._mutable_state["force_refresh"]:
            cached = self._cache.get_data(normalized)
            if cached is not None:
                return json.dumps(cached, default=str)

        try:
            fresh = fetch_company_data(normalized)
        except ScreenerInFetchError as exc:
            return json.dumps({"error": str(exc)})
        self._cache.set_data(normalized, fresh)
        return json.dumps(fresh, default=str)

    def _read_concall_impl(self, symbol: str) -> str:
        """Return the most recent concall transcript text for `symbol`, or "".

        Reads the cached company data (set by the fetch tool) to find the
        `concalls` metadata, then downloads + extracts the most recent
        transcript. Returns an empty string when no transcript is available so
        the model can fall back to announcements + structured data.
        """
        normalized = (symbol or "").strip().upper()
        if not normalized:
            return ""

        data = self._cache.get_data(normalized)
        if data is None:
            # The model called the transcript tool before fetch_company_data —
            # signal that so it knows to call fetch first.
            return "[no company data cached yet; call fetch_company_data first]"

        concalls = data.get("concalls") or []
        try:
            return read_recent_concall_text(concalls) or ""
        except Exception:  # noqa: BLE001
            logger.warning(
                "Concall transcript fetch failed for %s", normalized, exc_info=True
            )
            return ""

    # ------------------------------------------------------------------
    # Default runner (Claude Agent SDK)
    # ------------------------------------------------------------------

    async def _default_run(
        self,
        prompt: str,
        *,
        system_prompt: str,
        model: str,
        max_turns: int,
    ) -> AgentRunResult:
        """Run one agentic loop on the Claude Agent SDK and return final text.

        Imports `claude_agent_sdk` lazily so this module imports cleanly even
        when the SDK is not installed (e.g. in CI running only the unit tests).
        """
        try:
            from claude_agent_sdk import (  # type: ignore[import-not-found]
                AssistantMessage,
                ClaudeAgentOptions,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                create_sdk_mcp_server,
                query,
                tool,
            )
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise FundamentalsAgentError(
                "claude-agent-sdk is not installed. Run "
                "`pip install claude-agent-sdk` and sign in once with the "
                "bundled Claude CLI (using your Claude subscription) to enable "
                "the Check Fundamentals agent. Make sure ANTHROPIC_API_KEY is "
                "NOT set, or the SDK will bill your API account instead of your "
                "plan."
            ) from exc

        agent = self  # captured by the tool closures below

        @tool(
            "fetch_company_data",
            "Fetch a screener.in snapshot (valuation ratios, ROCE/ROE, annual "
            "and quarterly history, peer comparison, shareholding, recent "
            "announcements, and concall metadata) for one NSE stock symbol. "
            "Call exactly ONCE per analysis.",
            {"symbol": str},
        )
        async def _fetch_tool(args: dict[str, Any]) -> dict[str, Any]:
            text = await asyncio.to_thread(
                agent._fetch_company_data_impl, args.get("symbol", "")
            )
            return {"content": [{"type": "text", "text": text}]}

        @tool(
            "read_recent_concall_transcript",
            "Download and return the plain text of the most recent quarterly "
            "concall transcript (~8-15K tokens) for one NSE stock. Use only "
            "when you need management commentary for the forward outlook or "
            "criterion (e). Returns an empty string if no transcript exists.",
            {"symbol": str},
        )
        async def _concall_tool(args: dict[str, Any]) -> dict[str, Any]:
            text = await asyncio.to_thread(
                agent._read_concall_impl, args.get("symbol", "")
            )
            return {"content": [{"type": "text", "text": text}]}

        server = create_sdk_mcp_server(
            name="fundamentals",
            version="1.0.0",
            tools=[_fetch_tool, _concall_tool],
        )

        options = ClaudeAgentOptions(
            model=model,
            system_prompt=system_prompt,
            max_turns=max_turns,
            mcp_servers={"fundamentals": server},
            allowed_tools=[
                "mcp__fundamentals__fetch_company_data",
                "mcp__fundamentals__read_recent_concall_transcript",
            ],
            # "dontAsk" denies any tool that is NOT in allowed_tools, so the
            # agent can only ever call our two screener.in tools — never the
            # built-in filesystem/bash tools. This keeps a headless Streamlit
            # run locked down.
            permission_mode="dontAsk",
            # Do not load the user's Claude Code project/user settings or any
            # CLAUDE.md — this agent's behaviour comes entirely from our prompt.
            setting_sources=[],
        )

        final_text = ""
        cost_usd: float | None = None
        usage_limit: FundamentalsUsageLimitError | None = None
        result_message: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                # First structured sign of an exhausted plan limit wins; we keep
                # draining the stream so the run ends cleanly, then raise below.
                if usage_limit is None:
                    usage_limit = _usage_limit_from_message(message)
                if isinstance(message, ResultMessage):
                    result_message = message
                    cost_usd = message.total_cost_usd
                    if message.result:
                        final_text = message.result
                elif isinstance(message, AssistantMessage):
                    # Fallback: keep the last assistant text block in case the
                    # ResultMessage.result field comes back empty.
                    for block in getattr(message, "content", None) or []:
                        block_text = getattr(block, "text", None)
                        if block_text:
                            final_text = block_text
        except CLINotFoundError as exc:
            raise FundamentalsAgentError(
                "The bundled Claude CLI could not be found. Reinstall with "
                "`pip install --force-reinstall claude-agent-sdk`."
            ) from exc
        except ProcessError as exc:
            # A non-zero CLI exit can also mean the plan limit was hit; the
            # structured check above is preferred, this is the text fallback.
            if _mentions_usage_limit(str(exc), getattr(exc, "stderr", None)):
                raise FundamentalsUsageLimitError() from exc
            raise

        # Translate recognised conditions into typed errors the UI can react to.
        if usage_limit is not None:
            raise usage_limit
        if result_message is not None and result_message.is_error:
            if getattr(result_message, "api_error_status", None) == 429:
                raise FundamentalsUsageLimitError()
            raise FundamentalsAgentError(_describe_result_error(result_message))

        return AgentRunResult(text=final_text, cost_usd=cost_usd)

    # ------------------------------------------------------------------
    # Sync bridge
    # ------------------------------------------------------------------

    @staticmethod
    def _run_sync(coro: Awaitable[AgentRunResult]) -> AgentRunResult:
        """Run an async coroutine to completion from sync (Streamlit) code.

        Runs in a dedicated worker thread with its OWN event loop so we never
        collide with Streamlit/Tornado's running loop.

        On Windows we must build a ProactorEventLoop explicitly. The Agent SDK
        launches the Claude CLI as a subprocess, but Streamlit/Tornado installs
        the SelectorEventLoop policy on Windows, and that loop raises
        NotImplementedError for subprocesses. `asyncio.run()` would inherit
        that selector policy, so we create the right loop ourselves instead.
        """

        def _runner() -> AgentRunResult:
            if sys.platform == "win32":
                # ProactorEventLoop supports subprocess transports on Windows;
                # the default selector loop (installed by Tornado) does not.
                loop = asyncio.ProactorEventLoop()
            else:
                loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                return loop.run_until_complete(coro)
            finally:
                asyncio.set_event_loop(None)
                loop.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            return executor.submit(_runner).result()

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

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

        # Toggle force_refresh BEFORE running so the fetch tool sees the flag.
        self._mutable_state["force_refresh"] = bool(force_refresh)
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

        # 2. Build the mode-aware system prompt and run the agentic loop.
        system_prompt = SYSTEM_PROMPT
        if mode == "insights_only":
            system_prompt += _INSIGHTS_ONLY_PROMPT_ADDENDUM
        system_prompt += _FINAL_OUTPUT_INSTRUCTION

        prompt = _build_user_prompt(normalized, mode, self._model)
        runner = self._runner or self._default_run

        run_result = self._run_sync(
            runner(
                prompt,
                system_prompt=system_prompt,
                model=self._model,
                max_turns=self.MAX_TURNS,
            )
        )
        if run_result.cost_usd is not None:
            logger.info(
                "FundamentalAgent run for %s (%s) cost ~$%.4f",
                normalized,
                mode,
                run_result.cost_usd,
            )

        # 3. Validate the final JSON into AgentVerdict.
        verdict = self._parse_verdict(run_result.text, symbol=normalized, mode=mode)

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

    def _parse_verdict(
        self,
        text: str,
        *,
        symbol: str,
        mode: Literal["criteria", "insights_only"] = "criteria",
    ) -> AgentVerdict:
        """Extract + validate the AgentVerdict JSON from the agent's final text."""
        payload = _extract_json_object(text)
        if payload is None:
            preview = (text or "").strip()[:300] or "<empty response>"
            raise FundamentalsAgentError(
                "The agent did not return a parseable AgentVerdict JSON object. "
                f"Final message was: {preview}"
            )
        verdict = AgentVerdict.model_validate(payload)
        return self._normalize_verdict(verdict, symbol=symbol, mode=mode)

    def _normalize_verdict(
        self,
        raw: Any,
        *,
        symbol: str,
        mode: Literal["criteria", "insights_only"] = "criteria",
    ) -> AgentVerdict:
        """Ensure the model's structured output is a valid AgentVerdict.

        Coerces a dict into the Pydantic model if needed, then stamps any
        blank bookkeeping fields and enforces the mode invariants —
        insights_only verdicts always have empty criteria + zero passed count,
        regardless of what the LLM emitted, so a misbehaving model can't
        pollute the UI.
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
