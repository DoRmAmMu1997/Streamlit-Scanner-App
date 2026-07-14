"""IPO-010 extraction-proposal review flow tests.

Beginner note:
The review queue is the trust boundary between AI output and scoring
evidence. These tests pin the fail-closed promises: a proposal can only be
stored if it could be approved, approval replays the exact manual-extraction
validation (including re-verifying the cached PDF bytes), rejection keeps an
attributable audit record, and no path marks AI output as trusted without a
named reviewer.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.ipo.models import (
    Confidence,
    IpoDocumentData,
    IpoDocumentParseStatus,
    IpoExtractionProposalStatus,
    IpoIssueData,
    IpoIssueType,
    IpoStatus,
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    approve_extraction_proposal,
    create_document,
    create_issue,
    get_latest_manual_profile,
    list_extraction_proposals,
    reject_extraction_proposal,
    submit_extraction_proposal,
)
from backend.observability import EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED
from backend.storage.ipo_repository import update_ipo_document_cache_if_source_matches

_NOW = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)


def _cached_document(file_session_factory, data_dir: Path):
    """Create an issue and document row backed by verified local PDF bytes.

    Beginner note:
        Approval re-verifies the cached bytes exactly like a hand submission,
        so the fixture must write real hash-addressed bytes; metadata alone
        would make the approve path fail its source verification.
    """
    issue = create_issue(
        IpoIssueData(
            company_name="Example Ltd",
            issue_type=IpoIssueType.MAINBOARD,
            status=IpoStatus.RHP_FILED,
            source_confidence=Confidence.HIGH,
            price_band_low=Decimal("230"),
            price_band_high=Decimal("242"),
        ),
        session_factory=file_session_factory,
    )
    document = create_document(
        issue.id,
        IpoDocumentData(
            document_type="rhp",
            document_url="https://www.sebi.gov.in/filings/example-rhp.html",
            source_url="https://www.sebi.gov.in/filings/public-issues",
            source_confidence=Confidence.HIGH,
            record_hash="a" * 64,
        ),
        session_factory=file_session_factory,
    )
    pdf_bytes = b"%PDF-1.7\nextraction proposal fixture\n%%EOF"
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    absolute_path = data_dir / "ipo" / "documents" / f"{digest}.pdf"
    absolute_path.parent.mkdir(parents=True)
    absolute_path.write_bytes(pdf_bytes)
    with file_session_factory() as session:
        assert update_ipo_document_cache_if_source_matches(
            session,
            issue.id,
            document.id,
            expected_document_url=document.document_url,
            expected_document_type=document.document_type,
            values={
                "content_sha256": digest,
                "downloaded_at": dt.datetime(2026, 7, 1, 8, tzinfo=dt.UTC),
                "file_path": f"ipo/documents/{digest}.pdf",
                "page_count": None,
                "parse_status": IpoDocumentParseStatus.PENDING.value,
            },
        )
    return issue, document, digest


def _period_payload(year: int) -> dict[str, Any]:
    """Build one payload period row with constant sourced pages."""
    return {
        "period_end": f"{year}-03-31",
        "revenue": str(100 + year - 2023),
        "revenue_page": 10,
        "ebitda": "20",
        "ebitda_page": 10,
        "pat": "10",
        "pat_page": 10,
        "profit_before_tax": "12",
        "profit_before_tax_page": 10,
        "finance_cost": "2",
        "finance_cost_page": 10,
    }


def _payload(**overrides: Any) -> dict[str, Any]:
    """Build one complete, approvable proposal payload."""
    values: dict[str, Any] = {
        "financial_amount_unit": "crore_inr",
        "issue_amount_unit": "crore_inr",
        "equity_share_unit": "lakh_shares",
        "periods": [_period_payload(year) for year in (2023, 2024, 2025)],
        "net_worth": "90",
        "net_worth_page": 11,
        "total_debt": "12",
        "total_debt_page": 11,
        "cash": "5",
        "cash_page": 11,
        "cash_flow_from_operations": "14",
        "cash_flow_from_operations_page": 11,
        "equity_shares": "50",
        "equity_shares_page": 12,
        "eps": "2.50",
        "eps_page": 12,
        "nav_book_value": "18.75",
        "nav_book_value_page": 12,
        "objects_of_issue": "Build a plant and repay borrowings.",
        "objects_of_issue_page": 13,
        "fresh_issue_amount": "300",
        "fresh_issue_amount_page": 13,
        "ofs_amount": "0",
        "ofs_amount_page": 13,
        "promoter_holding_pre_issue": "75.25",
        "promoter_holding_pre_issue_page": 14,
        "promoter_holding_post_issue": "56.44",
        "promoter_holding_post_issue_page": 14,
        "total_assets": "150",
        "total_assets_page": 15,
        "current_liabilities": "45",
        "current_liabilities_page": 15,
        "post_issue_equity_shares": "60",
        "post_issue_equity_shares_page": 15,
        "peers": [
            {
                "company_name": "Peer One Ltd",
                "source_page": 16,
                "metrics": {"eps": "8.25", "pe": "21.40"},
            }
        ],
    }
    values.update(overrides)
    return values


def _submit(issue_id: int, document_id: int, digest: str, session_factory, **overrides: Any):
    """Queue one pending proposal with sensible defaults for the scenarios."""
    return submit_extraction_proposal(
        issue_id,
        document_id,
        payload=_payload(**overrides.pop("payload_overrides", {})),
        confidence=overrides.pop("confidence", Confidence.HIGH),
        needs_review_reasons=overrides.pop("needs_review_reasons", ()),
        model_version="ipo-010-extractor-v1",
        agent_model="claude-sonnet-4-6",
        source_content_sha256=digest,
        page_count=16,
        session_factory=session_factory,
    )


def test_submit_persists_a_pending_proposal_round_trip(
    file_session_factory, tmp_path: Path
) -> None:
    """The queue stores the payload, provenance, and verifier notes losslessly."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)

    proposal = _submit(
        issue.id,
        document.id,
        digest,
        file_session_factory,
        confidence=Confidence.MEDIUM,
        needs_review_reasons=("Could not independently verify eps (page 12).",),
    )

    assert proposal.status is IpoExtractionProposalStatus.PENDING
    assert proposal.company_name == "Example Ltd"
    assert proposal.confidence is Confidence.MEDIUM
    assert proposal.needs_review_reasons == (
        "Could not independently verify eps (page 12).",
    )
    assert proposal.source_content_sha256 == digest
    assert proposal.manual_extraction_id is None

    listed = list_extraction_proposals(
        issue_id=issue.id,
        status=IpoExtractionProposalStatus.PENDING,
        session_factory=file_session_factory,
    )
    assert [row.id for row in listed] == [proposal.id]
    assert dict(listed[0].payload) == _payload()


def test_submit_rejects_malformed_payload_and_duplicates(
    file_session_factory, tmp_path: Path
) -> None:
    """Unstorable proposals are refused before anything reaches the queue."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)

    with pytest.raises(IpoValidationError, match="malformed"):
        _submit(
            issue.id,
            document.id,
            digest,
            file_session_factory,
            payload_overrides={"net_worth": "not-a-number"},
        )

    _submit(issue.id, document.id, digest, file_session_factory)
    with pytest.raises(IpoValidationError, match="pending extraction proposal"):
        _submit(issue.id, document.id, digest, file_session_factory)

    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        submit_extraction_proposal(
            999,
            document.id,
            payload=_payload(),
            confidence=Confidence.HIGH,
            needs_review_reasons=(),
            model_version="ipo-010-extractor-v1",
            agent_model="claude-sonnet-4-6",
            source_content_sha256=digest,
            page_count=16,
            session_factory=file_session_factory,
        )


def test_approve_converts_the_proposal_into_a_manual_revision(
    file_session_factory, tmp_path: Path
) -> None:
    """Approval produces the same immutable record a hand submission produces.

    Beginner note:
        The reviewer becomes ``entered_by_email`` (an attestation), the cached
        PDF bytes are re-verified, and the ratio engine can run on the result
        exactly as it does for typed-in evidence — scoring never knows an
        agent drafted the numbers.
    """
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, file_session_factory)
    audit_events: list[dict[str, Any]] = []

    def _record_audit(**kwargs: Any) -> bool:
        """Capture audit payloads like the real best-effort sink."""
        audit_events.append(kwargs)
        return True

    revision = approve_extraction_proposal(
        proposal.id,
        reviewed_by_email="Reviewer@Example.com",
        data_dir=tmp_path,
        now=lambda: _NOW,
        audit_recorder=_record_audit,
        session_factory=file_session_factory,
    )

    assert revision.entered_by_email == "reviewer@example.com"
    assert revision.source_content_sha256 == digest
    assert revision.net_worth == Decimal("90")
    assert revision.periods[-1].period_end == dt.date(2025, 3, 31)

    profile = get_latest_manual_profile(issue.id, session_factory=file_session_factory)
    assert profile == revision

    reviewed = list_extraction_proposals(
        issue_id=issue.id, session_factory=file_session_factory
    )[0]
    assert reviewed.status is IpoExtractionProposalStatus.APPROVED
    assert reviewed.reviewed_by_email == "reviewer@example.com"
    assert reviewed.manual_extraction_id == revision.id
    assert any(
        event["event"] == EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED
        and event["metadata"]["decision"] == "approved"
        for event in audit_events
    )


def test_approve_requires_a_pending_proposal(
    file_session_factory, tmp_path: Path
) -> None:
    """Missing and already-reviewed proposals both fail loudly."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, file_session_factory)

    with pytest.raises(IpoNotFoundError, match="proposal 999"):
        approve_extraction_proposal(
            999,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )

    approve_extraction_proposal(
        proposal.id,
        reviewed_by_email="reviewer@example.com",
        data_dir=tmp_path,
        now=lambda: _NOW,
        session_factory=file_session_factory,
    )
    with pytest.raises(IpoValidationError, match="already approved"):
        approve_extraction_proposal(
            proposal.id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_reject_keeps_an_attributable_record(
    file_session_factory, tmp_path: Path
) -> None:
    """Rejection stores the reviewer, instant, and a required reason."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, file_session_factory)

    with pytest.raises(IpoValidationError, match="non-empty reason"):
        reject_extraction_proposal(
            proposal.id,
            reviewed_by_email="reviewer@example.com",
            reason="   ",
            session_factory=file_session_factory,
        )

    rejected = reject_extraction_proposal(
        proposal.id,
        reviewed_by_email="reviewer@example.com",
        reason="Totals do not match the cited pages.",
        now=lambda: _NOW,
        session_factory=file_session_factory,
    )

    assert rejected.status is IpoExtractionProposalStatus.REJECTED
    assert rejected.review_note == "Totals do not match the cited pages."
    assert rejected.reviewed_at == _NOW
    assert rejected.manual_extraction_id is None

    with pytest.raises(IpoValidationError, match="not pending"):
        reject_extraction_proposal(
            proposal.id,
            reviewed_by_email="reviewer@example.com",
            reason="Double review.",
            session_factory=file_session_factory,
        )

    # A rejected proposal never becomes evidence.
    assert (
        get_latest_manual_profile(issue.id, session_factory=file_session_factory)
        is None
    )
