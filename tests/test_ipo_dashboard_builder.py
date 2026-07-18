"""IPO-007 dashboard-builder tests.

Beginner note:
The dashboard's rules — which section an issue belongs to, what counts as
missing data, which factors headline as strengths or risks — all live in the
Streamlit-free builder so they can be pinned here without a browser. The
repository reads are monkeypatched at the module seam; everything else runs
for real.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

from backend.ipo import dashboard
from backend.ipo.dashboard import (
    IpoDashboardRow,
    IpoDashboardSnapshot,
    build_dashboard_snapshot,
    section_available_filings,
    section_drhp_watchlist,
    section_missing_data_queue,
    section_not_recommended,
    section_open,
    section_recommended,
    section_upcoming,
    top_positive_and_risk_reasons,
)
from backend.ipo.models import (
    Confidence,
    IpoCautionFlag,
    IpoCautionFlagStatus,
    IpoEvaluationRecord,
    IpoExtractionProposalStatus,
    IpoRecommendationResult,
    IpoStatus,
    Recommendation,
)

_SCORED_AT = dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC)


def _evaluation(
    *,
    contributions: dict[str, str],
    missing: tuple[str, ...] = (),
    recommendation: Recommendation = Recommendation.RECOMMENDED,
    recommendation_type: str = "Apply confidently and consider holding if allotted",
    flags: tuple[IpoCautionFlag, ...] = (),
) -> IpoEvaluationRecord:
    """Build one detached evaluation record for selection tests."""
    result = IpoRecommendationResult(
        company_name="Example Ltd",
        score=Decimal("81.25"),
        recommendation=recommendation,
        recommendation_type=recommendation_type,
        confidence=Confidence.HIGH,
        reasons=("Financial growth: strong.",),
        missing_data=missing,
        source_documents=("https://www.sebi.gov.in/filings/example-rhp",),
        caution_flags=flags,
    )
    return IpoEvaluationRecord(
        issue_id=1,
        score_id=10,
        recommendation_id=11,
        model_version="ipo-006-v1",
        scored_at=_SCORED_AT,
        result=result,
        inputs_fingerprint="f" * 64,
        contributions={name: Decimal(value) for name, value in contributions.items()},
    )


def test_top_reasons_rank_by_weight_and_exclude_missing_factors() -> None:
    """Strengths/risks come from contribution ratios, never from gaps."""
    evaluation = _evaluation(
        contributions={
            "business_quality": "21.25",  # 85% of 25 -> strength
            "financial_growth": "5.00",  # 25% of 20 -> risk
            "return_ratios": "8.25",  # 55% of 15 -> neither
            "valuation": "0.00",  # missing -> excluded entirely
            "qib_subscription": "8.50",  # 85% of 10 -> strength
            "promoter_quality": "2.00",  # 20% of 10 -> risk
            "gmp_sentiment": "0.00",  # missing -> excluded entirely
        },
        missing=("valuation", "gmp_sentiment"),
    )

    positives, risks = top_positive_and_risk_reasons(evaluation)

    assert positives == (
        "business quality (21.25/25)",
        "qib subscription (8.50/10)",
    )
    assert risks == (
        "financial growth (5.00/20)",
        "promoter quality (2.00/10)",
    )


def test_top_reasons_rank_by_awarded_and_lost_points_not_factor_weight() -> None:
    """Impact ordering uses actual contribution and loss with stable ties."""
    evaluation = _evaluation(
        contributions={
            "business_quality": "19.00",
            "financial_growth": "20.00",
            "return_ratios": "5.00",
            "valuation": "0.00",
            "qib_subscription": "8.00",
            "promoter_quality": "10.00",
            "gmp_sentiment": "5.00",
        }
    )

    positives, risks = top_positive_and_risk_reasons(evaluation)

    assert positives[:2] == (
        "financial growth (20.00/20)",
        "business quality (19.00/25)",
    )
    assert risks[:2] == (
        "valuation (0.00/15)",
        "return ratios (5.00/15)",
    )


def _row(**overrides: Any) -> IpoDashboardRow:
    """Build one display row; scenarios override the classifying fields."""
    values: dict[str, Any] = {
        "issue_id": 1,
        "company_name": "Example Ltd",
        "issue_status": IpoStatus.OPEN,
        "score": Decimal("81.25"),
        "recommendation": "Recommended",
        "recommendation_type": "Apply confidently and consider holding if allotted",
        "confidence": "high",
        "top_positives": ("business quality (21.25/25)",),
        "top_risks": (),
        "missing_data": (),
        "triggered_flags": (),
        "reasons": ("Financial growth: strong.",),
        "source_documents": ("https://www.sebi.gov.in/filings/example-rhp",),
        "last_updated": _SCORED_AT,
        "has_manual_profile": True,
        "pending_proposals": 0,
        "documents_downloaded": 1,
        "documents_total": 1,
    }
    values.update(overrides)
    return IpoDashboardRow(**values)


def test_sections_classify_by_lifecycle_and_verdict() -> None:
    """Each spec section selects exactly its lifecycle or verdict slice."""
    open_row = _row(issue_id=1)
    upcoming = _row(issue_id=2, issue_status=IpoStatus.RHP_FILED)
    watchlist = _row(
        issue_id=3,
        issue_status=IpoStatus.DRHP_FILED,
        recommendation="Not Recommended",
        recommendation_type="Skip",
    )
    snapshot = IpoDashboardSnapshot(
        generated_at=_SCORED_AT, rows=(open_row, upcoming, watchlist)
    )

    assert section_available_filings(snapshot) == snapshot.rows
    assert section_open(snapshot) == (open_row,)
    assert section_upcoming(snapshot) == (upcoming,)
    assert section_drhp_watchlist(snapshot) == (watchlist,)
    assert section_recommended(snapshot) == (open_row, upcoming)
    assert section_not_recommended(snapshot) == (watchlist,)


def test_missing_data_queue_catches_every_evidence_gap() -> None:
    """Any incomplete evidence chain routes an issue to the admin queue."""
    complete = _row(issue_id=1)
    no_profile = _row(issue_id=2, has_manual_profile=False)
    no_download = _row(issue_id=3, documents_downloaded=0)
    factor_gap = _row(issue_id=4, missing_data=("qib_subscription",))
    awaiting_review = _row(issue_id=5, pending_proposals=2)
    snapshot = IpoDashboardSnapshot(
        generated_at=_SCORED_AT,
        rows=(complete, no_profile, no_download, factor_gap, awaiting_review),
    )

    queued = section_missing_data_queue(snapshot)

    assert [row.issue_id for row in queued] == [2, 3, 4, 5]


def test_build_snapshot_denormalizes_stored_state_per_issue(monkeypatch) -> None:
    """The builder reads repositories only and flattens them into rows."""
    issue_updated = _SCORED_AT - dt.timedelta(days=2)
    issues = [
        SimpleNamespace(
            id=1,
            company_name="Scored Ltd",
            status=IpoStatus.OPEN,
            updated_at=issue_updated,
        ),
        SimpleNamespace(
            id=2,
            company_name="Fresh Ltd",
            status=IpoStatus.DRHP_FILED,
            updated_at=issue_updated,
        ),
    ]
    documents = {
        1: [
            SimpleNamespace(
                document_type="rhp",
                document_url="https://www.sebi.gov.in/scored-rhp",
                content_sha256="a" * 64,
                created_at=issue_updated,
                downloaded_at=issue_updated,
            )
        ],
        2: [
            SimpleNamespace(
                document_type="drhp",
                document_url="https://www.sebi.gov.in/fresh-drhp",
                content_sha256=None,
                created_at=issue_updated,
                downloaded_at=None,
            )
        ],
    }
    evaluation = _evaluation(
        contributions={"business_quality": "21.25"},
        flags=(
            IpoCautionFlag(
                name="very_expensive_valuation",
                status=IpoCautionFlagStatus.TRIGGERED,
                evidence="P/E premium 1.60x.",
            ),
        ),
    )
    monkeypatch.setattr(dashboard, "list_issues", lambda **_kwargs: issues)
    monkeypatch.setattr(
        dashboard,
        "list_documents",
        lambda issue_id, **_kwargs: documents[issue_id],
    )
    monkeypatch.setattr(
        dashboard,
        "get_latest_manual_profile",
        lambda issue_id, **_kwargs: (
            SimpleNamespace(
                source_document_url="https://www.sebi.gov.in/scored-rhp",
                submitted_at=issue_updated,
            )
            if issue_id == 1
            else None
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "list_extraction_proposals",
        lambda **kwargs: (
            [
                SimpleNamespace(
                    status=IpoExtractionProposalStatus.PENDING,
                    created_at=issue_updated,
                    reviewed_at=None,
                )
            ]
            if kwargs.get("issue_id") == 2
            else []
        ),
    )
    monkeypatch.setattr(
        dashboard,
        "list_subscriptions",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        dashboard,
        "list_enrichment_signals",
        lambda *_args, **_kwargs: [],
    )
    monkeypatch.setattr(
        dashboard,
        "get_latest_evaluation",
        lambda issue_id, **_kwargs: evaluation if issue_id == 1 else None,
    )

    snapshot = build_dashboard_snapshot(now=_SCORED_AT, session_factory=object)

    assert snapshot.generated_at == _SCORED_AT
    scored, fresh = snapshot.rows
    assert scored.score == Decimal("81.25")
    assert scored.triggered_flags == ("very_expensive_valuation",)
    assert scored.documents_downloaded == 1 and scored.documents_total == 1
    assert scored.has_manual_profile is True
    assert fresh.score is None
    assert fresh.recommendation is None
    assert fresh.pending_proposals == 1
    assert fresh.documents_downloaded == 0
    assert fresh.last_updated == issue_updated
    assert fresh.source_documents == (
        "https://www.sebi.gov.in/fresh-drhp",
    )


def test_newer_evidence_marks_the_displayed_evaluation_stale(
    monkeypatch,
) -> None:
    """Fresh evidence after scored_at routes the issue back to the review queue."""
    newer = _SCORED_AT + dt.timedelta(hours=1)
    issue = SimpleNamespace(
        id=1,
        company_name="Scored Ltd",
        status=IpoStatus.OPEN,
        updated_at=_SCORED_AT - dt.timedelta(days=1),
    )
    document = SimpleNamespace(
        document_type="rhp",
        document_url="https://www.sebi.gov.in/scored-rhp",
        content_sha256="a" * 64,
        created_at=_SCORED_AT,
        downloaded_at=_SCORED_AT,
    )
    monkeypatch.setattr(dashboard, "list_issues", lambda **_kwargs: [issue])
    monkeypatch.setattr(
        dashboard, "list_documents", lambda *_args, **_kwargs: [document]
    )
    monkeypatch.setattr(
        dashboard,
        "get_latest_manual_profile",
        lambda *_args, **_kwargs: SimpleNamespace(
            source_document_url=document.document_url,
            submitted_at=_SCORED_AT,
        ),
    )
    monkeypatch.setattr(
        dashboard, "list_extraction_proposals", lambda **_kwargs: []
    )
    monkeypatch.setattr(
        dashboard,
        "list_subscriptions",
        lambda *_args, **_kwargs: [
            SimpleNamespace(captured_at=newer, created_at=newer)
        ],
    )
    monkeypatch.setattr(
        dashboard, "list_enrichment_signals", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        dashboard,
        "get_latest_evaluation",
        lambda *_args, **_kwargs: _evaluation(
            contributions={"business_quality": "21.25"}
        ),
    )

    snapshot = build_dashboard_snapshot(
        now=newer,
        session_factory=object,
    )

    assert snapshot.rows[0].evaluation_stale is True
    assert snapshot.rows[0].last_updated == newer
    assert section_missing_data_queue(snapshot) == snapshot.rows
