"""Claude Agent SDK agent for the Technical Analysis (AI) screener.

Mirrors `backend.fundamentals.fundamental_agent` in every structural respect —
same Claude Agent SDK, same Claude-subscription auth (no API key), same
injectable `runner=` testing seam, same "emit one JSON object as the final
message" contract validated with Pydantic, same on-disk verdict cache.

The one deliberate difference from the original technical agent: this version
does use a tiny in-process MCP tool server. The prompt gives Claude a compact
OHLC/level orientation, then the tools return deterministic market-structure,
level-map, and price-pattern facts for the selected stock.

How it works:
1. The `technical_analysis` screener runs a cheap pivot gate over Hemant Super
   45 ∪ Good 45 and sends only the few candidate stocks (close near a major
   support, or freshly broken above a major resistance) to `analyze(...)`.
2. `analyze` builds the OHLC-window + major-levels prompt, wires tools for this
   one stock, and runs one agentic pass; the model's final message is a single
   `TechnicalVerdict` JSON object.
3. The verdict is cached per (symbol, model, chart-context hash,
   latest-candle-date) so re-runs on unchanged data/settings are free.

Testing seam: `TechnicalAnalysisAgent` accepts an optional `runner=` callable so
unit tests drive the loop without spawning the Claude CLI. The real runner
(`_default_run`) imports `claude_agent_sdk` lazily, so importing this module
does NOT require the SDK to be installed.

Subscription billing note: keep `ANTHROPIC_API_KEY` UNSET so the SDK draws on
your Claude plan's Agent SDK credit instead of per-token API billing.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hashlib
import json
import logging
import re
import sys
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any, Literal

import pandas as pd
from pydantic import BaseModel, Field, field_validator

from backend.fundamentals.fundamental_agent import (
    AgentRunResult,
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
    _mentions_usage_limit,
    _usage_limit_from_message,
)
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.technical.knowledge import FINAL_OUTPUT_INSTRUCTION, build_system_prompt
from backend.technical.tools import (
    TechnicalToolContext,
    build_technical_mcp_server,
    resolve_params,
)

logger = logging.getLogger(__name__)


# How many recent daily candles to hand the model. Enough to contain a full
# cup-and-handle or inverse-H&S formation without flooding the context window.
_OHLC_WINDOW_BARS = 250


# A runner drives one full agentic loop and returns the final message text.
# The default runner (`TechnicalAnalysisAgent._default_run`) uses the Claude
# Agent SDK; tests inject a fake to avoid spawning the CLI. The signature
# matches the fundamentals runner so the same `AgentRunResult` shape is reused.
RunnerFn = Callable[..., Awaitable[AgentRunResult]]


def _technical_context_hash(
    candles: pd.DataFrame,
    levels: list[dict[str, Any]],
    params: dict[str, Any] | None = None,
) -> str:
    """Return a stable hash for the chart facts the model reasons about.

    Cache safety depends on EVERYTHING that can change the verdict, not only the
    latest candle date. Three things feed this digest:
    - the prompt OHLC window,
    - the major support/resistance levels (user-tuned pivot settings can shift
      these on the same day), and
    - the detector settings (`params`) — because the agent's tools compute Fair
      Value Gaps, order blocks, structure, etc. deterministically from these, a
      changed setting can change the tool answers and therefore the verdict.

    The detectors are pure functions of (candles, params), so hashing those keeps
    a cached verdict reproducible.
    """
    window = candles.tail(_OHLC_WINDOW_BARS).copy() if not candles.empty else candles
    candle_records: list[dict[str, Any]] = []
    for row in window.to_dict("records"):
        candle_records.append(
            {
                key: (value.isoformat() if hasattr(value, "isoformat") else value)
                for key, value in row.items()
                if key in {"timestamp", "open", "high", "low", "close", "volume"}
            }
        )
    payload = {
        "candles": candle_records,
        "levels": levels,
        # Resolve to the full settings dict so omitted keys (which fall back to
        # defaults) hash identically whether or not the caller passed them.
        "params": resolve_params(params),
    }
    raw = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Pydantic schema (the agent's structured output)
# ---------------------------------------------------------------------------


# The mutually-exclusive BULLISH setups the agent can report (the screener is
# long-only). "none" means no qualifying setup — the screener drops the stock.
# Bearish structures are surfaced via `caution`, never as a `pattern`.
PatternName = Literal[
    "cup_and_handle",
    "inverse_head_and_shoulders",
    "at_support",
    "double_bottom",
    "fair_value_gap",
    "order_block",
    "none",
]


class RelevantLevel(BaseModel):
    """One support/resistance level the agent judged relevant to its verdict.

    Surfacing these answers the user's question — *which* levels are relevant —
    in the structured output instead of burying it in prose. `relevance` is the
    agent's qualitative call (high/medium/low); the deterministic numeric score
    is available separately from the `level_map` tool / `backend.indicators.
    rank_levels`.
    """

    price: float
    role: Literal["support", "resistance"] = "support"
    relevance: Literal["high", "medium", "low"] = "medium"
    why: str = Field(default="", description="One line on why this level matters now.")


class TechnicalVerdict(BaseModel):
    """Structured verdict returned by the technical-analysis agent for one stock.

    Note on the integer range: like `AgentVerdict`, we validate `confidence`
    with a `@field_validator` instead of `Field(ge=..., le=...)` so the JSON
    schema we describe to the model in the prompt stays free of `minimum` /
    `maximum` (which Claude rejects on integer types).
    """

    symbol: str
    pattern: PatternName = Field(
        description=(
            "Which setup is present: a breakout-confirmed cup-and-handle, a "
            "breakout-confirmed inverse head-and-shoulders, price at a major "
            "support, confirmed double bottom, bullish Fair Value Gap, bullish "
            "order block, or 'none' if no qualifying setup exists."
        )
    )
    confirmed: bool = Field(
        default=False,
        description=(
            "True only when the trigger has ALREADY happened: breakout close for "
            "classical/double-bottom patterns, current hold/reaction for support "
            "or demand-zone setups. Always False when pattern is 'none'."
        ),
    )
    key_levels: list[float] = Field(
        default_factory=list,
        description=(
            "The price levels that define the setup — e.g. the neckline / rim "
            "breakout price, or the support level price. 1-3 values."
        ),
    )
    confidence: int = Field(
        description="How confident the agent is in this read, 0-10 (10 = textbook)."
    )
    trend: Literal["uptrend", "downtrend", "sideways"] = Field(
        default="sideways",
        description="The daily market-structure trend this read is set against.",
    )
    htf_alignment: Literal["aligned", "against", "neutral"] = Field(
        default="neutral",
        description=(
            "Whether the weekly (higher-timeframe) trend supports the bullish "
            "setup: 'aligned', 'against', or 'neutral'."
        ),
    )
    relevant_levels: list[RelevantLevel] = Field(
        default_factory=list,
        description="The support/resistance levels the agent is keying on (0-4 of them).",
    )
    caution: str = Field(
        default="",
        description=(
            "Bearish or structural warnings that temper the bullish read "
            "(e.g. overhead resistance, downtrend, bearish CHoCH); '' if none."
        ),
    )
    reasoning: str = Field(
        description=(
            "2-4 sentences explaining the read: the structure seen, the level(s) "
            "involved, and (for patterns) why the breakout is or isn't confirmed."
        )
    )
    signal_date: str = Field(
        default="",
        description="Timestamp (YYYY-MM-DD) of the latest candle this read is based on.",
    )
    model_used: str = Field(default="", description="Which LLM produced this verdict.")

    @field_validator("confidence")
    @classmethod
    def _validate_confidence_range(cls, value: int) -> int:
        # Parse-time validation keeps the prompt's schema clean of min/max.
        if not 0 <= value <= 10:
            raise ValueError(f"confidence must be between 0 and 10 inclusive, got {value}")
        return value


# ---------------------------------------------------------------------------
# System prompt (assembled from the externalized knowledge module)
# ---------------------------------------------------------------------------


# The agent's expertise now lives in `backend/technical/knowledge.py` so it can be
# read, extended, and reviewed as prose. We compose it once at import time.
# `analyze` appends `_FINAL_OUTPUT_INSTRUCTION` (the strict JSON contract) last,
# exactly as the original inline prompt did. The names are kept so the rest of
# this module (and its tests) are unchanged.
SYSTEM_PROMPT = build_system_prompt()
_FINAL_OUTPUT_INSTRUCTION = FINAL_OUTPUT_INSTRUCTION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Pull the TechnicalVerdict JSON object out of the model's final message.

    Tolerant of a stray ```json fence or a leading sentence: it looks for a
    fenced block first, then falls back to the outermost {...} span. Returns
    None when nothing parses. (Mirrors the fundamentals agent's extractor; kept
    local so the two agents stay independent.)
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


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------


class TechnicalAnalysisAgent:
    """Per-stock technical agent backed by the Claude Agent SDK + Claude subscription.

    One instance can be reused across many `analyze(...)` calls in a session.
    The agentic loop is driven by an injectable `runner` so unit tests avoid
    spawning the CLI; production uses `_default_run`, which lazily imports
    `claude_agent_sdk`.
    """

    # The agent now calls up to three analysis tools, then writes its JSON, so it
    # needs a few more turns than the old tool-free agent. The ceiling still
    # guards against a runaway loop (three tool calls + reasoning + final answer).
    MAX_TURNS = 8

    def __init__(
        self,
        model: str,
        cache: FundamentalsCache | None = None,
        *,
        runner: RunnerFn | None = None,
        fast_mode: bool = False,
    ) -> None:
        if not model:
            raise ValueError("TechnicalAnalysisAgent: model is required.")
        self._model = model
        # Reuse the same on-disk cache as the fundamentals agent. The verdict
        # key embeds "::technical" so the two agents never collide on a symbol.
        self._cache = cache or FundamentalsCache()
        # `runner` injection lets tests drive the loop without the SDK/CLI.
        self._runner = runner
        # Fast mode disables extended thinking on the SDK call for lower latency.
        self._fast_mode = bool(fast_mode)

    def _cache_model_key(
        self,
        candles: pd.DataFrame,
        levels: list[dict[str, Any]],
        params: dict[str, Any] | None = None,
    ) -> str:
        """Build the cache namespace for one technical-analysis prompt.

        The date still lives in the cache filename via `data_date`; this model
        key adds a digest of the chart context (candles + levels + detector
        `params`) so a changed setup cannot reuse an older verdict from the same
        candle date. Thorough mode keeps the historical key shape for cache
        continuity; fast mode adds a suffix so lower-latency verdicts never
        masquerade as thorough ones.
        """
        speed_part = "::fast" if self._fast_mode else ""
        digest = _technical_context_hash(candles, levels, params)
        return f"{self._model}::technical{speed_part}::{digest}"

    # ------------------------------------------------------------------
    # Prompt construction
    # ------------------------------------------------------------------

    @staticmethod
    def _ohlc_csv(candles: pd.DataFrame, window: int = _OHLC_WINDOW_BARS) -> str:
        """Render the last `window` candles as terse 'date,o,h,l,c' CSV lines."""
        recent = candles.tail(window)
        lines = ["date,open,high,low,close"]
        for row in recent.itertuples(index=False):
            timestamp = getattr(row, "timestamp", "")
            date_str = str(timestamp)[:10] if timestamp is not None else ""
            lines.append(
                f"{date_str},{float(row.open):.2f},{float(row.high):.2f},"
                f"{float(row.low):.2f},{float(row.close):.2f}"
            )
        return "\n".join(lines)

    @staticmethod
    def _levels_text(levels: list[dict[str, Any]]) -> str:
        """Render major levels as readable 'price (kind, N touches)' lines."""
        if not levels:
            return "(no major levels detected)"
        return "\n".join(
            f"- {float(level['price']):.2f} "
            f"({level.get('kind', '?')}, {int(level.get('touches', 0))} touches)"
            for level in levels
        )

    def _build_user_prompt(
        self, symbol: str, candles: pd.DataFrame, levels: list[dict[str, Any]]
    ) -> str:
        """Build the per-stock kickoff message for the agent.

        The candle CSV and a quick level summary are included for orientation, but
        the precise analysis comes from the tools (`level_map`, `price_patterns`,
        `market_structure`) — the prompt steers the agent to call them rather than
        eyeball the CSV.
        """
        return (
            f"Stock: {symbol}\n\n"
            f"Quick view of major support/resistance (full detail via level_map):\n"
            f"{self._levels_text(levels)}\n\n"
            f"Recent daily candles (CSV, for orientation):\n{self._ohlc_csv(candles)}\n\n"
            "Call your tools to gather the facts: market_structure (trend + "
            "BOS/CHoCH on daily and weekly), level_map (relevance-scored "
            "support/resistance), and price_patterns (Fair Value Gaps, double "
            "bottom/top, order blocks). Then decide whether ONE bullish setup is "
            "present and confirmed as of the latest candle, judge which levels are "
            "relevant, and gauge weekly alignment. "
            f"Set model_used to '{self._model}'. Finally output the TechnicalVerdict "
            "JSON exactly per the FINAL OUTPUT FORMAT instructions."
        )

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
        tool_context: TechnicalToolContext | None = None,
    ) -> AgentRunResult:
        """Run one agentic loop on the Claude Agent SDK and return final text.

        Imports `claude_agent_sdk` lazily so this module imports cleanly even
        when the SDK is not installed (e.g. in CI running only the unit tests).
        When `tool_context` is provided, an in-process MCP server exposing the
        three technical tools is registered and the agent is restricted to those
        tools only (see `backend/technical/tools.py`).
        """
        try:
            import claude_agent_sdk as claude_sdk  # type: ignore[import-not-found, unused-ignore]
            from claude_agent_sdk import (  # type: ignore[import-not-found, unused-ignore]
                AssistantMessage,
                ClaudeAgentOptions,
                CLINotFoundError,
                ProcessError,
                ResultMessage,
                query,
            )
        except ImportError as exc:  # pragma: no cover - environment dependent
            raise FundamentalsAgentError(
                "claude-agent-sdk is not installed. Run "
                "`pip install claude-agent-sdk` and sign in once with the "
                "bundled Claude CLI (using your Claude subscription) to enable "
                "the Technical Analysis agent. Make sure ANTHROPIC_API_KEY is "
                "NOT set, or the SDK will bill your API account instead of your "
                "plan."
            ) from exc
        ThinkingConfigDisabled = getattr(claude_sdk, "ThinkingConfigDisabled", None)

        # Build the in-process tool server for THIS stock. The handlers close over
        # `tool_context`, so parallel confirmations never share mutable state.
        mcp_servers: dict[str, Any] = {}
        allowed_tools: list[str] = []
        if tool_context is not None:
            mcp_servers, allowed_tools = build_technical_mcp_server(tool_context)

        options_kwargs: dict[str, Any] = {
            "model": model,
            "system_prompt": system_prompt,
            "max_turns": max_turns,
            # Expose ONLY our three technical tools. With "dontAsk", any tool not
            # in allowed_tools is denied, so the agent can never reach the
            # built-in filesystem/bash tools in a headless Streamlit run.
            "mcp_servers": mcp_servers,
            "allowed_tools": allowed_tools,
            "permission_mode": "dontAsk",
            "setting_sources": [],
        }
        if self._fast_mode:
            if ThinkingConfigDisabled is None:
                # Older Agent SDK builds may not expose the thinking toggle yet.
                # Fast mode only improves latency, so the safe fallback is to
                # run with the SDK's default thinking behavior and log why.
                logger.warning(
                    "Agent fast mode was requested, but claude-agent-sdk does not "
                    "expose ThinkingConfigDisabled; using default thinking behavior."
                )
            else:
                # Fast mode disables extended thinking for lower latency; pattern
                # detection from the OHLC window is a single bounded pass.
                options_kwargs["thinking"] = ThinkingConfigDisabled()
        options = ClaudeAgentOptions(**options_kwargs)

        final_text = ""
        cost_usd: float | None = None
        usage_limit: FundamentalsUsageLimitError | None = None
        result_message: ResultMessage | None = None
        try:
            async for message in query(prompt=prompt, options=options):
                if usage_limit is None:
                    usage_limit = _usage_limit_from_message(message)
                if isinstance(message, ResultMessage):
                    result_message = message
                    cost_usd = message.total_cost_usd
                    if message.result:
                        final_text = message.result
                elif isinstance(message, AssistantMessage):
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
            if _mentions_usage_limit(str(exc), getattr(exc, "stderr", None)):
                raise FundamentalsUsageLimitError() from exc
            raise

        if usage_limit is not None:
            raise usage_limit
        if result_message is not None and result_message.is_error:
            if getattr(result_message, "api_error_status", None) == 429:
                raise FundamentalsUsageLimitError()
            raise FundamentalsAgentError(
                f"The Technical Analysis agent run failed: "
                f"{str(getattr(result_message, 'result', '') or '')[:300]}".strip()
            )

        return AgentRunResult(text=final_text, cost_usd=cost_usd)

    # ------------------------------------------------------------------
    # Sync bridge
    # ------------------------------------------------------------------

    @staticmethod
    def _run_sync(coro: Awaitable[AgentRunResult]) -> AgentRunResult:
        """Run an async coroutine to completion from sync (Streamlit) code.

        Runs in a dedicated worker thread with its OWN event loop so we never
        collide with Streamlit/Tornado's running loop. On Windows we build a
        ProactorEventLoop explicitly because the Agent SDK launches the Claude
        CLI as a subprocess, which the default selector loop cannot do.
        (Identical to the fundamentals agent's bridge.)
        """

        def _runner() -> AgentRunResult:
            if sys.platform == "win32":
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

    def analyze(
        self,
        symbol: str,
        candles: pd.DataFrame,
        levels: list[dict[str, Any]],
        *,
        params: dict[str, Any] | None = None,
        force_refresh: bool = False,
    ) -> TechnicalVerdict:
        """Return a technical verdict for `symbol`, hitting the cache when possible.

        `candles` must be a prepared OHLC frame (oldest→newest, with a
        `timestamp` column). `levels` is the output of
        `backend.indicators.major_levels`. `params` are the detector settings the
        tools use (FVG/double/order-block/structure/relevance knobs); omitted keys
        fall back to `tools.DEFAULT_TOOL_PARAMS`. The verdict is cached per
        (symbol, model, context-hash, latest-candle-date) — and the context hash
        includes `params` — so re-runs on unchanged data/settings are free;
        `force_refresh=True` bypasses and overwrites the cache.
        """
        if not symbol or not str(symbol).strip():
            raise ValueError("TechnicalAnalysisAgent.analyze: symbol must be non-empty")
        normalized = str(symbol).strip().upper()

        # The signal date is the latest candle's date. Using it as the cache
        # "data_date" means a new candle automatically invalidates the verdict.
        signal_date = ""
        if not candles.empty and "timestamp" in candles.columns:
            signal_date = str(candles.iloc[-1]["timestamp"])[:10]
        data_date = signal_date or datetime.now(UTC).date().isoformat()
        cache_key_model = self._cache_model_key(candles, levels, params)

        # 1. Verdict cache: free re-runs on the same candle date.
        if not force_refresh:
            cached = self._cache.get_verdict(normalized, cache_key_model, data_date)
            if cached is not None:
                return TechnicalVerdict.model_validate(cached)

        # 2. Run the agentic loop. The agent calls the technical tools (built from
        #    this per-call context) to gather facts, then emits the final JSON.
        system_prompt = SYSTEM_PROMPT + _FINAL_OUTPUT_INSTRUCTION
        prompt = self._build_user_prompt(normalized, candles, levels)
        tool_context = TechnicalToolContext.build(normalized, candles, levels, params)
        runner = self._runner or self._default_run

        run_result = self._run_sync(
            runner(
                prompt,
                system_prompt=system_prompt,
                model=self._model,
                max_turns=self.MAX_TURNS,
                tool_context=tool_context,
            )
        )
        if run_result.cost_usd is not None:
            logger.info(
                "TechnicalAnalysisAgent run for %s cost ~$%.4f",
                normalized,
                run_result.cost_usd,
            )

        # 3. Validate the final JSON into TechnicalVerdict.
        verdict = self._parse_verdict(run_result.text, symbol=normalized, signal_date=signal_date)

        # 4. Persist so the next run on the same candle date is instant.
        try:
            self._cache.set_verdict(
                normalized, cache_key_model, data_date, verdict.model_dump(mode="json")
            )
        except OSError:
            logger.warning(
                "Could not write technical verdict cache for %s", normalized, exc_info=True
            )

        return verdict

    def _parse_verdict(
        self, text: str, *, symbol: str, signal_date: str
    ) -> TechnicalVerdict:
        """Extract + validate the TechnicalVerdict JSON from the agent's final text."""
        payload = _extract_json_object(text)
        if payload is None:
            preview = (text or "").strip()[:300] or "<empty response>"
            raise FundamentalsAgentError(
                "The technical agent did not return a parseable TechnicalVerdict "
                f"JSON object. Final message was: {preview}"
            )
        verdict = TechnicalVerdict.model_validate(payload)
        return self._normalize_verdict(verdict, symbol=symbol, signal_date=signal_date)

    def _normalize_verdict(
        self, raw: Any, *, symbol: str, signal_date: str
    ) -> TechnicalVerdict:
        """Coerce the model output into a valid TechnicalVerdict and stamp fields."""
        if isinstance(raw, TechnicalVerdict):
            verdict = raw
        elif isinstance(raw, dict):
            verdict = TechnicalVerdict.model_validate(raw)
        else:
            raise RuntimeError(
                f"Technical agent returned an unexpected output type: {type(raw).__name__}"
            )

        # The requested symbol is the trusted source of truth. Claude sees only
        # this stock's tool context, but if it emits a stale or mismatched ticker
        # in the JSON, normalize it here before caching or returning the verdict.
        updates: dict[str, Any] = {"symbol": symbol}
        if not verdict.model_used:
            updates["model_used"] = self._model
        if not verdict.signal_date:
            updates["signal_date"] = signal_date
        # Invariant: "none" can never be a confirmed signal.
        if verdict.pattern == "none" and verdict.confirmed:
            updates["confirmed"] = False
        if updates:
            verdict = verdict.model_copy(update=updates)
        return verdict
