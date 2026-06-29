"""IPO-001 typed repository façade tests."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.ipo.models import (
    Confidence,
    FactorAssessment,
    FinancialPeriodType,
    IpoDocumentData,
    IpoFinancialData,
    IpoIssueData,
    IpoIssueType,
    IpoRecommendationResult,
    IpoScoreInput,
    IpoStatus,
    IpoSubscriptionData,
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    create_document,
    create_financial,
    create_issue,
    create_subscription,
    delete_document,
    delete_evaluation,
    delete_financial,
    delete_issue,
    delete_subscription,
    evaluate_issue,
    get_document,
    get_evaluation,
    get_financial,
    get_issue,
    get_latest_recommendation,
    get_subscription,
    list_documents,
    list_evaluations,
    list_financials,
    list_issues,
    list_subscriptions,
    update_document,
    update_financial,
    update_issue,
    update_subscription,
)
from backend.storage import IpoRecommendation, IpoScore


def _issue_data(**overrides: object) -> IpoIssueData:
    values: dict[str, object] = {
        "company_name": "Example Ltd",
        "issue_type": IpoIssueType.MAINBOARD,
        "status": IpoStatus.OPEN,
        "open_date": dt.date(2026, 7, 1),
        "close_date": dt.date(2026, 7, 3),
        "price_band_low": Decimal("95.00"),
        "price_band_high": Decimal("100.00"),
        "lot_size": 150,
        "fresh_issue_amount": Decimal("5000000000.00"),
        "ofs_amount": Decimal("1000000000.00"),
        "source_url": "https://www.sebi.gov.in/filings/example",
        "source_confidence": Confidence.HIGH,
    }
    values.update(overrides)
    return IpoIssueData(**values)


def test_issue_crud_returns_detached_typed_records(file_session_factory) -> None:
    created = create_issue(_issue_data(), session_factory=file_session_factory)

    assert created.id > 0
    assert created.issue_type is IpoIssueType.MAINBOARD
    assert created.status is IpoStatus.OPEN
    assert created.price_band_high == Decimal("100.00")
    assert get_issue(created.id, session_factory=file_session_factory) == created

    updated = update_issue(
        created.id,
        _issue_data(status=IpoStatus.CLOSED, price_band_high=Decimal("105.00")),
        session_factory=file_session_factory,
    )
    assert updated.id == created.id
    assert updated.status is IpoStatus.CLOSED
    assert updated.price_band_high == Decimal("105.00")

    assert delete_issue(created.id, session_factory=file_session_factory) is True
    assert delete_issue(created.id, session_factory=file_session_factory) is False
    assert get_issue(created.id, session_factory=file_session_factory) is None


def test_list_issues_uses_stable_open_date_then_company_order(file_session_factory) -> None:
    later = create_issue(
        _issue_data(company_name="Zulu Ltd", open_date=dt.date(2026, 8, 1), close_date=None),
        session_factory=file_session_factory,
    )
    alpha = create_issue(
        _issue_data(company_name="Alpha Ltd", open_date=dt.date(2026, 7, 1)),
        session_factory=file_session_factory,
    )
    no_date = create_issue(
        _issue_data(company_name="No Date Ltd", open_date=None, close_date=None),
        session_factory=file_session_factory,
    )

    assert [row.id for row in list_issues(session_factory=file_session_factory)] == [
        later.id,
        alpha.id,
        no_date.id,
    ]


def test_update_missing_issue_raises_typed_not_found(file_session_factory) -> None:
    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        update_issue(999, _issue_data(), session_factory=file_session_factory)


def _document_data(**overrides: object) -> IpoDocumentData:
    values: dict[str, object] = {
        "document_type": "rhp",
        "document_url": "https://www.sebi.gov.in/example-rhp.pdf",
        "source_url": "https://www.sebi.gov.in/filings/example",
        "source_confidence": Confidence.HIGH,
    }
    values.update(overrides)
    return IpoDocumentData(**values)


def test_document_crud_is_scoped_to_its_parent_issue(file_session_factory) -> None:
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    other = create_issue(
        _issue_data(company_name="Other Ltd"), session_factory=file_session_factory
    )

    created = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    assert created.issue_id == issue.id
    assert get_document(
        issue.id, created.id, session_factory=file_session_factory
    ) == created
    assert get_document(
        other.id, created.id, session_factory=file_session_factory
    ) is None

    updated = update_document(
        issue.id,
        created.id,
        _document_data(document_type="drhp"),
        session_factory=file_session_factory,
    )
    assert updated.document_type == "drhp"
    assert [row.id for row in list_documents(issue.id, session_factory=file_session_factory)] == [
        created.id
    ]

    assert delete_document(
        other.id, created.id, session_factory=file_session_factory
    ) is False
    assert delete_document(
        issue.id, created.id, session_factory=file_session_factory
    ) is True
    assert delete_document(
        issue.id, created.id, session_factory=file_session_factory
    ) is False


def test_create_document_requires_an_existing_issue(file_session_factory) -> None:
    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        create_document(999, _document_data(), session_factory=file_session_factory)


def test_financial_crud_normalizes_secret_safe_metrics_and_source_ownership(
    file_session_factory,
) -> None:
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    other = create_issue(
        _issue_data(company_name="Other Ltd"), session_factory=file_session_factory
    )
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    other_document = create_document(
        other.id,
        _document_data(document_url="https://www.sebi.gov.in/other-rhp.pdf"),
        session_factory=file_session_factory,
    )
    data = IpoFinancialData(
        period_end=dt.date(2026, 3, 31),
        period_type=FinancialPeriodType.ANNUAL,
        metrics={"revenue": Decimal("1250000000.00"), "api_key": "do-not-store"},
        source_document_id=document.id,
        source_url="https://www.sebi.gov.in/example-rhp.pdf",
        source_confidence=Confidence.HIGH,
    )

    created = create_financial(issue.id, data, session_factory=file_session_factory)
    assert created.metrics == {
        "revenue": "1250000000.00",
        "api_key": "***REDACTED***",
    }
    assert get_financial(
        issue.id, created.id, session_factory=file_session_factory
    ) == created

    updated = update_financial(
        issue.id,
        created.id,
        IpoFinancialData(
            period_end=dt.date(2026, 3, 31),
            period_type=FinancialPeriodType.ANNUAL,
            metrics={"revenue": Decimal("1300000000.00")},
            source_document_id=document.id,
            source_confidence=Confidence.HIGH,
        ),
        session_factory=file_session_factory,
    )
    assert updated.metrics == {"revenue": "1300000000.00"}
    assert [row.id for row in list_financials(issue.id, session_factory=file_session_factory)] == [
        created.id
    ]

    with pytest.raises(IpoValidationError, match="does not belong"):
        create_financial(
            issue.id,
            IpoFinancialData(
                period_end=dt.date(2025, 3, 31),
                period_type=FinancialPeriodType.ANNUAL,
                metrics={},
                source_document_id=other_document.id,
                source_confidence=Confidence.HIGH,
            ),
            session_factory=file_session_factory,
        )

    assert delete_financial(
        issue.id, created.id, session_factory=file_session_factory
    ) is True
    assert delete_financial(
        issue.id, created.id, session_factory=file_session_factory
    ) is False


def _subscription_data(**overrides: object) -> IpoSubscriptionData:
    values: dict[str, object] = {
        "captured_at": dt.datetime(2026, 7, 2, 10, 30, tzinfo=dt.UTC),
        "qib_multiple": Decimal("25.50"),
        "nii_multiple": Decimal("11.25"),
        "retail_multiple": Decimal("6.75"),
        "total_multiple": Decimal("14.20"),
        "source_url": "https://www.nseindia.com/ipo/example",
        "source_confidence": Confidence.MEDIUM,
    }
    values.update(overrides)
    return IpoSubscriptionData(**values)


def test_subscription_crud_is_timestamp_ordered_and_parent_scoped(
    file_session_factory,
) -> None:
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    first = create_subscription(
        issue.id, _subscription_data(), session_factory=file_session_factory
    )
    later = create_subscription(
        issue.id,
        _subscription_data(captured_at=dt.datetime(2026, 7, 2, 15, 0, tzinfo=dt.UTC)),
        session_factory=file_session_factory,
    )

    assert first.qib_multiple == Decimal("25.50")
    assert get_subscription(
        issue.id, first.id, session_factory=file_session_factory
    ) == first
    assert [
        row.id for row in list_subscriptions(issue.id, session_factory=file_session_factory)
    ] == [later.id, first.id]

    updated = update_subscription(
        issue.id,
        first.id,
        _subscription_data(qib_multiple=Decimal("30.00")),
        session_factory=file_session_factory,
    )
    assert updated.qib_multiple == Decimal("30.00")
    assert delete_subscription(
        issue.id, first.id, session_factory=file_session_factory
    ) is True
    assert delete_subscription(
        issue.id, first.id, session_factory=file_session_factory
    ) is False


def _score_input(company_name: str = "Example Ltd") -> IpoScoreInput:
    def factor(score: object | None, reason: str) -> FactorAssessment:
        return FactorAssessment(score=score, reason=reason)

    return IpoScoreInput(
        company_name=company_name,
        business_quality=factor(90, "Strong business quality"),
        financial_growth=factor(80, "Strong financial growth"),
        return_ratios=factor(75, "Healthy return ratios"),
        valuation=factor(70, "Reasonable peer valuation"),
        qib_subscription=factor(85, "Strong QIB demand"),
        promoter_quality=factor(90, "Experienced promoters"),
        gmp_sentiment=factor(60, "Measured market sentiment"),
        source_documents=("https://www.sebi.gov.in/example-rhp.pdf",),
    )


def test_evaluation_history_is_immutable_ordered_and_deletable_as_a_pair(
    file_session_factory,
) -> None:
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    create_document(issue.id, _document_data(), session_factory=file_session_factory)

    first = evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)
    second = evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)

    assert first.result.recommendation.value == "Recommended"
    assert first.result.to_dict()["source_documents"] == [
        "https://www.sebi.gov.in/example-rhp.pdf"
    ]
    assert get_evaluation(
        issue.id, first.score_id, session_factory=file_session_factory
    ) == first
    assert [
        row.score_id for row in list_evaluations(issue.id, session_factory=file_session_factory)
    ] == [second.score_id, first.score_id]
    assert get_latest_recommendation(
        issue.id, session_factory=file_session_factory
    ) == second.result

    assert delete_evaluation(
        issue.id, first.score_id, session_factory=file_session_factory
    ) is True
    assert delete_evaluation(
        issue.id, first.score_id, session_factory=file_session_factory
    ) is False


def test_get_latest_recommendation_handles_missing_issue_and_empty_history(
    file_session_factory,
) -> None:
    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        get_latest_recommendation(999, session_factory=file_session_factory)

    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    # An existing but unscored issue is distinct from a missing one: it returns
    # None instead of raising, without loading any evaluation history.
    assert (
        get_latest_recommendation(issue.id, session_factory=file_session_factory)
        is None
    )

    create_document(issue.id, _document_data(), session_factory=file_session_factory)
    evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)
    newest = evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)
    assert (
        get_latest_recommendation(issue.id, session_factory=file_session_factory)
        == newest.result
    )


def test_evaluation_rejects_company_or_document_provenance_mismatch(
    file_session_factory,
) -> None:
    issue = create_issue(_issue_data(), session_factory=file_session_factory)

    with pytest.raises(IpoValidationError, match="company_name"):
        evaluate_issue(
            issue.id, _score_input("Wrong Ltd"), session_factory=file_session_factory
        )
    with pytest.raises(IpoValidationError, match="not registered"):
        evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)


def test_evaluation_score_and_verdict_rollback_together(
    file_session_factory, monkeypatch
) -> None:
    from backend.ipo import repository as ipo_repository

    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    create_document(issue.id, _document_data(), session_factory=file_session_factory)
    real_builder = ipo_repository.build_recommendation

    def invalid_builder(score_result) -> IpoRecommendationResult:
        valid = real_builder(score_result)
        return IpoRecommendationResult(
            company_name=valid.company_name,
            score=valid.score,
            recommendation=valid.recommendation,
            recommendation_type="Invalid type",
            confidence=valid.confidence,
            reasons=valid.reasons,
            missing_data=valid.missing_data,
            source_documents=valid.source_documents,
        )

    monkeypatch.setattr(ipo_repository, "build_recommendation", invalid_builder)

    with pytest.raises(IntegrityError):
        evaluate_issue(issue.id, _score_input(), session_factory=file_session_factory)

    with file_session_factory() as session:
        assert session.scalar(select(func.count()).select_from(IpoScore)) == 0
        assert session.scalar(select(func.count()).select_from(IpoRecommendation)) == 0
