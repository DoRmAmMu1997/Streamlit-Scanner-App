"""MCP tool server for the Technical Analysis agent.

Why the agent gets tools (beginner note)
----------------------------------------
The user asked for the Claude agent to be given *tools* rather than having every
fact pre-chewed into its prompt. So instead of eyeballing a candle dump, the agent
calls these three deterministic tools to fetch precise, structured analysis of the
one stock it is looking at:

- ``level_map``        → relevance-scored support/resistance (daily + weekly).
- ``price_patterns``   → unfilled Fair Value Gaps, double bottom/top, order blocks.
- ``market_structure`` → daily + weekly trend and the latest BOS / CHoCH.

How this stays cache-safe
-------------------------
Every tool result is a pure function of the stock's candles plus the detector
settings — there is no randomness and no network call. The agent's per-day verdict
cache therefore stays valid as long as the cache key covers the candles and those
settings (see `technical_agent._technical_context_hash`, which folds in `params`).

Concurrency
-----------
The screener confirms several candidates in parallel on a shared agent instance.
To stay race-free, the tool handlers close over a **per-call** `TechnicalToolContext`
(built fresh inside each `analyze(...)`), never over mutable state on the agent.

SDK import is lazy
------------------
`claude_agent_sdk` is imported INSIDE `build_technical_mcp_server` so this module
(and the whole `backend.technical` package) imports cleanly even when the SDK is
not installed — only actually building the server needs it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pandas as pd

from backend.indicators import major_levels, rank_levels, resample_to_weekly
from backend.technical.patterns import (
    detect_double_patterns,
    detect_fair_value_gaps,
    detect_market_structure,
    detect_order_blocks,
)

# The in-process MCP server name and the fully-qualified tool names the agent is
# allowed to call. The "mcp__<server>__<tool>" form is how the Claude Agent SDK
# namespaces in-process tools (mirrors the fundamentals agent's allowed_tools).
SERVER_NAME = "technical"
TOOL_NAMES = [
    f"mcp__{SERVER_NAME}__level_map",
    f"mcp__{SERVER_NAME}__price_patterns",
    f"mcp__{SERVER_NAME}__market_structure",
]


# Default detector settings. The screener can override any of these via the
# `params` it passes to `analyze`; anything it omits falls back to these. Keeping
# them here (one place) means the tools and the gate share the same definitions.
DEFAULT_TOOL_PARAMS: dict[str, Any] = {
    # Swing-pivot width used by double patterns, order blocks, and structure.
    "swing_left": 5,
    "swing_right": 5,
    # Fair Value Gaps.
    "fvg_min_gap_pct": 0.3,
    "fvg_lookback_bars": 250,
    # Double top/bottom.
    "double_tolerance_pct": 3.0,
    "double_lookback_bars": 250,
    # Order blocks.
    "ob_lookback_bars": 250,
    # Market structure.
    "structure_lookback_bars": 400,
    # Level relevance scoring.
    "level_band_pct": 1.0,
    "level_recency_halflife_bars": 120,
    # Weekly major-level detection (reuses the daily pivot/cluster knobs).
    "pivot_left": 5,
    "pivot_right": 5,
    "cluster_pct": 2.0,
    "min_touches": 3,
    "weekly_enabled": True,
    # How many items each tool returns (keep the agent's context small).
    "max_levels": 6,
    "max_weekly_levels": 4,
    "max_patterns": 5,
}


def resolve_params(params: dict | None) -> dict[str, Any]:
    """Merge caller-supplied settings over the defaults (caller wins)."""
    merged = dict(DEFAULT_TOOL_PARAMS)
    if params:
        merged.update(params)
    return merged


@dataclass
class TechnicalToolContext:
    """Everything the three tools need to answer questions about ONE stock.

    Built once per `analyze(...)` call from that stock's candles + major levels +
    detector settings, then captured by the tool closures. Because it is created
    fresh for each call (and never mutated), parallel confirmations on a shared
    agent never step on each other.
    """

    symbol: str
    daily: pd.DataFrame
    weekly: pd.DataFrame
    daily_levels: list[dict]
    weekly_levels: list[dict]
    params: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def build(
        cls,
        symbol: str,
        candles: pd.DataFrame,
        levels: list[dict],
        params: dict | None = None,
    ) -> TechnicalToolContext:
        """Build the per-call context — the intended way to create one.

        Always construct a ``TechnicalToolContext`` through this factory rather than
        the dataclass directly: it performs the two non-obvious assembly steps the
        tools depend on. (1) It aggregates the daily candles into a weekly frame
        (``resample_to_weekly`` — no new data source, just a coarser view). (2) It
        relevance-*ranks* the support/resistance levels on BOTH timeframes via
        ``rank_levels``, decaying weekly recency ~5x faster because roughly five
        daily bars pass per week. ``params`` overrides ``DEFAULT_TOOL_PARAMS``
        (caller wins); pass ``None`` to use the defaults.
        """
        cfg = resolve_params(params)

        # Weekly is just the daily candles aggregated — no new data source.
        weekly = (
            resample_to_weekly(candles)
            if cfg.get("weekly_enabled", True)
            else pd.DataFrame()
        )

        daily_levels = rank_levels(
            candles,
            levels,
            band_pct=float(cfg["level_band_pct"]),
            recency_halflife_bars=int(cfg["level_recency_halflife_bars"]),
        )

        weekly_levels: list[dict] = []
        if weekly is not None and not weekly.empty:
            weekly_major = major_levels(
                weekly,
                left=int(cfg["pivot_left"]),
                right=int(cfg["pivot_right"]),
                cluster_pct=float(cfg["cluster_pct"]),
                min_touches=int(cfg["min_touches"]),
            )
            # Weeks pass ~5x slower than days, so decay recency ~5x faster.
            weekly_halflife = max(1, int(cfg["level_recency_halflife_bars"]) // 5)
            weekly_levels = rank_levels(
                weekly,
                weekly_major,
                band_pct=float(cfg["level_band_pct"]),
                recency_halflife_bars=weekly_halflife,
            )

        return cls(
            symbol=symbol,
            daily=candles,
            weekly=weekly if weekly is not None else pd.DataFrame(),
            daily_levels=daily_levels,
            weekly_levels=weekly_levels,
            params=cfg,
        )

    # ------------------------------------------------------------------
    # The three tool payload builders (plain functions, easy to unit test)
    # ------------------------------------------------------------------

    def level_map_payload(self) -> dict[str, Any]:
        """Top relevance-scored support/resistance for daily and weekly."""
        cfg = self.params
        return {
            "symbol": self.symbol,
            "daily": self.daily_levels[: int(cfg["max_levels"])],
            "weekly": self.weekly_levels[: int(cfg["max_weekly_levels"])],
            "note": "relevance is 0..1 (1 = most relevant); prefer high + near price.",
        }

    def price_patterns_payload(self) -> dict[str, Any]:
        """Unfilled FVGs, double bottom/top, and unmitigated order blocks."""
        cfg = self.params
        limit = int(cfg["max_patterns"])

        gaps = detect_fair_value_gaps(
            self.daily,
            min_gap_pct=float(cfg["fvg_min_gap_pct"]),
            lookback_bars=int(cfg["fvg_lookback_bars"]),
        )
        # The agent mostly cares about UNFILLED gaps (still-live zones), freshest first.
        unfilled = [g for g in gaps if not g["filled"]]
        unfilled.sort(key=lambda g: g["bars_ago"])

        doubles = detect_double_patterns(
            self.daily,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            tolerance_pct=float(cfg["double_tolerance_pct"]),
            lookback_bars=int(cfg["double_lookback_bars"]),
        )

        blocks = detect_order_blocks(
            self.daily,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            lookback_bars=int(cfg["ob_lookback_bars"]),
        )
        unmitigated = [b for b in blocks if not b["mitigated"]]

        return {
            "symbol": self.symbol,
            "fair_value_gaps": unfilled[:limit],
            "double_bottom": doubles["double_bottom"],
            "double_top": doubles["double_top"],
            "order_blocks": unmitigated[:limit],
            "note": "Only bullish, confirmed/unmitigated, near-price setups should drive a BUY.",
        }

    def market_structure_payload(self) -> dict[str, Any]:
        """Daily and weekly trend + the latest BOS / CHoCH on each."""
        cfg = self.params
        daily_structure = detect_market_structure(
            self.daily,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            lookback_bars=int(cfg["structure_lookback_bars"]),
        )
        if self.weekly is not None and not self.weekly.empty:
            weekly_structure = detect_market_structure(
                self.weekly,
                left=int(cfg["swing_left"]),
                right=int(cfg["swing_right"]),
                lookback_bars=int(cfg["structure_lookback_bars"]),
            )
        else:
            weekly_structure = None
        return {
            "symbol": self.symbol,
            "daily": daily_structure,
            "weekly": weekly_structure,
        }


def _as_tool_text(payload: dict[str, Any]) -> dict[str, Any]:
    """Wrap a payload dict in the MCP tool-result envelope the SDK expects."""
    # `default=str` is a belt-and-braces guard so any stray numpy/Timestamp value
    # still serializes instead of raising mid-tool-call.
    return {"content": [{"type": "text", "text": json.dumps(payload, default=str)}]}


def build_technical_mcp_server(context: TechnicalToolContext):
    """Build the in-process MCP server exposing the three technical tools.

    Returns ``(mcp_servers, allowed_tool_names)`` ready to drop into
    `ClaudeAgentOptions` — ``mcp_servers`` is ``{SERVER_NAME: server}`` and
    ``allowed_tool_names`` is the list of fully-qualified tool names. Imports the
    Claude Agent SDK lazily so importing this module never requires the SDK.
    Mirrors the fundamentals agent's `create_sdk_mcp_server` wiring (see
    `backend/fundamentals/fundamental_agent.py`).
    """
    # Lazy import: only building the real server needs the SDK.
    from claude_agent_sdk import create_sdk_mcp_server, tool  # type: ignore[import-not-found, unused-ignore]

    # No-argument tools: each one already knows which stock it is about because it
    # closes over `context`. The empty ``{}`` schema declares "takes no input".
    @tool("level_map", "Relevance-scored support/resistance levels (daily + weekly).", {})
    async def _level_map(_args: dict[str, Any]) -> dict[str, Any]:
        return _as_tool_text(context.level_map_payload())

    @tool(
        "price_patterns",
        "Unfilled Fair Value Gaps, double bottom/top (with confirmation), and "
        "unmitigated order blocks near current price.",
        {},
    )
    async def _price_patterns(_args: dict[str, Any]) -> dict[str, Any]:
        return _as_tool_text(context.price_patterns_payload())

    @tool(
        "market_structure",
        "Daily and weekly trend plus the latest Break of Structure / Change of "
        "Character.",
        {},
    )
    async def _market_structure(_args: dict[str, Any]) -> dict[str, Any]:
        return _as_tool_text(context.market_structure_payload())

    server = create_sdk_mcp_server(
        name=SERVER_NAME,
        version="1.0.0",
        tools=[_level_map, _price_patterns, _market_structure],
    )
    return {SERVER_NAME: server}, list(TOOL_NAMES)
