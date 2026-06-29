"""IPO-001 ORM schema tests for the six persistent tables."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage import (
    IpoDocument,
    IpoFinancial,
    IpoIssue,
    IpoRecommendation,
    IpoScore,
    IpoSubscription,
)


def _issue(**overrides: object) -> IpoIssue:
    values: dict[str, object] = {
        "company_name": "Example Ltd",
        "issue_type": "mainboard",
        "status": "open",
        "open_date": dt.date(2026, 7, 1),
        "close_date": dt.date(2026, 7, 3),
        "price_band_low": Decimal("95.00"),
        "price_band_high": Decimal("100.00"),
        "lot_size": 150,
        "fresh_issue_amount": Decimal("5000000000.00"),
        "ofs_amount": Decimal("1000000000.00"),
        "source_url": "https://www.sebi.gov.in/filings/example",
        "source_confidence": "high",
    }
    values.update(overrides)
    return IpoIssue(**values)


def test_all_six_ipo_tables_round_trip_with_exact_money(db_session: Session) -> None:
    issue = _issue()
    document = IpoDocument(
        issue=issue,
        document_type="rhp",
        document_url="https://www.sebi.gov.in/example-rhp.pdf",
        source_url="https://www.sebi.gov.in/filings/example",
        source_confidence="high",
    )
    financial = IpoFinancial(
        issue=issue,
        period_end=dt.date(2026, 3, 31),
        period_type="annual",
        metrics_json={"revenue": "1250000000.00", "roe_pct": "18.50"},
        source_document=document,
        source_confidence="high",
    )
    subscription = IpoSubscription(
        issue=issue,
        captured_at=dt.datetime(2026, 7, 2, 10, 30, tzinfo=dt.UTC),
        qib_multiple=Decimal("25.50"),
        nii_multiple=Decimal("11.25"),
        retail_multiple=Decimal("6.75"),
        total_multiple=Decimal("14.20"),
        source_url="https://www.nseindia.com/ipo/example",
        source_confidence="medium",
    )
    score = IpoScore(
        issue=issue,
        business_quality=Decimal("90.00"),
        financial_growth=Decimal("80.00"),
        return_ratios=Decimal("75.00"),
        valuation=Decimal("70.00"),
        qib_subscription=Decimal("85.00"),
        promoter_quality=Decimal("90.00"),
        gmp_sentiment=Decimal("60.00"),
        total_score=Decimal("81.25"),
        contributions_json={"business_quality": "22.50"},
        missing_data_json=[],
        reasons_json=["Strong business quality"],
        model_version="ipo-001-v1",
    )
    recommendation = IpoRecommendation(
        score=score,
        recommendation="Recommended",
        recommendation_type="Apply confidently and consider holding if allotted",
        confidence="high",
        reasons_json=["Strong business quality"],
        missing_data_json=[],
        source_documents_json=["https://www.sebi.gov.in/example-rhp.pdf"],
    )
    db_session.add_all([issue, financial, subscription, score, recommendation])
    db_session.flush()

    assert issue.price_band_high == Decimal("100.00")
    assert issue.fresh_issue_amount == Decimal("5000000000.00")
    assert document.issue_id == issue.id
    assert financial.source_document_id == document.id
    assert subscription.qib_multiple == Decimal("25.50")
    assert score.recommendation is recommendation
    assert recommendation.score_id == score.id


@pytest.mark.parametrize(
    ("field", "value"),
    [("issue_type", "rights_issue"), ("status", "rumoured")],
)
def test_issue_check_constraints_reject_unknown_enum_values(
    db_session: Session, field: str, value: str
) -> None:
    db_session.add(_issue(**{field: value}))

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_one_recommendation_is_allowed_per_immutable_score(db_session: Session) -> None:
    issue = _issue()
    score = IpoScore(
        issue=issue,
        total_score=Decimal("78.00"),
        contributions_json={},
        missing_data_json=[],
        reasons_json=[],
        model_version="ipo-001-v1",
    )
    db_session.add_all(
        [
            IpoRecommendation(
                score=score,
                recommendation="Recommended",
                recommendation_type="Apply primarily for listing gains",
                confidence="high",
                reasons_json=[],
                missing_data_json=[],
                source_documents_json=[],
            ),
            IpoRecommendation(
                score=score,
                recommendation="Recommended",
                recommendation_type="Apply primarily for listing gains",
                confidence="high",
                reasons_json=[],
                missing_data_json=[],
                source_documents_json=[],
            ),
        ]
    )

    with pytest.raises(IntegrityError):
        db_session.flush()


def test_deleting_issue_cascades_to_every_ipo_child_table(db_session: Session) -> None:
    issue = _issue()
    document = IpoDocument(
        issue=issue,
        document_type="rhp",
        document_url="https://www.sebi.gov.in/example-rhp.pdf",
        source_confidence="high",
    )
    financial = IpoFinancial(
        issue=issue,
        period_end=dt.date(2026, 3, 31),
        period_type="annual",
        metrics_json={},
        source_document=document,
        source_confidence="high",
    )
    subscription = IpoSubscription(
        issue=issue,
        captured_at=dt.datetime(2026, 7, 2, tzinfo=dt.UTC),
        source_confidence="medium",
    )
    score = IpoScore(
        issue=issue,
        total_score=Decimal("50.00"),
        contributions_json={},
        missing_data_json=[],
        reasons_json=[],
        model_version="ipo-001-v1",
    )
    recommendation = IpoRecommendation(
        score=score,
        recommendation="Not Recommended",
        recommendation_type="Skip",
        confidence="low",
        reasons_json=[],
        missing_data_json=[],
        source_documents_json=[],
    )
    db_session.add_all([issue, financial, subscription, score, recommendation])
    db_session.flush()

    db_session.delete(issue)
    db_session.flush()

    for model in (
        IpoIssue,
        IpoDocument,
        IpoFinancial,
        IpoSubscription,
        IpoScore,
        IpoRecommendation,
    ):
        assert db_session.scalar(select(func.count()).select_from(model)) == 0

