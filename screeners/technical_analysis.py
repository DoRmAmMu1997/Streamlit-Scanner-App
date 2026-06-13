"""Hemant Super 45 ∪ Good 45 — Technical Analysis (AI) screener.

Flow in plain English:
1. Fetch daily candles for every stock in the Hemant Super 45 ∪ Good 45 union.
2. Compute that stock's MAJOR support/resistance levels — price zones touched
   by many confirmed pivots across the full ~10-year history.
3. **Cheap gate (no LLM):** keep the stock as a *candidate* only if it is near a
   deterministic bullish trigger: at major support, freshly broken above major
   resistance, freshly confirmed double bottom, retesting an unfilled bullish
   Fair Value Gap, or tapping a bullish order block.
   Stocks that are mid-range get dropped here, before any LLM cost.
4. **LLM confirm:** each candidate's OHLC window + major levels are sent to the
   `TechnicalAnalysisAgent` (Claude Agent SDK, on your Claude subscription),
   which calls tools for level relevance, price patterns, and market structure
   before deciding whether one bullish setup is genuinely present.
5. Shortlist (BUY) when the agent reports `at_support`, or one of the bullish
   chart/price-action patterns WITH `confirmed=True`.

Why a gate first: an LLM call per stock over ~90 names would be slow. The
pivot-based gate is pure pandas and rejects most stocks for free, so only a
handful of candidates ever reach the model.

Beginner note: this is the only screener that calls an LLM mid-scan, so the
progress bar pauses on candidate symbols. Verdicts cache per latest-candle date,
so re-running the same day is free. The agent uses the Claude Agent SDK
(authenticated via your Claude subscription — no API key); if the SDK is not
installed or the plan limit is hit, the screener degrades gracefully to
gate-only "near support" candidates rather than failing the whole scan.
"""

from __future__ import annotations

import concurrent.futures
import logging
import threading
from typing import ClassVar

import pandas as pd

from backend.charts import (
    add_levels_overlay,
    add_neckline_overlay,
    add_zone_overlays,
    candlestick_with_volume,
)
from backend.config import get_agent_fast_mode, get_fundamentals_model
from backend.fundamentals.fundamental_agent import (
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
)
from backend.indicators import major_levels, rank_levels
from backend.scanner_base import BaseScanner
from backend.technical import TechnicalAnalysisAgent, TechnicalVerdict
from backend.technical.patterns import (
    detect_double_patterns,
    detect_fair_value_gaps,
    detect_order_blocks,
)
from backend.technical.tools import DEFAULT_TOOL_PARAMS, resolve_params

logger = logging.getLogger(__name__)


# A module-level agent cache keyed by (model, fast_mode), mirroring how app.py
# memoizes the fundamental agent. The Claude Agent SDK authenticates via
# subscription, so there is no API key — only the model name and fast-mode flag
# key the cache (so a toggled SCANNER_AGENT_FAST_MODE rebuilds the agent).
_AGENT_CACHE: dict[tuple[str, bool], TechnicalAnalysisAgent] = {}
_AGENT_CACHE_LOCK = threading.Lock()

# The boolean gate flags (the gate dict's keys excluding "nearest_support"). Used
# to label which deterministic setups fired in PROV-002 provenance.
_GATE_RULE_FLAGS = (
    "at_support",
    "fresh_breakout",
    "double_bottom",
    "bullish_fvg",
    "order_block",
)


def _get_agent() -> TechnicalAnalysisAgent:
    """Return a cached technical agent for the configured Claude model + fast mode."""
    model = get_fundamentals_model()
    fast_mode = get_agent_fast_mode()
    key = (model, fast_mode)
    # PR #23 parallelized candidate confirmations, so worker threads can ask for
    # the shared agent at the same time. The lock keeps construction one-at-a-
    # time while all callers still reuse the same cached agent afterward.
    with _AGENT_CACHE_LOCK:
        agent = _AGENT_CACHE.get(key)
        if agent is None:
            agent = TechnicalAnalysisAgent(model=model, fast_mode=fast_mode)
            _AGENT_CACHE[key] = agent
        return agent


class TechnicalAnalysis(BaseScanner):
    """BUY when price is at support or an AI-confirmed bullish setup is present."""

    SCREENER: ClassVar[dict] = {
        "key": "technical_analysis",
        "name": "Technical Analysis (AI)",
        "description": (
            "Hemant Super 45 ∪ Good 45 stocks showing a bullish setup: at a major "
            "support, a breakout-confirmed cup-and-handle / inverse head-and-"
            "shoulders, a confirmed double bottom, a retested bullish Fair Value "
            "Gap, or a tap of a bullish order block. A cheap pivot/price-action "
            "gate prefilters candidates; a Claude Agent SDK agent (with tools for "
            "level relevance, structure, and pattern detection) confirms the setup."
        ),
        "universe": "hemant_super_good_union",
        "timeframe": "daily",
        # ~10 years of daily candles so the major-level clustering sees the full
        # history. The app prefetches ~10y anyway; this drives the sidebar value.
        "lookback_days": 2600,
        "default_params": {
            # Pivot detection width for the major-level builder.
            "pivot_left": 5,
            "pivot_right": 5,
            # Pivots within this percent of each other cluster into one level,
            # and a level needs at least `min_touches` pivots to count as major.
            "cluster_pct": 2.0,
            "min_touches": 3,
            # "At support": latest close within this percent of a major support.
            # Also used as the tolerance for "price is tapping" a FVG/order block.
            "support_tolerance_pct": 2.0,
            # "Fresh breakout": close crossed above a major resistance within
            # this many recent candles. Also bounds a "fresh" double-bottom
            # neckline breakout.
            "breakout_lookback_bars": 10,
            # Swing-pivot width for the price-action detectors (double patterns,
            # order blocks, market structure).
            "swing_left": 5,
            "swing_right": 5,
            # Smallest Fair Value Gap (percent of price) worth considering.
            "fvg_min_gap_pct": 0.3,
            # How equal the two lows of a double bottom must be (percent).
            "double_tolerance_pct": 3.0,
            # Resample daily → weekly so the agent gets higher-timeframe context.
            "weekly_enabled": True,
            # Budget guard: after the cheap gate admits this many candidates,
            # skip further AI confirmations for the run. Cached verdicts still
            # make repeat runs cheap, but this protects first runs on busy days.
            "max_ai_candidates": 10,
        },
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "pattern",
        "confirmed",
        "confidence",
        "trend",
        "nearest_level",
    ]

    # ------------------------------------------------------------------
    # Gate (cheap, no LLM)
    # ------------------------------------------------------------------

    def _gate(self, frame: pd.DataFrame, levels: list[dict], params: dict) -> dict | None:
        """Return gate context when the stock is a candidate, else None.

        The returned dict carries what both the LLM step and the
        graceful-degrade path need: the nearest support, whether price is at
        support, and whether a fresh resistance breakout occurred.
        """
        support_tol = self.coerce_param(params, "support_tolerance_pct", float) / 100.0
        breakout_lookback = self.coerce_param(params, "breakout_lookback_bars", int)

        close = float(frame.iloc[-1]["close"])
        supports = [lvl for lvl in levels if lvl["kind"] in ("support", "both")]
        resistances = [lvl for lvl in levels if lvl["kind"] in ("resistance", "both")]

        # "At support": close within tolerance of a major support (a bounce
        # zone, so the close should sit at/near the level on either side).
        nearest_support = None
        at_support = False
        if supports:
            nearest_support = min(supports, key=lambda lvl: abs(close - lvl["price"]))
            level_price = float(nearest_support["price"])
            if level_price > 0:
                distance = (close - level_price) / level_price
                at_support = -support_tol <= distance <= support_tol

        # "Fresh breakout": within the last `breakout_lookback` candles the close
        # crossed from below to above a major resistance.
        fresh_breakout = False
        window = frame.tail(breakout_lookback + 1)
        if len(window) >= 2 and resistances:
            window_closes = window["close"].astype(float).to_numpy()
            for level in resistances:
                level_price = float(level["price"])
                below_then = (window_closes[:-1] < level_price).any()
                above_now = window_closes[-1] > level_price
                if below_then and above_now:
                    fresh_breakout = True
                    break

        # --- New price-action triggers (still cheap, pure pandas) ---
        # Resolve the detector settings exactly as the agent's tools do, so the
        # gate and the agent agree on what counts as a setup.
        cfg = resolve_params(self._tool_params(params))

        # Fresh confirmed DOUBLE BOTTOM: the neckline breakout printed within the
        # last `breakout_lookback` candles (so we catch it near the trigger).
        double_bottom_fresh = False
        doubles = detect_double_patterns(
            frame,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            tolerance_pct=float(cfg["double_tolerance_pct"]),
            lookback_bars=int(cfg["double_lookback_bars"]),
        )
        db = doubles["double_bottom"]
        if db and db["confirmed"] and db["confirm_bars_ago"] is not None:
            double_bottom_fresh = db["confirm_bars_ago"] <= breakout_lookback

        # Price tapping an UNFILLED bullish FAIR VALUE GAP (a demand zone): the
        # latest close sits within the gap (allowing the support tolerance).
        bullish_fvg = False
        for gap in detect_fair_value_gaps(
            frame,
            min_gap_pct=float(cfg["fvg_min_gap_pct"]),
            lookback_bars=int(cfg["fvg_lookback_bars"]),
        ):
            if gap["direction"] != "bullish" or gap["filled"]:
                continue
            if gap["bottom"] * (1 - support_tol) <= close <= gap["top"] * (1 + support_tol):
                bullish_fvg = True
                break

        # Price tapping an UNMITIGATED bullish ORDER BLOCK (a demand zone).
        order_block = False
        for block in detect_order_blocks(
            frame,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            lookback_bars=int(cfg["ob_lookback_bars"]),
        ):
            if block["direction"] != "bullish" or block["mitigated"]:
                continue
            if block["bottom"] * (1 - support_tol) <= close <= block["top"] * (1 + support_tol):
                order_block = True
                break

        if not (at_support or fresh_breakout or double_bottom_fresh or bullish_fvg or order_block):
            # Mid-range stock with no bullish setup — dropped for free, no LLM.
            return None
        return {
            "nearest_support": nearest_support,
            "at_support": at_support,
            "fresh_breakout": fresh_breakout,
            "double_bottom": double_bottom_fresh,
            "bullish_fvg": bullish_fvg,
            "order_block": order_block,
        }

    @staticmethod
    def _tool_params(params: dict) -> dict:
        """Pass through only the detector settings the agent's tools understand.

        The screener's `params` also carries run-time keys (start/end dates,
        progress callbacks, the AI-budget counter) that must NEVER reach the
        agent's verdict cache hash — a callback object would change the hash every
        run. So we forward only the known tool knobs; the agent fills any missing
        ones from `DEFAULT_TOOL_PARAMS`.
        """
        return {key: params[key] for key in DEFAULT_TOOL_PARAMS if key in params}

    # ------------------------------------------------------------------
    # Strategy hook
    # ------------------------------------------------------------------

    def _prepare_candidate(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Run the cheap (no-LLM) prep + gate for one symbol.

        Returns a candidate context dict (frame, levels, gate, close, etc.) when
        the stock passes the gate, or None when it has too little history, no
        major levels, or is mid-range. This is the part that is safe and fast to
        run sequentially for the whole universe before any AI calls.
        """
        frame = self.prepare_candles(candles)
        # Need enough history for the pivot windows to confirm at all.
        pivot_left = self.coerce_param(params, "pivot_left", int)
        pivot_right = self.coerce_param(params, "pivot_right", int)
        if frame.empty or len(frame) < (pivot_left + pivot_right + 1):
            return None

        levels = major_levels(
            frame,
            left=pivot_left,
            right=pivot_right,
            cluster_pct=self.coerce_param(params, "cluster_pct", float),
            min_touches=self.coerce_param(params, "min_touches", int),
        )
        if not levels:
            return None

        gate = self._gate(frame, levels, params)
        if gate is None:
            # Mid-range stock — dropped for free, no LLM call.
            return None

        nearest_support = gate["nearest_support"]
        return {
            "symbol": symbol,
            "frame": frame,
            "levels": levels,
            "gate": gate,
            # Only the detector knobs travel to the agent (see `_tool_params`).
            "tool_params": self._tool_params(params),
            "close": float(frame.iloc[-1]["close"]),
            "signal_date": frame.iloc[-1].get("timestamp", ""),
            "nearest_level": float(nearest_support["price"]) if nearest_support else float("nan"),
        }

    def _confirm_candidate(self, candidate: dict, *, force_refresh: bool = False) -> dict | None:
        """Run the AI confirmation for one gate-passing candidate → result row.

        Calls the Claude Agent SDK agent and maps its verdict to a result row.
        On agent failure (SDK missing / plan limit) it degrades to a gate-only
        "at support" row, so one missing dependency never fails the scan. This
        is the slow part — `run()` fans these calls out across a thread pool.
        """
        symbol = candidate["symbol"]
        close = candidate["close"]
        signal_date = candidate["signal_date"]
        nearest_level = candidate["nearest_level"]
        try:
            # A user-triggered refresh should bypass both layers of cache:
            # candles in the loader and AI verdicts inside the agent. The detector
            # settings travel along so the agent's tools match the gate.
            verdict: TechnicalVerdict = _get_agent().analyze(
                symbol,
                candidate["frame"],
                candidate["levels"],
                params=candidate.get("tool_params"),
                force_refresh=force_refresh,
            )
        except (FundamentalsAgentError, FundamentalsUsageLimitError) as exc:
            logger.warning("Technical agent unavailable for %s: %s", symbol, exc)
            return self._gate_only_row(candidate)

        # A BUY needs either price currently AT a major support, or one of the
        # bullish patterns with its trigger already confirmed (breakout/reaction).
        bullish_confirmable = (
            "cup_and_handle",
            "inverse_head_and_shoulders",
            "double_bottom",
            "fair_value_gap",
            "order_block",
        )
        qualifies = verdict.pattern == "at_support" or (
            verdict.pattern in bullish_confirmable and verdict.confirmed
        )
        if not qualifies:
            return None

        # Prefer a level the agent named; otherwise fall back to the nearest
        # gate support so the column is always populated.
        reported_level = float(verdict.key_levels[0]) if verdict.key_levels else nearest_level

        gate_rules = [
            f"gate_{flag}"
            for flag in _GATE_RULE_FLAGS
            if candidate["gate"].get(flag)
        ]
        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": signal_date,
            "close": close,
            "pattern": verdict.pattern,
            "confirmed": bool(verdict.confirmed),
            "confidence": int(verdict.confidence),
            "trend": verdict.trend,
            "nearest_level": reported_level,
            "reason": verdict.reasoning,
            # Gate + AI agreed, so this is a hybrid signal. AI evidence (model,
            # prompt, scraped text) is intentionally out of scope until PROV-003.
            "provenance": self.build_provenance(
                triggered_rules=[*gate_rules, "ai_setup_qualified"],
                indicator_values={
                    "close": close,
                    "nearest_level": reported_level,
                    "confidence": int(verdict.confidence),
                },
                source="hybrid",
            ),
        }

    def _gate_only_row(self, candidate: dict) -> dict | None:
        """Build a gate-only BUY row when the Claude Agent SDK is unavailable.

        Without the agent (not installed, or plan limit hit) we still surface a
        stock when the cheap gate found a *deterministically* bullish setup — at
        support, a freshly confirmed double bottom, or price tapping an unfilled
        bullish FVG / order block — so one missing dependency never fails the
        scan. A bare resistance breakout is NOT surfaced here: identifying the
        cup-and-handle / H&S behind it needs the AI, so it waits for confirmation.
        `confirmed=False` flags that no AI confirmation happened.
        """
        gate = candidate["gate"]
        close = candidate["close"]
        nearest_level = candidate["nearest_level"]
        if gate.get("at_support"):
            pattern, what = "at_support", f"at major support {nearest_level:.2f}"
        elif gate.get("double_bottom"):
            pattern, what = "double_bottom", "at a freshly confirmed double bottom"
        elif gate.get("bullish_fvg"):
            pattern, what = "fair_value_gap", "tapping an unfilled bullish fair value gap"
        elif gate.get("order_block"):
            pattern, what = "order_block", "tapping a bullish order block"
        else:
            # Only a resistance breakout fired — that needs AI to label. Drop it.
            return None
        return {
            "symbol": candidate["symbol"],
            "rating": "BUY",
            "signal_date": candidate["signal_date"],
            "close": close,
            "pattern": pattern,
            "confirmed": False,
            "confidence": 0,
            "trend": "",
            "nearest_level": nearest_level,
            "reason": (
                f"Gate: close {close:.2f} is {what}. AI confirmation unavailable "
                "(Claude Agent SDK not ready); showing gate-only candidate."
            ),
            # No AI ran, so this fallback row is a purely deterministic gate hit.
            "provenance": self.build_provenance(
                triggered_rules=[f"gate_{pattern}", "ai_confirmation_unavailable"],
                indicator_values={"close": close, "nearest_level": nearest_level},
                source="deterministic",
            ),
        }

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Return a BUY row when the gate + LLM agree on a qualifying setup.

        Single-symbol path (used by the BaseScanner contract and tests). The
        screener's own `run()` override parallelizes the AI step across symbols,
        but the gate → confirm logic is shared via the helpers below so behavior
        is identical either way.
        """
        candidate = self._prepare_candidate(symbol, candles, params)
        if candidate is None:
            return None

        # Budget guard: the cheap gate can admit many stocks on volatile days;
        # this caps the expensive model step. Universe order gives deterministic
        # behavior for repeatable scans and tests.
        max_ai_candidates = int(params.get("max_ai_candidates") or 0)
        ai_calls_used = int(params.get("_technical_ai_calls_used", 0))
        if max_ai_candidates > 0 and ai_calls_used >= max_ai_candidates:
            logger.info(
                "Skipping %s technical AI confirmation; max_ai_candidates=%d reached",
                symbol,
                max_ai_candidates,
            )
            return None
        params["_technical_ai_calls_used"] = ai_calls_used + 1
        return self._confirm_candidate(
            candidate,
            force_refresh=bool(params.get("force_refresh", False)),
        )

    def run(self, universe_df: pd.DataFrame, data_loader, params: dict) -> pd.DataFrame:
        """Fetch candles, then gate sequentially and confirm candidates in parallel.

        Overrides `BaseScanner.run` for this screener ONLY. The cheap pivot gate
        runs sequentially over the whole universe (pure pandas), then the few
        gate-passing candidates (capped by `max_ai_candidates`) have their slow
        Claude Agent SDK confirmations fanned out across a small thread pool —
        each `analyze()` already owns its event loop, so they overlap safely.
        Rows are assembled in universe order so output stays deterministic.
        """
        batch = data_loader.load_universe_history(
            universe_df=universe_df,
            start_date=params["start_date"],
            end_date=params["end_date"],
            max_symbols=params.get("max_symbols"),
            force_refresh=bool(params.get("force_refresh", False)),
            progress_callback=params.get("progress_callback"),
        )

        # 1. Sequential gate pass (cheap, no LLM). Respect the candidate budget.
        max_ai_candidates = int(params.get("max_ai_candidates") or 0)
        force_refresh = bool(params.get("force_refresh", False))
        candidates: list[dict] = []
        compute_failure_callback = params.get("compute_failure_callback")
        for symbol, candles in batch.frames.items():
            try:
                candidate = self._prepare_candidate(symbol, candles, params)
            except Exception as exc:  # noqa: BLE001 — one bad frame must not abort the scan
                logger.warning("%s gate failed for %s: %s", type(self).__name__, symbol, exc)
                if callable(compute_failure_callback):
                    compute_failure_callback(
                        {"symbol": symbol, "scanner": type(self).__name__, "message": str(exc)}
                    )
                continue
            if candidate is not None:
                candidates.append(candidate)
            if max_ai_candidates > 0 and len(candidates) >= max_ai_candidates:
                break

        # 2. Parallel AI pass. Each analyze() owns its own event loop/subprocess,
        #    so a small thread pool just overlaps the wall-clock latency.
        rows_by_symbol: dict[str, dict] = {}
        if candidates:
            max_workers = min(len(candidates), 4)
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                future_to_symbol = {
                    executor.submit(
                        self._confirm_candidate,
                        candidate,
                        force_refresh=force_refresh,
                    ): candidate["symbol"]
                    for candidate in candidates
                }
                for future in concurrent.futures.as_completed(future_to_symbol):
                    symbol = future_to_symbol[future]
                    try:
                        row = future.result()
                    except Exception as exc:  # noqa: BLE001 — isolate per-symbol failures
                        logger.warning(
                            "%s AI confirm failed for %s: %s", type(self).__name__, symbol, exc
                        )
                        if callable(compute_failure_callback):
                            compute_failure_callback(
                                {"symbol": symbol, "scanner": type(self).__name__, "message": str(exc)}
                            )
                        continue
                    if row is not None:
                        rows_by_symbol[symbol] = row

        # 3. Assemble in universe order so the table + tests stay deterministic.
        rows = [rows_by_symbol[c["symbol"]] for c in candidates if c["symbol"] in rows_by_symbol]
        return pd.DataFrame(rows, columns=self.result_columns)

    # ------------------------------------------------------------------
    # Chart
    # ------------------------------------------------------------------

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Daily candles annotated with the structures the agent reasons about.

        Draws, on the price pane: relevance-weighted support/resistance (thicker
        line = more relevant), the nearest unfilled Fair Value Gaps and
        unmitigated order blocks as dotted demand/supply bands, and any
        double-pattern neckline. This is the visual companion to the agent's
        verdict so a beginner can *see* why a stock was shortlisted.
        """
        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(
            frame, title="Daily candles + relevant levels, FVGs & order blocks", ha=False
        )
        panes = spec.get("panes", [])
        if frame.empty or not panes:
            return spec

        cfg = resolve_params(self._tool_params(params))

        # Relevance-weighted support/resistance.
        levels = major_levels(
            frame,
            left=self.coerce_param(params, "pivot_left", int),
            right=self.coerce_param(params, "pivot_right", int),
            cluster_pct=self.coerce_param(params, "cluster_pct", float),
            min_touches=self.coerce_param(params, "min_touches", int),
        )
        ranked = rank_levels(
            frame,
            levels,
            band_pct=float(cfg["level_band_pct"]),
            recency_halflife_bars=int(cfg["level_recency_halflife_bars"]),
        )
        add_levels_overlay(spec, ranked)

        # Unfilled Fair Value Gaps + unmitigated order blocks, the few NEAREST to
        # the latest close (keeps a 10-year chart readable).
        close = float(frame.iloc[-1]["close"])
        zones: list[dict] = []
        gaps = [
            g
            for g in detect_fair_value_gaps(
                frame,
                min_gap_pct=float(cfg["fvg_min_gap_pct"]),
                lookback_bars=int(cfg["fvg_lookback_bars"]),
            )
            if not g["filled"]
        ]
        gaps.sort(key=lambda g: abs((g["top"] + g["bottom"]) / 2.0 - close))
        for gap in gaps[:3]:
            bull = gap["direction"] == "bullish"
            zones.append(
                {
                    "top": gap["top"],
                    "bottom": gap["bottom"],
                    "kind": "fvg_bull" if bull else "fvg_bear",
                    "title": f"{'Bull' if bull else 'Bear'} FVG",
                }
            )
        blocks = [
            b
            for b in detect_order_blocks(
                frame,
                left=int(cfg["swing_left"]),
                right=int(cfg["swing_right"]),
                lookback_bars=int(cfg["ob_lookback_bars"]),
            )
            if not b["mitigated"]
        ]
        for ob in blocks[:2]:
            bull = ob["direction"] == "bullish"
            zones.append(
                {
                    "top": ob["top"],
                    "bottom": ob["bottom"],
                    "kind": "ob_bull" if bull else "ob_bear",
                    "title": f"{'Bull' if bull else 'Bear'} OB",
                }
            )
        add_zone_overlays(spec, zones)

        # Double top/bottom necklines (the breakout trigger line).
        doubles = detect_double_patterns(
            frame,
            left=int(cfg["swing_left"]),
            right=int(cfg["swing_right"]),
            tolerance_pct=float(cfg["double_tolerance_pct"]),
            lookback_bars=int(cfg["double_lookback_bars"]),
        )
        for key, label in (
            ("double_bottom", "Double-bottom neckline"),
            ("double_top", "Double-top neckline"),
        ):
            pattern = doubles.get(key)
            if pattern:
                add_neckline_overlay(spec, pattern["neckline"], label)
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = TechnicalAnalysis()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
