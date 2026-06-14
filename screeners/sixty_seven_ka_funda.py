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

import datetime as dt
import logging
from typing import ClassVar, Literal

import pandas as pd

from backend.charts import candlestick_with_volume
from backend.config import get_fundamentals_model
from backend.scanner_base import BaseScanner
from backend.scanning.result_contract import (
    AIEvaluationRecord,
    AIProvenance,
)
from backend.security import redact_text
from backend.sixty_seven.agent import (
    SIXTY_SEVEN_PROMPT_VERSION,
    SixtySevenEvaluationResult,
    SixtySevenVerdict,
    get_cached_agent,
    sixty_seven_provenance_fingerprints,
)
from backend.sixty_seven.shortlister import DrawdownCandidate, shortlist_candidate

logger = logging.getLogger(__name__)


def _get_agent():
    # Indirection so tests can monkeypatch the agent with a stub (see
    # tests/test_real_screeners.py) without disturbing get_cached_agent's cache.
    return get_cached_agent()


def _signal_date(value) -> dt.date | None:
    try:
        return pd.Timestamp(value).date()
    except (TypeError, ValueError):
        return None


def _emit_ai_evaluation(
    callback,
    *,
    candidate: DrawdownCandidate,
    result: SixtySevenEvaluationResult,
    outcome: Literal["approved", "rejected", "error"],
) -> None:
    if not callable(callback):
        return
    callback(
        AIEvaluationRecord(
            symbol=candidate.symbol,
            signal_date=_signal_date(candidate.signal_date),
            outcome=outcome,
            verdict=result.provenance.verdict,
            confidence=result.provenance.confidence,
            decision_reason=result.provenance.decision_reason,
            provenance=result.provenance,
            validated_verdict_json=result.validated_verdict_json,
        )
    )


def _sixty_seven_error_result(
    candidate: DrawdownCandidate, exc: Exception
) -> SixtySevenEvaluationResult:
    model = get_fundamentals_model()
    prompt_sha256, context_sha256 = sixty_seven_provenance_fingerprints(
        model,
        candidate.symbol,
        candidate,
    )
    provenance = AIProvenance(
        model_name=model,
        prompt_version=SIXTY_SEVEN_PROMPT_VERSION,
        prompt_sha256=prompt_sha256,
        generated_at=dt.datetime.now(dt.UTC),
        cache_hit=False,
        evidence_references=[],
        input_context_hash=context_sha256,
        verdict="error",
        confidence=None,
        decision_reason=(
            "67 ka funda evaluation failed "
            f"({type(exc).__name__})."
        ),
    )
    return SixtySevenEvaluationResult(
        verdict=None,
        provenance=provenance,
        validated_verdict_json={},
        error_type=type(exc).__name__,
    )


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
        provenance: AIProvenance,
    ) -> dict | None:
        """Turn an approved verdict into a result row, or None when not approved."""
        if not verdict.approved:
            return None
        # Display only source labels/domains. Scraped titles and snippets are
        # untrusted model context and must not enter the result table or CSV.
        evidence_summary = "; ".join(
            reference.source_label
            for reference in provenance.evidence_references[:3]
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
                ai=provenance,
            ),
        }

    def _consume_evaluation(
        self,
        candidate: DrawdownCandidate,
        evaluation: SixtySevenEvaluationResult,
        *,
        ai_evaluation_callback=None,
        compute_failure_callback=None,
    ) -> dict | None:
        if evaluation.verdict is None:
            _emit_ai_evaluation(
                ai_evaluation_callback,
                candidate=candidate,
                result=evaluation,
                outcome="error",
            )
            if callable(compute_failure_callback):
                compute_failure_callback(
                    {
                        "symbol": candidate.symbol,
                        "scanner": type(self).__name__,
                        "message": (
                            evaluation.provenance.decision_reason
                            or "67 ka funda AI evaluation failed."
                        ),
                        "phase": "ai_evaluation",
                    }
                )
            return None
        _emit_ai_evaluation(
            ai_evaluation_callback,
            candidate=candidate,
            result=evaluation,
            outcome="approved" if evaluation.verdict.approved else "rejected",
        )
        return self._row_from_verdict(
            candidate,
            evaluation.verdict,
            evaluation.provenance,
        )

    def compute_signal(self, symbol: str, candles: pd.DataFrame, params: dict) -> dict | None:
        """Single-symbol gate → verify path (the BaseScanner contract + tests).

        `run()` below overrides the universe loop to add the AI-candidate budget,
        but the per-symbol logic is identical: gate first, only then pay for the AI.
        """
        candidate = self._candidate_from_frame(symbol, candles, params)
        if candidate is None:
            return None
        try:
            evaluation = _get_agent().evaluate(
                candidate.symbol,
                candidate,
                force_refresh=bool(params.get("force_refresh", False)),
                search_result_count=int(params.get("search_result_count") or 5),
            )
        except Exception as exc:  # noqa: BLE001 - emit a trusted error receipt
            evaluation = _sixty_seven_error_result(candidate, exc)
        return self._consume_evaluation(
            candidate,
            evaluation,
            ai_evaluation_callback=params.get("ai_evaluation_callback"),
            compute_failure_callback=params.get("compute_failure_callback"),
        )

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
                safe_message = redact_text(str(exc))
                logger.warning("67 ka funda gate failed for %s: %s", symbol, safe_message)
                if callable(compute_failure_callback):
                    compute_failure_callback(
                        {
                            "symbol": symbol,
                            "scanner": type(self).__name__,
                            "message": safe_message,
                        }
                    )
                continue
            if candidate is None:
                continue
            # Budget guard: once we have verified this many gate-passing candidates
            # this run, stop spending AI calls (cached verdicts keep repeats cheap).
            if max_ai_candidates > 0 and candidates_seen >= max_ai_candidates:
                break
            candidates_seen += 1
            try:
                evaluation = _get_agent().evaluate(
                    candidate.symbol,
                    candidate,
                    force_refresh=force_refresh,
                    search_result_count=search_result_count,
                )
            except Exception as exc:  # noqa: BLE001 - emit a trusted error receipt
                logger.warning(
                    "67 ka funda AI verification unavailable for %s (%s).",
                    symbol,
                    type(exc).__name__,
                )
                evaluation = _sixty_seven_error_result(candidate, exc)
            row = self._consume_evaluation(
                candidate,
                evaluation,
                ai_evaluation_callback=params.get("ai_evaluation_callback"),
                compute_failure_callback=compute_failure_callback,
            )
            if row is not None:
                rows.append(row)

        return self.build_result_frame(
            rows,
            compute_failure_callback=compute_failure_callback,
        )

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
