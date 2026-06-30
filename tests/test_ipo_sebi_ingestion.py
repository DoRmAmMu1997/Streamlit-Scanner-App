"""IPO-002 atomic ingestion and ownership tests."""

from __future__ import annotations

import datetime as dt
import hashlib

import pytest

from backend.ipo.models import (
    Confidence,
    IpoDocumentData,
    IpoFilingData,
    IpoIssueData,
    IpoIssueType,
    IpoStatus,
    IpoValidationError,
)
from backend.ipo.repository import (
    create_document,
    create_issue,
    get_latest_filing_date,
    ingest_filings,
    list_documents,
    list_issues,
)


def _filing(
    *,
    company_name: str = "Example Limited",
    company_key: str = "example limited",
    document_type: str = "drhp",
    filing_date: dt.date = dt.date(2026, 6, 26),
    document_url: str = "https://www.sebi.gov.in/filings/example-drhp.html",
    record_hash: str | None = None,
) -> IpoFilingData:
    smid = {"drhp": 10, "rhp": 11, "final_offer": 12}[document_type]
    status = {
        "drhp": IpoStatus.DRHP_FILED,
        "rhp": IpoStatus.RHP_FILED,
        "final_offer": IpoStatus.CLOSED,
    }[document_type]
    return IpoFilingData(
        company_name=company_name,
        sebi_company_key=company_key,
        issue_type=IpoIssueType.UNKNOWN,
        status=status,
        document_type=document_type,
        filing_date=filing_date,
        document_url=document_url,
        source_url=(
            "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?"
            f"doListing=yes&sid=3&smid={smid}&ssid=15"
        ),
        record_hash=record_hash or hashlib.sha256(
            f"{company_key}|{document_type}|{filing_date}|{document_url}".encode()
        ).hexdigest(),
    )


def test_ingest_creates_issue_document_and_is_repeat_run_idempotent(
    file_session_factory,
) -> None:
    filing = _filing()

    first = ingest_filings((filing,), session_factory=file_session_factory)
    second = ingest_filings((filing,), session_factory=file_session_factory)

    assert first.received == 1
    assert first.issues_created == 1
    assert first.documents_created == 1
    assert second.unchanged == 1
    assert second.issues_created == second.documents_created == 0
    issue = list_issues(session_factory=file_session_factory)[0]
    assert issue.sebi_company_key == "example limited"
    assert issue.status is IpoStatus.DRHP_FILED
    document = list_documents(issue.id, session_factory=file_session_factory)[0]
    assert document.filing_date == dt.date(2026, 6, 26)
    assert document.record_hash == filing.record_hash


def test_later_documents_advance_status_but_older_filings_never_regress_it(
    file_session_factory,
) -> None:
    ingest_filings((_filing(),), session_factory=file_session_factory)
    ingest_filings(
        (
            _filing(
                document_type="rhp",
                filing_date=dt.date(2026, 7, 1),
                document_url="https://www.sebi.gov.in/filings/example-rhp.html",
            ),
        ),
        session_factory=file_session_factory,
    )
    older = ingest_filings(
        (
            _filing(
                filing_date=dt.date(2026, 6, 20),
                document_url="https://www.sebi.gov.in/filings/example-older-drhp.html",
            ),
        ),
        session_factory=file_session_factory,
    )

    issue = list_issues(session_factory=file_session_factory)[0]
    assert issue.status is IpoStatus.RHP_FILED
    assert len(list_documents(issue.id, session_factory=file_session_factory)) == 3
    assert older.issues_updated == 0


def test_single_case_insensitive_legacy_company_is_claimed_once(file_session_factory) -> None:
    legacy = create_issue(
        IpoIssueData(
            company_name="EXAMPLE LIMITED",
            issue_type=IpoIssueType.MAINBOARD,
            status=IpoStatus.DRHP_FILED,
            source_confidence=Confidence.MEDIUM,
        ),
        session_factory=file_session_factory,
    )

    summary = ingest_filings((_filing(),), session_factory=file_session_factory)

    issues = list_issues(session_factory=file_session_factory)
    assert [issue.id for issue in issues] == [legacy.id]
    assert issues[0].sebi_company_key == "example limited"
    assert issues[0].issue_type is IpoIssueType.MAINBOARD
    assert issues[0].source_confidence is Confidence.HIGH
    assert "smid=10" in (issues[0].source_url or "")
    assert summary.issues_updated == 1


def test_cross_issue_fingerprint_or_url_conflict_rolls_back_whole_batch(
    file_session_factory,
) -> None:
    owner = create_issue(
        IpoIssueData(
            company_name="Owner Limited",
            issue_type=IpoIssueType.UNKNOWN,
            status=IpoStatus.DRHP_FILED,
            source_confidence=Confidence.HIGH,
            sebi_company_key="owner limited",
        ),
        session_factory=file_session_factory,
    )
    shared_hash = "c" * 64
    create_document(
        owner.id,
        IpoDocumentData(
            document_type="drhp",
            document_url="https://www.sebi.gov.in/filings/owned.html",
            source_confidence=Confidence.HIGH,
            filing_date=dt.date(2026, 6, 20),
            record_hash=shared_hash,
        ),
        session_factory=file_session_factory,
    )

    with pytest.raises(IpoValidationError, match="owned by another IPO issue"):
        ingest_filings(
            (
                _filing(
                    company_name="Safe Limited",
                    company_key="safe limited",
                    document_url="https://www.sebi.gov.in/filings/safe.html",
                ),
                _filing(
                    company_name="Conflict Limited",
                    company_key="conflict limited",
                    document_url="https://www.sebi.gov.in/filings/conflict.html",
                    record_hash=shared_hash,
                ),
            ),
            session_factory=file_session_factory,
        )

    assert [issue.company_name for issue in list_issues(session_factory=file_session_factory)] == [
        "Owner Limited"
    ]


def test_same_issue_url_match_claims_missing_fingerprint_and_updates_metadata(
    file_session_factory,
) -> None:
    issue = create_issue(
        IpoIssueData(
            company_name="Example Limited",
            issue_type=IpoIssueType.UNKNOWN,
            status=IpoStatus.DRHP_FILED,
            source_confidence=Confidence.HIGH,
            sebi_company_key="example limited",
        ),
        session_factory=file_session_factory,
    )
    create_document(
        issue.id,
        IpoDocumentData(
            document_type="drhp",
            document_url="https://www.sebi.gov.in/filings/example-drhp.html",
            source_confidence=Confidence.MEDIUM,
        ),
        session_factory=file_session_factory,
    )

    summary = ingest_filings((_filing(),), session_factory=file_session_factory)

    assert summary.documents_updated == 1
    document = list_documents(issue.id, session_factory=file_session_factory)[0]
    assert document.record_hash == _filing().record_hash
    assert document.filing_date == dt.date(2026, 6, 26)


def test_latest_filing_date_is_the_global_ingestion_watermark(file_session_factory) -> None:
    assert get_latest_filing_date(session_factory=file_session_factory) is None
    ingest_filings(
        (
            _filing(filing_date=dt.date(2026, 6, 20)),
            _filing(
                company_name="Other Limited",
                company_key="other limited",
                filing_date=dt.date(2026, 6, 29),
                document_url="https://www.sebi.gov.in/filings/other.html",
            ),
        ),
        session_factory=file_session_factory,
    )

    assert get_latest_filing_date(session_factory=file_session_factory) == dt.date(2026, 6, 29)
