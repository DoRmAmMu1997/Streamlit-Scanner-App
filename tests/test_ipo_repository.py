"""IPO-001 typed repository façade tests."""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from backend.ipo.documents.downloader import (
    IpoDocumentDownloadError,
    IpoDocumentDownloadErrorCode,
    IpoDocumentDownloadResult,
)
from backend.ipo.models import (
    Confidence,
    FactorAssessment,
    FinancialPeriodType,
    IpoDocumentData,
    IpoDocumentParseStatus,
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
    download_document,
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
    """Build the reusable issue data fixture used by the scenarios below."""
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
    """Pin issue crud returns detached typed records as an executable IPO regression contract."""
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
    """Pin list issues uses stable open date then company order as an executable IPO regression contract."""
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
    """Pin update missing issue raises typed not found as an executable IPO regression contract."""
    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        update_issue(999, _issue_data(), session_factory=file_session_factory)


def _document_data(**overrides: object) -> IpoDocumentData:
    """Build the reusable document data fixture used by the scenarios below."""
    values: dict[str, object] = {
        "document_type": "rhp",
        "document_url": "https://www.sebi.gov.in/example-rhp.pdf",
        "source_url": "https://www.sebi.gov.in/filings/example",
        "source_confidence": Confidence.HIGH,
    }
    values.update(overrides)
    return IpoDocumentData(**values)


def test_document_crud_is_scoped_to_its_parent_issue(file_session_factory) -> None:
    """Pin document crud is scoped to its parent issue as an executable IPO regression contract."""
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
    """Pin create document requires an existing issue as an executable IPO regression contract."""
    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        create_document(999, _document_data(), session_factory=file_session_factory)


def _download_result(document_id: int) -> IpoDocumentDownloadResult:
    """Build the frozen provenance returned by the filesystem downloader."""
    digest = "c" * 64
    return IpoDocumentDownloadResult(
        document_id=document_id,
        content_sha256=digest,
        downloaded_at=dt.datetime(2026, 6, 30, 12, tzinfo=dt.UTC),
        file_path=f"ipo/documents/{digest}.pdf",
        page_count=None,
        parse_status=IpoDocumentParseStatus.PENDING,
        cache_hit=False,
        bytes_written=512,
    )


def test_download_document_closes_read_transaction_before_downloader(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """A slow PDF transfer must never retain a database transaction or lock."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    active_transactions = 0

    @contextmanager
    def tracked_session_factory():
        """Count open sessions so the downloader can assert the lock-free gap."""
        nonlocal active_transactions
        with file_session_factory() as session:
            active_transactions += 1
            try:
                yield session
            finally:
                active_transactions -= 1

    def fake_downloader(record, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Return verified metadata only after proving no session remains open."""
        assert active_transactions == 0
        return _download_result(record.id)

    result = download_document(
        issue.id,
        document.id,
        data_dir=tmp_path,
        downloader=fake_downloader,
        session_factory=tracked_session_factory,
    )

    stored = get_document(issue.id, document.id, session_factory=file_session_factory)
    assert result == _download_result(document.id)
    assert stored is not None
    assert stored.content_sha256 == "c" * 64
    assert stored.parse_status is IpoDocumentParseStatus.PENDING
    assert stored.page_count is None


def test_download_failure_clears_metadata_records_safe_audit_and_reraises(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """A failed transfer leaves no trusted path/hash and emits only an error code."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    audits: list[dict[str, Any]] = []

    def failing_downloader(*_args: object, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Model a categorized network failure without unsafe exception text."""
        raise IpoDocumentDownloadError(IpoDocumentDownloadErrorCode.NETWORK_ERROR)

    def capture_audit(**kwargs: Any) -> bool:
        """Capture the secondary audit call for exact secret-safe assertions."""
        audits.append(kwargs)
        return True

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document(
            issue.id,
            document.id,
            data_dir=tmp_path,
            downloader=failing_downloader,
            audit_recorder=capture_audit,
            session_factory=file_session_factory,
        )

    stored = get_document(issue.id, document.id, session_factory=file_session_factory)
    assert caught.value.code is IpoDocumentDownloadErrorCode.NETWORK_ERROR
    assert stored is not None
    assert stored.parse_status is IpoDocumentParseStatus.DOWNLOAD_FAILED
    assert stored.content_sha256 is None
    assert stored.downloaded_at is None
    assert stored.file_path is None
    assert audits[0]["metadata"] == {
        "issue_id": issue.id,
        "document_id": document.id,
        "document_type": "rhp",
        "error_code": "network_error",
    }


def test_final_offer_is_rejected_without_calling_downloader(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """IPO-003 downloads only DRHP/RHP records and leaves final offers metadata-only."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id,
        _document_data(document_type="final_offer"),
        session_factory=file_session_factory,
    )
    called = False

    def forbidden_downloader(*_args: object, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Fail loudly if unsupported final-offer metadata reaches networking."""
        nonlocal called
        called = True
        raise AssertionError("downloader must not be called")

    with pytest.raises(IpoValidationError, match="DRHP or RHP"):
        download_document(
            issue.id,
            document.id,
            data_dir=tmp_path,
            downloader=forbidden_downloader,
            session_factory=file_session_factory,
        )

    assert called is False


def test_changing_document_source_invalidates_download_provenance(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """A cache digest for old source metadata must not survive a source edit."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    download_document(
        issue.id,
        document.id,
        data_dir=tmp_path,
        downloader=lambda record, **_kwargs: _download_result(record.id),
        session_factory=file_session_factory,
    )

    updated = update_document(
        issue.id,
        document.id,
        _document_data(document_url="https://www.sebi.gov.in/filings/revised-rhp.html"),
        session_factory=file_session_factory,
    )

    assert updated.parse_status is IpoDocumentParseStatus.NOT_DOWNLOADED
    assert updated.content_sha256 is None
    assert updated.downloaded_at is None
    assert updated.file_path is None


def test_source_change_during_download_cannot_attach_stale_bytes(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """Bind downloaded bytes to the same source identity read before HTTP.

    Network I/O intentionally runs outside a transaction. A concurrent source
    correction can therefore happen while bytes are in flight. The final short
    transaction must compare-and-set against the detached URL and type instead
    of silently attaching old bytes to the corrected filing record.
    """
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    revised_url = "https://www.sebi.gov.in/filings/revised-rhp.html"

    def racing_downloader(record, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Simulate an operator correcting provenance while HTTP is in flight."""
        update_document(
            issue.id,
            document.id,
            _document_data(document_url=revised_url),
            session_factory=file_session_factory,
        )
        return _download_result(record.id)

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document(
            issue.id,
            document.id,
            data_dir=tmp_path,
            downloader=racing_downloader,
            session_factory=file_session_factory,
        )

    stored = get_document(issue.id, document.id, session_factory=file_session_factory)
    assert caught.value.code is IpoDocumentDownloadErrorCode.SOURCE_CHANGED
    assert stored is not None
    assert stored.document_url == revised_url
    assert stored.parse_status is IpoDocumentParseStatus.NOT_DOWNLOADED
    assert stored.content_sha256 is None
    assert stored.downloaded_at is None
    assert stored.file_path is None


def test_source_change_during_failed_download_preserves_corrected_state(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """Do not mark a corrected source failed for an older request's error."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )
    revised_url = "https://www.sebi.gov.in/filings/corrected-rhp.html"

    def racing_failure(*_args: object, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Correct the source, then fail the request that used its old identity."""
        update_document(
            issue.id,
            document.id,
            _document_data(document_url=revised_url),
            session_factory=file_session_factory,
        )
        raise IpoDocumentDownloadError(IpoDocumentDownloadErrorCode.NETWORK_ERROR)

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document(
            issue.id,
            document.id,
            data_dir=tmp_path,
            downloader=racing_failure,
            session_factory=file_session_factory,
        )

    stored = get_document(issue.id, document.id, session_factory=file_session_factory)
    assert caught.value.code is IpoDocumentDownloadErrorCode.SOURCE_CHANGED
    assert stored is not None
    assert stored.document_url == revised_url
    assert stored.parse_status is IpoDocumentParseStatus.NOT_DOWNLOADED


def test_audit_sink_failure_does_not_hide_authoritative_download_status(
    file_session_factory,
    tmp_path: Path,
) -> None:
    """Keep the database status and stable error authoritative over audit I/O."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id, _document_data(), session_factory=file_session_factory
    )

    def failing_downloader(*_args: object, **_kwargs: object) -> IpoDocumentDownloadResult:
        """Return the stable network category used by the repository contract."""
        raise IpoDocumentDownloadError(IpoDocumentDownloadErrorCode.NETWORK_ERROR)

    def broken_audit_sink(**_kwargs: object) -> bool:
        """Model a secondary audit store outage without leaking its exception."""
        raise RuntimeError("audit database unavailable")

    with pytest.raises(IpoDocumentDownloadError) as caught:
        download_document(
            issue.id,
            document.id,
            data_dir=tmp_path,
            downloader=failing_downloader,
            audit_recorder=broken_audit_sink,
            session_factory=file_session_factory,
        )

    stored = get_document(issue.id, document.id, session_factory=file_session_factory)
    assert caught.value.code is IpoDocumentDownloadErrorCode.NETWORK_ERROR
    assert stored is not None
    assert stored.parse_status is IpoDocumentParseStatus.DOWNLOAD_FAILED


def test_financial_crud_normalizes_secret_safe_metrics_and_source_ownership(
    file_session_factory,
) -> None:
    """Keep financial JSON secret-safe and document provenance issue-scoped."""
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
    """Build the reusable subscription data fixture used by the scenarios below."""
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
    """Pin subscription crud is timestamp ordered and parent scoped as an executable IPO regression contract."""
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
    """Build one complete deterministic score input tied to a registered URL."""
    def factor(score: object | None, reason: str) -> FactorAssessment:
        """Keep seven test factors concise while retaining readable reasons."""
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
    """Pin evaluation history is immutable ordered and deletable as a pair as an executable IPO regression contract."""
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
    """Distinguish a missing issue from a valid issue with no evaluation yet."""
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
    """Pin evaluation rejects company or document provenance mismatch as an executable IPO regression contract."""
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
    """Pin evaluation score and verdict rollback together as an executable IPO regression contract."""
    from backend.ipo import repository as ipo_repository

    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    create_document(issue.id, _document_data(), session_factory=file_session_factory)
    real_builder = ipo_repository.build_recommendation

    def invalid_builder(score_result) -> IpoRecommendationResult:
        """Create a verdict that violates the recommendation-type CHECK."""
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
