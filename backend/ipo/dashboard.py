"""IPO-007: assemble the read-only dashboard snapshot from stored evidence.

The Streamlit page renders whatever this module returns and nothing else, so
every rule about what the dashboard shows lives here, Streamlit-free and unit
testable: which issues belong to which section, what counts as missing data,
and which factors are surfaced as an issue's top strengths and risks.

Beginner note:
Everything here is a repository *read*. No network call, no scoring, and no
writes happen while building a snapshot — the compute pass is the
``run_ipo_screener`` job (or the dashboard's explicit re-score action), never
a page render. That is the same read-page/compute-job split the validation
dashboard established.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from backend.ipo.models import (
    IpoEvaluationRecord,
    IpoExtractionProposalStatus,
    IpoStatus,
    Recommendation,
)
from backend.ipo.repository import (
    SessionFactory,
    get_latest_evaluation,
    get_latest_manual_profile,
    list_documents,
    list_extraction_proposals,
    list_issues,
)
from backend.ipo.scoring.score_model import PDF_WEIGHTS
from backend.storage import session_scope

# Selection thresholds for the strengths/risks columns: a factor earning at
# least 75% of its weight is a headline strength; one earning 35% or less is a
# headline risk. Missing factors are excluded — they belong to missing_data.
_POSITIVE_RATIO = Decimal("0.75")
_RISK_RATIO = Decimal("0.35")
_TOP_N = 3


@dataclass(frozen=True)
class IpoDashboardRow:
    """Everything one dashboard card/table row needs, already denormalized."""

    issue_id: int
    company_name: str
    issue_status: IpoStatus
    score: Decimal | None
    recommendation: str | None
    recommendation_type: str | None
    confidence: str | None
    top_positives: tuple[str, ...]
    top_risks: tuple[str, ...]
    missing_data: tuple[str, ...]
    triggered_flags: tuple[str, ...]
    reasons: tuple[str, ...]
    source_documents: tuple[str, ...]
    last_updated: dt.datetime | None
    has_manual_profile: bool
    pending_proposals: int
    documents_downloaded: int
    documents_total: int


@dataclass(frozen=True)
class IpoDashboardSnapshot:
    """One consistent, timestamped read of every scanned IPO filing."""

    generated_at: dt.datetime
    rows: tuple[IpoDashboardRow, ...]


def top_positive_and_risk_reasons(
    evaluation: IpoEvaluationRecord,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Rank factor contributions into headline strengths and risks.

    Beginner note:
        The stored ``contributions`` receipt already carries each factor's
        weighted points, so this is pure arithmetic against ``PDF_WEIGHTS`` —
        no re-scoring. A missing factor contributes zero but is *not* labeled
        a risk here, because "we could not check" (missing_data) and "we
        checked and it is weak" are deliberately different messages.
    """
    missing = set(evaluation.result.missing_data)
    positives: list[tuple[int, str]] = []
    risks: list[tuple[int, str]] = []
    for name, weight in PDF_WEIGHTS.items():
        if name in missing:
            continue
        contribution = evaluation.contributions.get(name)
        if contribution is None:
            continue
        ratio = contribution / Decimal(weight)
        label = f"{name.replace('_', ' ')} ({contribution}/{weight})"
        if ratio >= _POSITIVE_RATIO:
            positives.append((weight, label))
        elif ratio <= _RISK_RATIO:
            risks.append((weight, label))
    positives.sort(key=lambda item: item[0], reverse=True)
    risks.sort(key=lambda item: item[0], reverse=True)
    return (
        tuple(label for _weight, label in positives[:_TOP_N]),
        tuple(label for _weight, label in risks[:_TOP_N]),
    )


def _row_for_issue(
    issue: Any, *, session_factory: SessionFactory
) -> IpoDashboardRow:
    """Denormalize one issue's stored state into a display-ready row."""
    documents = [
        document
        for document in list_documents(issue.id, session_factory=session_factory)
        if document.document_type in {"drhp", "rhp"}
    ]
    downloaded = sum(1 for document in documents if document.content_sha256)
    profile = get_latest_manual_profile(issue.id, session_factory=session_factory)
    pending_proposals = len(
        list_extraction_proposals(
            issue_id=issue.id,
            status=IpoExtractionProposalStatus.PENDING,
            session_factory=session_factory,
        )
    )
    evaluation = get_latest_evaluation(issue.id, session_factory=session_factory)

    if evaluation is None:
        return IpoDashboardRow(
            issue_id=issue.id,
            company_name=issue.company_name,
            issue_status=issue.status,
            score=None,
            recommendation=None,
            recommendation_type=None,
            confidence=None,
            top_positives=(),
            top_risks=(),
            missing_data=(),
            triggered_flags=(),
            reasons=(),
            source_documents=(),
            last_updated=None,
            has_manual_profile=profile is not None,
            pending_proposals=pending_proposals,
            documents_downloaded=downloaded,
            documents_total=len(documents),
        )

    result = evaluation.result
    positives, risks = top_positive_and_risk_reasons(evaluation)
    return IpoDashboardRow(
        issue_id=issue.id,
        company_name=issue.company_name,
        issue_status=issue.status,
        score=result.score,
        recommendation=result.recommendation.value,
        recommendation_type=result.recommendation_type,
        confidence=result.confidence.value,
        top_positives=positives,
        top_risks=risks,
        missing_data=result.missing_data,
        triggered_flags=tuple(
            flag.name
            for flag in result.caution_flags
            if flag.status.value == "triggered"
        ),
        reasons=result.reasons,
        source_documents=result.source_documents,
        last_updated=evaluation.scored_at,
        has_manual_profile=profile is not None,
        pending_proposals=pending_proposals,
        documents_downloaded=downloaded,
        documents_total=len(documents),
    )


def build_dashboard_snapshot(
    *,
    now: dt.datetime | None = None,
    session_factory: SessionFactory = session_scope,
) -> IpoDashboardSnapshot:
    """Read every issue's stored state into one display-ready snapshot.

    Beginner note:
        The per-issue reads are simple repository calls rather than one big
        join because the IPO universe is dozens of issues, not thousands; the
        page additionally caches the snapshot, so clarity wins over query
        golf here.
    """
    when = now if now is not None else dt.datetime.now(dt.UTC)
    rows = tuple(
        _row_for_issue(issue, session_factory=session_factory)
        for issue in list_issues(session_factory=session_factory)
    )
    return IpoDashboardSnapshot(generated_at=when, rows=rows)


def section_available_filings(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Every scanned filing: the complete inventory, whatever its state."""
    return snapshot.rows


def section_open(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Issues whose subscription book is open right now."""
    return tuple(row for row in snapshot.rows if row.issue_status is IpoStatus.OPEN)


def section_upcoming(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """RHP-stage issues expected to open next."""
    return tuple(
        row for row in snapshot.rows if row.issue_status is IpoStatus.RHP_FILED
    )


def section_drhp_watchlist(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Early DRHP-stage filings worth tracking before an RHP lands."""
    return tuple(
        row for row in snapshot.rows if row.issue_status is IpoStatus.DRHP_FILED
    )


def section_recommended(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Issues whose latest verdict is the binary Recommended."""
    return tuple(
        row
        for row in snapshot.rows
        if row.recommendation == Recommendation.RECOMMENDED.value
    )


def section_not_recommended(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Issues whose latest verdict is the binary Not Recommended."""
    return tuple(
        row
        for row in snapshot.rows
        if row.recommendation == Recommendation.NOT_RECOMMENDED.value
    )


def section_missing_data_queue(snapshot: IpoDashboardSnapshot) -> tuple[IpoDashboardRow, ...]:
    """Issues blocked on evidence: the admin's work queue.

    Beginner note:
        An issue lands here when any step of the evidence chain is incomplete:
        no verified manual profile yet, no downloaded prospectus, a factor the
        verdict flagged as missing, or an AI proposal waiting for review.
    """
    return tuple(
        row
        for row in snapshot.rows
        if not row.has_manual_profile
        or row.documents_downloaded == 0
        or row.missing_data
        or row.pending_proposals > 0
    )
