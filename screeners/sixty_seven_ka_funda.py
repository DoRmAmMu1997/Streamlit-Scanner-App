"""Hemant Super 45 + Good 45 + Good 200 — 67 Ka Funda (AI) screener.

Two-step flow (beginner note):
1. Cheap gate (no LLM): `shortlist_candidate` keeps only stocks down at least 67%
   from their available-history ATH (with >=100% upside). Pure price math over the
   whole universe — most stocks are dropped here for free.
2. AI verify (per candidate): the `SixtySevenAgent` researches each survivor via
   Screener.in + SerpAPI and returns an approve/reject verdict. Only AI-approved
   stocks become BUY rows.

If the Claude Agent SDK or SerpAPI is unavailable, the run logs and skips the AI
step for that symbol rather than failing the whole scan.
"""

from __future__ import annotations

import logging
from typing import ClassVar

import pandas as pd

from backend.charts import candlestick_with_volume
from backend.fundamentals.fundamental_agent import (
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
)
from backend.scanner_base import BaseScanner
from backend.sixty_seven.agent import SixtySevenVerdict, get_cached_agent
from backend.sixty_seven.search_client import SerpApiSearchError, SerpApiSetupError
from backend.sixty_seven.shortlister import DrawdownCandidate, shortlist_candidate

logger = logging.getLogger(__name__)


def _get_agent():
    # Indirection so tests can monkeypatch the agent with a stub (see
    # tests/test_real_screeners.py) without disturbing get_cached_agent's cache.
    return get_cached_agent()


class SixtySevenKaFunda(BaseScanner):
    """BUY only when the deterministic 67% gate and AI verifier both pass."""

    SCREENER: ClassVar[dict] = {
        "key": "sixty_seven_ka_funda",
        "name": "67 Ka Funda (AI)",
        "description": (
            "Hemant Super 45 + Good 45 + Good 200 stocks down at least 67% "
            "from available-history ATH, then approved by a Claude Agent SDK "
            "research verifier using Screener.in and SerpAPI Google snippets."
        ),
        "universe": "hemant_super_good_200_union",
        "timeframe": "daily",
        "lookback_days": 3650,
        "default_params": {
            "drawdown_threshold_pct": 67.0,
            "upside_threshold_pct": 100.0,
            "max_ai_candidates": 10,
            "search_result_count": 5,
        },
    }

    EXTRA_RESULT_COLUMNS: ClassVar[list[str]] = [
        "ath_price",
        "ath_date",
        "drawdown_pct",
        "upside_to_ath_pct",
        "fall_reason_category",
        "confidence",
        "evidence_summary",
    ]

    def _candidate_from_frame(
        self,
        symbol: str,
        candles: pd.DataFrame,
        params: dict,
    ) -> DrawdownCandidate | None:
        """Run only the cheap deterministic 67% gate (step 1) for one symbol.

        Split out from `_row_from_verdict` so `run()` can gate the whole universe
        first and only spend the expensive AI call on the survivors.
        """
        return shortlist_candidate(
            symbol,
            candles,
            drawdown_threshold_pct=self.coerce_param(params, "drawdown_threshold_pct", float),
            upside_threshold_pct=self.coerce_param(params, "upside_threshold_pct", float),
        )

    def _row_from_verdict(
        self,
        candidate: DrawdownCandidate,
        verdict: SixtySevenVerdict,
    ) -> dict | None:
        """Turn an approved verdict into a result row, or None when not approved."""
        if not verdict.approved:
            return None
        # A short, human-readable digest of the first few evidence items for the
        # results table (titles preferred, falling back to snippets).
        evidence_summary = "; ".join(
            item.title or item.snippet for item in verdict.evidence[:3] if (item.title or item.snippet)
        )
        return {
            "symbol": candidate.symbol,
            "rating": "BUY",
            "signal_date": candidate.signal_date,
            "close": float(candidate.latest_close),
            "reason": verdict.summary,
            "ath_price": float(candidate.ath_price),
            "ath_date": candidate.ath_date,
            "drawdown_pct": float(candidate.drawdown_pct),
            "upside_to_ath_pct": float(candidate.upside_to_ath_pct),
            "fall_reason_category": verdict.fall_reason_category,
            "confidence": int(verdict.confidence),
            "evidence_summary": evidence_summary,
            # Deterministic drawdown gate + AI verifier => hybrid. The AI evidence
            # itself (model, prompt, scraped text) is reserved for PROV-003.
            "provenance": self.build_provenance(
                triggered_rules=["drawdown_gate_passed", "ai_verdict_approved"],
                indicator_values={
                    "close": float(candidate.latest_close),
                    "ath_price": float(candidate.ath_price),
                    "drawdown_pct": float(candidate.drawdown_pct),
                    "upside_to_ath_pct": float(candidate.upside_to_ath_pct),
                    "confidence": int(verdict.confidence),
                },
                source="hybrid",
            ),
        }

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Single-symbol gate → verify path (the BaseScanner contract + tests).

        `run()` below overrides the universe loop to add the AI-candidate budget,
        but the per-symbol logic is identical: gate first, only then pay for the AI.
        """
        candidate = self._candidate_from_frame(symbol, candles, params)
        if candidate is None:
            return None
        verdict = _get_agent().verify(
            candidate.symbol,
            candidate,
            force_refresh=bool(params.get("force_refresh", False)),
            search_result_count=int(params.get("search_result_count") or 5),
        )
        return self._row_from_verdict(candidate, verdict)

    def run(self, universe_df: pd.DataFrame, data_loader, params: dict) -> pd.DataFrame:
        """Gate the whole universe cheaply, then AI-verify the survivors in order.

        Overrides `BaseScanner.run` so we can cap the number of (expensive) AI
        verifications per scan via `max_ai_candidates`. Both the gate and the AI
        step isolate per-symbol failures (log + the failure callback) so one bad
        stock never aborts the whole scan.
        """
        batch = data_loader.load_universe_history(
            universe_df=universe_df,
            start_date=params["start_date"],
            end_date=params["end_date"],
            max_symbols=params.get("max_symbols"),
            force_refresh=bool(params.get("force_refresh", False)),
            progress_callback=params.get("progress_callback"),
        )

        max_ai_candidates = int(params.get("max_ai_candidates") or 0)
        search_result_count = int(params.get("search_result_count") or 5)
        force_refresh = bool(params.get("force_refresh", False))
        compute_failure_callback = params.get("compute_failure_callback")

        rows: list[dict] = []
        candidates_seen = 0
        for symbol, candles in batch.frames.items():
            try:
                candidate = self._candidate_from_frame(symbol, candles, params)
            except Exception as exc:  # noqa: BLE001 - isolate malformed candle frames
                logger.warning("67 ka funda gate failed for %s: %s", symbol, exc)
                if callable(compute_failure_callback):
                    compute_failure_callback(
                        {"symbol": symbol, "scanner": type(self).__name__, "message": str(exc)}
                    )
                continue
            if candidate is None:
                continue
            # Budget guard: once we have verified this many gate-passing candidates
            # this run, stop spending AI calls (cached verdicts keep repeats cheap).
            if max_ai_candidates > 0 and candidates_seen >= max_ai_candidates:
                break
            candidates_seen += 1
            # Graceful degradation: a missing SDK / plan limit / SerpAPI outage
            # skips just this symbol's AI step instead of failing the whole scan.
            try:
                verdict = _get_agent().verify(
                    candidate.symbol,
                    candidate,
                    force_refresh=force_refresh,
                    search_result_count=search_result_count,
                )
            except (
                FundamentalsAgentError,
                FundamentalsUsageLimitError,
                SerpApiSetupError,
                SerpApiSearchError,
            ) as exc:
                logger.warning("67 ka funda AI verification unavailable for %s: %s", symbol, exc)
                if callable(compute_failure_callback):
                    compute_failure_callback(
                        {"symbol": symbol, "scanner": type(self).__name__, "message": str(exc)}
                    )
                continue
            row = self._row_from_verdict(candidate, verdict)
            if row is not None:
                rows.append(row)

        return pd.DataFrame(rows, columns=self.result_columns)

    def build_chart(self, candles: pd.DataFrame, params: dict) -> dict:
        """Daily candles with the available-history ATH drawn as a red guide line."""
        frame = self.prepare_candles(candles)
        spec = candlestick_with_volume(frame, title="67 ka funda daily candles", ha=False)
        if frame.empty:
            return spec
        panes = spec.get("panes", [])
        if panes:
            # The ATH is the reference the whole strategy hangs off of, so mark it.
            panes[0].setdefault("price_lines", []).append(
                {"price": float(frame["high"].max()), "color": "#ef5350", "title": "ATH"}
            )
        return spec


_scanner = SixtySevenKaFunda()
SCREENER = _scanner.SCREENER
RESULT_COLUMNS = _scanner.result_columns
run = _scanner.run
build_chart = _scanner.build_chart
