"""Compose a structured daily-scan report from a job summary + persisted history.

A read-only consumer of the daily job's ``DailyScanSummary`` and the scan-history
tables. It produces a fully-materialised ``DailyScanReport`` (primitives only — no
detached ORM rows) that the renderers turn into a Telegram/email message. All
database access happens inside one short session and degrades gracefully: if the
read fails, the alert still goes out with the counts already in the summary.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from sqlalchemy.orm import Session

from backend.notifications.config import NotificationSettings
from backend.storage import get_scan_runs, get_top_ranked_results, session_scope

if TYPE_CHECKING:
    from backend.jobs.run_daily_scan import DailyScanOutcome, DailyScanSummary

logger = logging.getLogger(__name__)

SessionFactory = Callable[[], AbstractContextManager[Session]]

TOP_RESULTS_LIMIT = 10


@dataclass(frozen=True)
class RankedRow:
    """One ranked shortlist row for the summary's top-N table."""

    symbol: str
    rating: str | None
    score: float | None
    screener_key: str
    score_source: str = "final_score"

    @property
    def final_score(self) -> float | None:
        """Backward-compatible alias for older renderer/tests.

        ALERT-001 originally exposed only ``final_score``. Once we added the
        confidence fallback, the neutral field name ``score`` became clearer,
        but this property keeps older in-process callers from breaking.
        """
        return self.score


@dataclass(frozen=True)
class ScreenerLine:
    """One per-screener status line in the summary."""

    screener_key: str
    universe_key: str | None
    status: str
    shortlisted: int
    message: str


@dataclass(frozen=True)
class DailyScanReport:
    """The fully-materialised summary the renderers format into a message."""

    ok: bool
    screeners: tuple[ScreenerLine, ...]
    total_symbols_scanned: int | None
    total_shortlisted: int
    failed_count: int
    failed_symbols_or_findings: int
    top_results: tuple[RankedRow, ...]
    app_url: str


def _status_label(outcome: DailyScanOutcome) -> str:
    """Human status for one outcome: ``failed`` when fatal, else the run status."""
    if outcome.fatal:
        return "failed"
    return outcome.status.value if outcome.status is not None else "unknown"


def _count_attr(outcome: DailyScanOutcome, name: str) -> int:
    """Read optional integer fields from real or test outcome objects safely."""
    try:
        value = int(getattr(outcome, name, 0) or 0)
    except (TypeError, ValueError):
        return 0
    return max(value, 0)


def _failed_symbols_or_findings(outcome: DailyScanOutcome) -> int:
    """Return the non-fatal dropped-symbol/finding count for one outcome.

    ``ai_validation_failures`` is intentionally not added here because those
    failures are already included in ``compute_failures`` by the scan service.
    Counting it again would make AI partial runs look worse than they are.
    """
    return (
        _count_attr(outcome, "loader_failures")
        + _count_attr(outcome, "compute_failures")
        + _count_attr(outcome, "rejected_result_rows")
        + _count_attr(outcome, "data_quality_fatal_symbols")
        + _count_attr(outcome, "data_quality_fatal_findings")
    )


def _finite_decimal(value: Any) -> Decimal | None:
    """Parse a finite numeric score from typed columns or raw JSON.

    Raw scanner JSON can contain strings, ints, floats, ``None``, or accidental
    values such as ``"nan"``. The notification should simply treat bad values as
    unscored, not fail the whole alert.
    """
    if value is None:
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return score if score.is_finite() else None


def _score_and_source(row: Any) -> tuple[float | None, str]:
    """Return the report score and the label the renderer should show."""
    final_score = _finite_decimal(getattr(row, "final_score", None))
    if final_score is not None:
        return float(final_score), "final_score"
    raw_result = getattr(row, "raw_result_json", None)
    if isinstance(raw_result, dict):
        confidence = _finite_decimal(raw_result.get("confidence"))
        if confidence is not None:
            return float(confidence), "confidence"
    return None, "unscored"


def build_daily_scan_report(
    summary: DailyScanSummary,
    *,
    settings: NotificationSettings,
    session_factory: SessionFactory = session_scope,
) -> DailyScanReport:
    """Build the report from a job ``summary`` plus a read of its persisted runs."""
    outcomes = list(summary.outcomes)
    screeners = tuple(
        ScreenerLine(
            screener_key=outcome.screener_key,
            universe_key=outcome.universe_key,
            status=_status_label(outcome),
            shortlisted=int(outcome.row_count),
            message=outcome.message,
        )
        for outcome in outcomes
    )
    total_shortlisted = sum(int(outcome.row_count) for outcome in outcomes)
    failed_count = sum(1 for outcome in outcomes if outcome.fatal)
    failed_symbols_or_findings = sum(
        _failed_symbols_or_findings(outcome) for outcome in outcomes
    )
    run_ids = [outcome.run_id for outcome in outcomes if outcome.run_id is not None]
    screener_by_run = {
        outcome.run_id: outcome.screener_key
        for outcome in outcomes
        if outcome.run_id is not None
    }

    total_symbols_scanned: int | None = None
    top_results: tuple[RankedRow, ...] = ()
    if run_ids:
        try:
            with session_factory() as session:
                runs = get_scan_runs(session, run_ids)
                scanned = [
                    run.symbols_scanned for run in runs if run.symbols_scanned is not None
                ]
                total_symbols_scanned = sum(scanned) if scanned else None
                top_rows = get_top_ranked_results(
                    session, run_ids, limit=TOP_RESULTS_LIMIT
                )
                ranked_rows: list[RankedRow] = []
                for row in top_rows:
                    score, score_source = _score_and_source(row)
                    ranked_rows.append(
                        RankedRow(
                            symbol=row.symbol,
                            rating=row.rating,
                            score=score,
                            screener_key=screener_by_run.get(row.run_id, ""),
                            score_source=score_source,
                        )
                    )
                top_results = tuple(ranked_rows)
        except Exception:  # noqa: BLE001 - a read failure must not block the alert
            logger.warning("daily-scan report DB read failed", exc_info=True)
            total_symbols_scanned = None
            top_results = ()

    return DailyScanReport(
        ok=summary.exit_code == 0,
        screeners=screeners,
        total_symbols_scanned=total_symbols_scanned,
        total_shortlisted=total_shortlisted,
        failed_count=failed_count,
        failed_symbols_or_findings=failed_symbols_or_findings,
        top_results=top_results,
        app_url=settings.app_url,
    )
