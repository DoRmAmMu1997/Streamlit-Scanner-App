"""Hemant Super 45 ∪ Good 45 — Technical Analysis (AI) screener.

Flow in plain English:
1. Fetch daily candles for every stock in the Hemant Super 45 ∪ Good 45 union.
2. Compute that stock's MAJOR support/resistance levels — price zones touched
   by many confirmed pivots across the full ~10-year history.
3. **Cheap gate (no LLM):** keep the stock as a *candidate* only if either
   - its latest close is within `support_tolerance_pct` of a major support, OR
   - its latest close has broken above a major resistance within the last
     `breakout_lookback_bars` candles (a possible cup-rim / H&S-neckline break).
   Stocks that are mid-range get dropped here, before any LLM cost.
4. **LLM confirm:** each candidate's OHLC window + major levels are sent to the
   `TechnicalAnalysisAgent` (Claude Agent SDK, on your Claude subscription),
   which decides whether a breakout-confirmed cup-and-handle, a
   breakout-confirmed inverse head-and-shoulders, or an at-major-support setup
   is genuinely present.
5. Shortlist (BUY) when the agent reports `at_support`, or one of the two chart
   patterns WITH `confirmed=True`.

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

import pandas as pd

from backend.charts import candlestick_with_volume
from backend.config import get_agent_fast_mode, get_fundamentals_model
from backend.fundamentals.fundamental_agent import (
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
)
from backend.indicators import major_levels
from backend.scanner_base import BaseScanner
from backend.technical import TechnicalAnalysisAgent, TechnicalVerdict


logger = logging.getLogger(__name__)


# A module-level agent cache keyed by (model, fast_mode), mirroring how app.py
# memoizes the fundamental agent. The Claude Agent SDK authenticates via
# subscription, so there is no API key — only the model name and fast-mode flag
# key the cache (so a toggled SCANNER_AGENT_FAST_MODE rebuilds the agent).
_AGENT_CACHE: dict[tuple[str, bool], TechnicalAnalysisAgent] = {}
_AGENT_CACHE_LOCK = threading.Lock()


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
    """BUY when a breakout-confirmed pattern, or price at major support, is found."""

    SCREENER = {
        "key": "technical_analysis",
        "name": "Technical Analysis (AI)",
        "description": (
            "Hemant Super 45 ∪ Good 45 stocks with a breakout-confirmed "
            "cup-and-handle or inverse head-and-shoulders, or sitting at a major "
            "support level. A cheap pivot gate prefilters candidates; a Claude "
            "Agent SDK agent confirms the pattern."
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
            "support_tolerance_pct": 2.0,
            # "Fresh breakout": close crossed above a major resistance within
            # this many recent candles.
            "breakout_lookback_bars": 10,
            # Budget guard: after the cheap gate admits this many candidates,
            # skip further AI confirmations for the run. Cached verdicts still
            # make repeat runs cheap, but this protects first runs on busy days.
            "max_ai_candidates": 10,
        },
    }

    EXTRA_RESULT_COLUMNS = [
        "pattern",
        "confirmed",
        "confidence",
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

        if not at_support and not fresh_breakout:
            return None
        return {
            "nearest_support": nearest_support,
            "at_support": at_support,
            "fresh_breakout": fresh_breakout,
        }

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
            # candles in the loader and AI verdicts inside the agent.
            verdict: TechnicalVerdict = _get_agent().analyze(
                symbol,
                candidate["frame"],
                candidate["levels"],
                force_refresh=force_refresh,
            )
        except (FundamentalsAgentError, FundamentalsUsageLimitError) as exc:
            logger.warning("Technical agent unavailable for %s: %s", symbol, exc)
            if not candidate["gate"]["at_support"]:
                return None
            return {
                "symbol": symbol,
                "rating": "BUY",
                "signal_date": signal_date,
                "close": close,
                "pattern": "at_support",
                "confirmed": False,
                "confidence": 0,
                "nearest_level": nearest_level,
                "reason": (
                    f"Close {close:.2f} is at major support {nearest_level:.2f}. "
                    "AI pattern confirmation unavailable (Claude Agent SDK not "
                    "ready); showing gate-only candidate."
                ),
            }

        qualifies = verdict.pattern == "at_support" or (
            verdict.pattern in ("cup_and_handle", "inverse_head_and_shoulders")
            and verdict.confirmed
        )
        if not qualifies:
            return None

        # Prefer a level the agent named; otherwise fall back to the nearest
        # gate support so the column is always populated.
        reported_level = float(verdict.key_levels[0]) if verdict.key_levels else nearest_level

        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": signal_date,
            "close": close,
            "pattern": verdict.pattern,
            "confirmed": bool(verdict.confirmed),
            "confidence": int(verdict.confidence),
            "nearest_level": reported_level,
            "reason": verdict.reasoning,
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
        """Daily candles with the major support/resistance levels as guide lines."""
        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(
            frame, title="Daily candles + major support/resistance", ha=False
        )
        if frame.empty:
            return spec

        levels = major_levels(
            frame,
            left=self.coerce_param(params, "pivot_left", int),
            right=self.coerce_param(params, "pivot_right", int),
            cluster_pct=self.coerce_param(params, "cluster_pct", float),
            min_touches=self.coerce_param(params, "min_touches", int),
        )
        panes = spec.get("panes", [])
        if levels and panes:
            # Supports in teal, resistances in red, "both" in grey — drawn as
            # horizontal price lines on the price pane (pane 0).
            color_by_kind = {"support": "#26a69a", "resistance": "#ef5350", "both": "#888888"}
            panes[0].setdefault("price_lines", []).extend(
                {
                    "price": float(level["price"]),
                    "color": color_by_kind.get(str(level["kind"]), "#888888"),
                    "title": f"{level['kind']} ({level['touches']})",
                }
                for level in levels
            )
        return spec


# ---------------------------------------------------------------------------
# Module-level back-compat aliases
# ---------------------------------------------------------------------------

_scanner = TechnicalAnalysis()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
