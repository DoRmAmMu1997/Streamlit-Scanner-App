"""IPO-002 frozen contracts and deterministic identity tests."""

from __future__ import annotations

import datetime as dt
from dataclasses import FrozenInstanceError

import pytest

from backend.ipo.models import (
    Confidence,
    IpoDocumentData,
    IpoFilingData,
    IpoIssueData,
    IpoIssueType,
    IpoStatus,
    IpoValidationError,
    SebiFiling,
    SebiFilingCategory,
)


def test_unknown_issue_type_is_supported_by_the_domain() -> None:
    issue = IpoIssueData(
        company_name="Example Limited",
        issue_type="UNKNOWN",
        status=IpoStatus.DRHP_FILED,
        source_confidence=Confidence.HIGH,
    )

    assert issue.issue_type is IpoIssueType.UNKNOWN


def test_sebi_filing_is_frozen_and_validates_its_date_window() -> None:
    filing = SebiFiling(
        category=SebiFilingCategory.DRHP,
        title="Example Limited - Draft Red Herring Prospectus",
        filing_date=dt.date(2026, 6, 26),
        document_url="https://www.sebi.gov.in/filings/example.html",
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do",
    )

    with pytest.raises(FrozenInstanceError):
        filing.title = "Changed"  # type: ignore[misc]


def test_normalized_filing_requires_a_sha256_fingerprint() -> None:
    with pytest.raises(IpoValidationError, match="record_hash"):
        IpoFilingData(
            company_name="Example Limited",
            sebi_company_key="example limited",
            issue_type=IpoIssueType.UNKNOWN,
            status=IpoStatus.DRHP_FILED,
            document_type="drhp",
            filing_date=dt.date(2026, 6, 26),
            document_url="https://www.sebi.gov.in/filings/example.html",
            source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do",
            record_hash="not-a-sha256",
        )


def test_manual_issue_and_document_contracts_keep_new_fields_nullable() -> None:
    issue = IpoIssueData(
        company_name="Legacy Limited",
        issue_type=IpoIssueType.MAINBOARD,
        status=IpoStatus.OPEN,
        source_confidence=Confidence.MEDIUM,
    )
    document = IpoDocumentData(
        document_type="rhp",
        document_url="https://www.sebi.gov.in/legacy-rhp.pdf",
        source_confidence=Confidence.MEDIUM,
    )

    assert issue.sebi_company_key is None
    assert document.filing_date is None
    assert document.record_hash is None
