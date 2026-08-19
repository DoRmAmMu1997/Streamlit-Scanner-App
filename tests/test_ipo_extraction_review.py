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
from sqlalchemy.exc import IntegrityError

from backend.ipo.agents import financial_extractor
from backend.ipo.documents import table_extractor
from backend.ipo.documents.table_extractor import ExtractedPage
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
    delete_document,
    get_issue,
    get_latest_manual_profile,
    list_extraction_proposals,
    reject_extraction_proposal,
    submit_extraction_proposal,
)
from backend.observability import EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED
from backend.storage.ipo_repository import (
    insert_ipo_extraction_proposal,
    update_ipo_document_cache_if_source_matches,
)

_NOW = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)


def _cached_document(file_session_factory, data_dir: Path, *, priced: bool = True):
    """Create an issue and document row backed by verified local PDF bytes.

    Beginner note:
        Approval re-verifies the cached bytes exactly like a hand submission,
        so the fixture must write real hash-addressed bytes; metadata alone
        would make the approve path fail its source verification.

        ``priced=False`` models a freshly ingested issue whose price band is
        still unknown, which is the state the IPO-011 cap-price extraction
        exists to resolve.
    """
    issue = create_issue(
        IpoIssueData(
            company_name="Example Ltd",
            issue_type=IpoIssueType.MAINBOARD,
            status=IpoStatus.RHP_FILED,
            source_confidence=Confidence.HIGH,
            price_band_low=Decimal("230") if priced else None,
            price_band_high=Decimal("242") if priced else None,
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
        "financial_amount_unit_page": 10,
        "issue_amount_unit": "crore_inr",
        "issue_amount_unit_page": 13,
        "equity_share_unit": "lakh_shares",
        "equity_share_unit_page": 12,
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


def _verified_parsed_pages() -> tuple[ExtractedPage, ...]:
    """Return truthful bounded page receipts for the review-flow fixtures."""
    lines_by_page: dict[int, tuple[str, ...]] = {
        10: tuple(
            f"{label} FY{year} {value} (in crore INR)"
            for year, period in zip((2023, 2024, 2025), _payload()["periods"], strict=True)
            for label, value in (
                ("Revenue", period["revenue"]),
                ("EBITDA", period["ebitda"]),
                ("PAT", period["pat"]),
                ("Profit before tax", period["profit_before_tax"]),
                ("Finance cost", period["finance_cost"]),
            )
        ),
        11: (
            "Net worth 90 (in crore INR)",
            "Net worth 91 (in crore INR)",
            "Total debt 12 (in crore INR)",
            "Cash 5 (in crore INR)",
            "Cash flow from operations 14 (in crore INR)",
        ),
        12: (
            "Equity shares 50 lakh shares",
            "EPS 2.50",
            "NAV 18.75",
        ),
        13: (
            "Fresh issue 300 (in crore INR)",
            "Offer for sale 0 (in crore INR)",
            "Build a plant and repay borrowings.",
        ),
        14: (
            "Promoter holding before issue 75.25",
            "Promoter holding after issue 56.44",
        ),
        15: (
            "Total assets 150 (in crore INR)",
            "Current liabilities 45 (in crore INR)",
            "Post issue equity shares 60 lakh shares",
        ),
        16: (
            "Peer One Ltd EPS 8.25",
            "Peer One Ltd P/E 21.40",
        ),
        # IPO-011: a cover-page band naming both bounds, as an RHP prints it.
        1: ("Price Band: Rs 230 to Rs 242 per equity share",),
    }
    return tuple(
        ExtractedPage(
            page_number=page_number,
            text="\n".join(lines_by_page.get(page_number, (f"Prospectus page {page_number}",))),
            tables=(),
        )
        for page_number in range(1, 17)
    )


@pytest.fixture(autouse=True)
def _bounded_pdf_parser_fixture(monkeypatch) -> None:
    """Keep review tests at the repository boundary with deterministic parsed pages."""
    monkeypatch.setattr(
        table_extractor,
        "extract_document_pages",
        lambda _path: _verified_parsed_pages(),
    )


def _bound_payload(digest: str, **overrides: Any) -> dict[str, Any]:
    """Attach host-verifiable cited facts to the raw proposal draft."""
    payload = _payload(**overrides)
    facts: list[dict[str, Any]] = []

    def _fact(
        field_name: str,
        value: str,
        page_number: int,
        *,
        unit: str | None = None,
        multiplier: str = "1",
        period_end: str | None = None,
    ) -> None:
        """Append one JSON-safe fact bound to the fixture document."""
        facts.append(
            {
                "field_name": field_name,
                "value": value,
                "unit": unit,
                "unit_multiplier": multiplier,
                "period_end": period_end,
                "document_sha256": digest,
                "page_number": page_number,
                "location": f"text-line:{page_number}",
                "source_token": value,
                "confidence": "high",
                "verification_reasons": [],
            }
        )

    for index, period in enumerate(payload["periods"]):
        for field in ("revenue", "ebitda", "pat", "profit_before_tax", "finance_cost"):
            _fact(
                f"periods[{index}].{field}",
                str(period[field]),
                int(period[f"{field}_page"]),
                unit="crore_inr",
                multiplier="10000000",
                period_end=str(period["period_end"]),
            )
    financial_fields = {
        "net_worth",
        "total_debt",
        "cash",
        "cash_flow_from_operations",
        "total_assets",
        "current_liabilities",
    }
    issue_fields = {"fresh_issue_amount", "ofs_amount"}
    share_fields = {"equity_shares", "post_issue_equity_shares"}
    for field in (
        "net_worth",
        "total_debt",
        "cash",
        "cash_flow_from_operations",
        "equity_shares",
        "eps",
        "nav_book_value",
        "fresh_issue_amount",
        "ofs_amount",
        "promoter_holding_pre_issue",
        "promoter_holding_post_issue",
        "total_assets",
        "current_liabilities",
        "post_issue_equity_shares",
    ):
        unit = None
        multiplier = "1"
        if field in financial_fields or field in issue_fields:
            unit, multiplier = "crore_inr", "10000000"
        elif field in share_fields:
            unit, multiplier = "lakh_shares", "100000"
        _fact(
            field,
            str(payload[field]),
            int(payload[f"{field}_page"]),
            unit=unit,
            multiplier=multiplier,
        )
    for peer in payload["peers"]:
        for metric, value in peer["metrics"].items():
            _fact(
                f"peer {peer['company_name']} {metric}",
                str(value),
                int(peer["source_page"]),
            )
    if payload.get("price_band_high") is not None:
        _fact(
            "price_band_high",
            str(payload["price_band_high"]),
            int(payload["price_band_high_page"]),
        )
    payload["evidence_schema_version"] = "cited-financial-fact/v3"
    payload["cited_financial_facts"] = facts
    payload["cited_text_evidence"] = [
        {
            "field_name": "objects_of_issue",
            "document_sha256": digest,
            "page_number": int(payload["objects_of_issue_page"]),
            "location": f"text-line:{payload['objects_of_issue_page']}",
            "source_text": str(payload["objects_of_issue"]),
            "confidence": "high",
            "verification_reasons": ["Matched the exact normalized source span."],
        }
    ]
    try:
        proposal = financial_extractor._ProposalModel.model_validate(
            {
                # Optional fields (the IPO-011 cap price) may be absent from a
                # fixture payload; only pass through what it actually carries.
                name: payload[name]
                for name in financial_extractor._ProposalModel.model_fields
                if name in payload
            }
        )
    except ValueError:
        # Malformed-payload tests need the public repository validator to own
        # the stable IpoValidationError conversion.
        return payload
    pages = _verified_parsed_pages()
    cited_facts = financial_extractor._cited_financial_facts(
        proposal,
        pages,
        source_content_sha256=digest,
        confidence=Confidence.HIGH,
    )
    cited_text = financial_extractor._cited_text_evidence(
        proposal,
        pages,
        source_content_sha256=digest,
        confidence=Confidence.HIGH,
    )
    payload = financial_extractor._payload_from_model(
        proposal,
        cited_facts,
        cited_text,
    )
    return payload


def _unrelated_parsed_pages() -> tuple[ExtractedPage, ...]:
    """Return bounded pages that contain none of the proposal's claimed spans."""
    return tuple(
        ExtractedPage(
            page_number=page_number,
            text="Unrelated prospectus content.",
            tables=(),
        )
        for page_number in range(1, 17)
    )


def _submit(issue_id: int, document_id: int, digest: str, session_factory, **overrides: Any):
    """Queue one pending proposal with sensible defaults for the scenarios."""
    return submit_extraction_proposal(
        issue_id,
        document_id,
        payload=_bound_payload(digest, **overrides.pop("payload_overrides", {})),
        confidence=overrides.pop("confidence", Confidence.HIGH),
        needs_review_reasons=overrides.pop("needs_review_reasons", ()),
        model_version="ipo-010-extractor-v1",
        agent_model="claude-sonnet-4-6",
        source_content_sha256=digest,
        page_count=16,
        data_dir=overrides.pop("data_dir"),
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
        data_dir=tmp_path,
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
    assert dict(listed[0].payload) == _bound_payload(digest)


def test_submit_rejects_forged_receipts_absent_from_cached_pages(
    file_session_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Self-consistent caller receipts cannot replace host source verification."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    monkeypatch.setattr(
        table_extractor,
        "extract_document_pages",
        lambda _path: _unrelated_parsed_pages(),
    )

    with pytest.raises(IpoValidationError, match=r"cached PDF|source pages|receipt"):
        _submit(
            issue.id,
            document.id,
            digest,
            file_session_factory,
            data_dir=tmp_path,
        )


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
            data_dir=tmp_path,
            payload_overrides={"net_worth": "not-a-number"},
        )

    _submit(issue.id, document.id, digest, file_session_factory, data_dir=tmp_path)
    with pytest.raises(IpoValidationError, match="pending extraction proposal"):
        _submit(issue.id, document.id, digest, file_session_factory, data_dir=tmp_path)

    with pytest.raises(IpoNotFoundError, match="IPO issue 999"):
        submit_extraction_proposal(
            999,
            document.id,
            payload=_bound_payload(digest),
            confidence=Confidence.HIGH,
            needs_review_reasons=(),
            model_version="ipo-010-extractor-v1",
            agent_model="claude-sonnet-4-6",
            source_content_sha256=digest,
            page_count=16,
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


@pytest.mark.parametrize(
    "mutation",
    ["missing", "empty", "duplicate", "wrong_digest", "wrong_page", "wrong_location", "wrong_text"],
)
def test_submit_requires_one_matching_objects_text_fact(
    file_session_factory,
    tmp_path: Path,
    mutation: str,
) -> None:
    """Raw narrative cannot enter the queue without one host-bound source span."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    payload = _bound_payload(digest)
    if mutation == "missing":
        payload.pop("cited_text_evidence")
    elif mutation == "empty":
        payload["cited_text_evidence"] = []
    elif mutation == "duplicate":
        payload["cited_text_evidence"] *= 2
    elif mutation == "wrong_digest":
        payload["cited_text_evidence"][0]["document_sha256"] = "b" * 64
    elif mutation == "wrong_page":
        payload["cited_text_evidence"][0]["page_number"] = 12
    elif mutation == "wrong_location":
        payload["cited_text_evidence"][0]["location"] = "page-wide"
    else:
        payload["cited_text_evidence"][0]["source_text"] = "Invented repayment narrative."

    with pytest.raises(IpoValidationError, match=r"text evidence|Cited text|citation-bound"):
        submit_extraction_proposal(
            issue.id,
            document.id,
            payload=payload,
            confidence=Confidence.HIGH,
            needs_review_reasons=(),
            model_version="ipo-010-extractor-v2",
            agent_model="claude-sonnet-4-6",
            source_content_sha256=digest,
            page_count=16,
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_v1_proposal_is_legacy_and_cannot_be_submitted(
    file_session_factory, tmp_path: Path
) -> None:
    """The former numeric-only schema never inherits narrative authority."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    payload = _bound_payload(digest)
    payload["evidence_schema_version"] = "cited-financial-fact/v1"
    payload.pop("cited_text_evidence")

    with pytest.raises(IpoValidationError, match=r"legacy.*review"):
        submit_extraction_proposal(
            issue.id,
            document.id,
            payload=payload,
            confidence=Confidence.HIGH,
            needs_review_reasons=(),
            model_version="ipo-010-extractor-v2",
            agent_model="claude-sonnet-4-6",
            source_content_sha256=digest,
            page_count=16,
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_approval_writes_the_verified_cap_price_onto_the_issue(
    file_session_factory, tmp_path: Path
) -> None:
    """The cap price reaches the issue only through a verified approval.

    Beginner note:
        Price band lives on the issue row, not the manual revision, but it is
        evidence like any other: the model cites it, the host re-resolves that
        citation against the cached PDF, and only approval writes it. Without
        it the valuation factor is missing, and valuation is critical -- so
        this is the step that lets an issue reach a real verdict at all.
    """
    issue, document, digest = _cached_document(
        file_session_factory, tmp_path, priced=False
    )
    assert issue.price_band_high is None

    proposal = _submit(
        issue.id,
        document.id,
        digest,
        file_session_factory,
        data_dir=tmp_path,
        payload_overrides={"price_band_high": "242", "price_band_high_page": 1},
    )
    assert proposal.payload["price_band_high"] == "242"

    approve_extraction_proposal(
        proposal.id,
        reviewed_by_email="reviewer@example.com",
        data_dir=tmp_path,
        now=lambda: _NOW,
        session_factory=file_session_factory,
    )

    priced = get_issue(issue.id, session_factory=file_session_factory)
    assert priced is not None
    assert priced.price_band_high == Decimal("242")


def test_unpriced_drhp_proposal_still_approves_and_leaves_the_issue_unpriced(
    file_session_factory, tmp_path: Path
) -> None:
    """A DRHP is filed before pricing, so the cap price must stay optional."""
    issue, document, digest = _cached_document(
        file_session_factory, tmp_path, priced=False
    )

    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )
    approve_extraction_proposal(
        proposal.id,
        reviewed_by_email="reviewer@example.com",
        data_dir=tmp_path,
        now=lambda: _NOW,
        session_factory=file_session_factory,
    )

    unpriced = get_issue(issue.id, session_factory=file_session_factory)
    assert unpriced is not None
    assert unpriced.price_band_high is None


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
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )
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
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )

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
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )

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


def test_submit_rejects_a_source_sha_that_is_not_current(
    file_session_factory, tmp_path: Path
) -> None:
    """A proposal cannot claim bytes different from the current document row."""
    issue, document, _digest = _cached_document(file_session_factory, tmp_path)

    with pytest.raises(IpoValidationError, match="source SHA"):
        _submit(
            issue.id,
            document.id,
            "b" * 64,
            file_session_factory,
            data_dir=tmp_path,
        )


def test_approval_refuses_a_stale_document_and_cached_bytes(
    file_session_factory, tmp_path: Path
) -> None:
    """Refreshing a document after extraction makes its proposal non-approvable."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )
    replacement = b"%PDF-1.7\nreplacement prospectus\n%%EOF"
    replacement_digest = hashlib.sha256(replacement).hexdigest()
    replacement_path = tmp_path / "ipo" / "documents" / f"{replacement_digest}.pdf"
    replacement_path.write_bytes(replacement)
    with file_session_factory() as session:
        assert update_ipo_document_cache_if_source_matches(
            session,
            issue.id,
            document.id,
            expected_document_url=document.document_url,
            expected_document_type=document.document_type,
            values={
                "content_sha256": replacement_digest,
                "downloaded_at": _NOW,
                "file_path": f"ipo/documents/{replacement_digest}.pdf",
                "page_count": None,
                "parse_status": IpoDocumentParseStatus.PENDING.value,
            },
        )

    with pytest.raises(IpoValidationError, match="stale"):
        approve_extraction_proposal(
            proposal.id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )

    assert get_latest_manual_profile(
        issue.id, session_factory=file_session_factory
    ) is None


def test_legacy_unbound_proposal_is_review_required_not_approvable(
    file_session_factory, tmp_path: Path
) -> None:
    """Historical pending rows without cited facts never inherit new trust."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    with file_session_factory() as session:
        legacy = insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "payload_json": _payload(),
                "confidence": "high",
                "needs_review_reasons_json": [],
                "model_version": "ipo-010-extractor-v1",
                "agent_model": "claude-sonnet-4-6",
                "source_content_sha256": digest,
                "page_count": 16,
            },
        )
        proposal_id = legacy.id

    with pytest.raises(IpoValidationError, match=r"legacy.*review"):
        approve_extraction_proposal(
            proposal_id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )

    assert get_latest_manual_profile(
        issue.id, session_factory=file_session_factory
    ) is None


def test_v1_pending_history_is_review_required_not_approvable(
    file_session_factory, tmp_path: Path
) -> None:
    """A stored numeric-only v1 row remains visible but cannot become evidence."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    payload = _bound_payload(digest)
    payload["evidence_schema_version"] = "cited-financial-fact/v1"
    payload.pop("cited_text_evidence")
    with file_session_factory() as session:
        legacy = insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "payload_json": payload,
                "evidence_schema_version": "cited-financial-fact/v1",
                "confidence": "high",
                "needs_review_reasons_json": [],
                "model_version": "ipo-010-extractor-v2",
                "agent_model": "claude-sonnet-4-6",
                "source_content_sha256": digest,
                "page_count": 16,
            },
        )
        proposal_id = legacy.id

    with pytest.raises(IpoValidationError, match=r"legacy.*review"):
        approve_extraction_proposal(
            proposal_id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_approval_revalidates_objects_text_evidence(
    file_session_factory, tmp_path: Path
) -> None:
    """A malformed stored v2 text fact cannot bypass the approval boundary."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    payload = _bound_payload(digest)
    payload["cited_text_evidence"][0]["source_text"] = "Invented repayment narrative."
    with file_session_factory() as session:
        malformed = insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "payload_json": payload,
                "evidence_schema_version": "cited-financial-fact/v2",
                "confidence": "high",
                "needs_review_reasons_json": [],
                "model_version": "ipo-010-extractor-v2",
                "agent_model": "claude-sonnet-4-6",
                "source_content_sha256": digest,
                "page_count": 16,
            },
        )
        proposal_id = malformed.id

    with pytest.raises(IpoValidationError, match=r"text evidence does not match"):
        approve_extraction_proposal(
            proposal_id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_approval_rejects_forged_receipts_absent_from_cached_pages(
    file_session_factory,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """A stored self-consistent receipt is re-resolved before approval."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    payload = _bound_payload(digest)
    with file_session_factory() as session:
        forged = insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "payload_json": payload,
                "evidence_schema_version": "cited-financial-fact/v2",
                "confidence": "high",
                "needs_review_reasons_json": [],
                "model_version": "ipo-010-extractor-v2",
                "agent_model": "claude-sonnet-4-6",
                "source_content_sha256": digest,
                "page_count": 16,
            },
        )
        proposal_id = forged.id
    monkeypatch.setattr(
        table_extractor,
        "extract_document_pages",
        lambda _path: _unrelated_parsed_pages(),
    )

    with pytest.raises(IpoValidationError, match=r"cached PDF|source pages|receipt"):
        approve_extraction_proposal(
            proposal_id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )


def test_lost_approval_race_rolls_back_the_manual_revision(
    file_session_factory, tmp_path: Path, monkeypatch
) -> None:
    """Proposal CAS and all manual child rows share one transaction."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )
    monkeypatch.setattr(
        "backend.ipo.repository.mark_ipo_extraction_proposal_reviewed",
        lambda *_args, **_kwargs: None,
    )

    with pytest.raises(IpoValidationError, match="reviewed concurrently"):
        approve_extraction_proposal(
            proposal.id,
            reviewed_by_email="reviewer@example.com",
            data_dir=tmp_path,
            session_factory=file_session_factory,
        )

    assert get_latest_manual_profile(
        issue.id, session_factory=file_session_factory
    ) is None


def test_pending_proposal_blocks_document_deletion_but_reviewed_history_survives(
    file_session_factory, tmp_path: Path
) -> None:
    """Retention keeps reviewed provenance while pending work fails closed."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )

    with pytest.raises(IpoValidationError, match="pending extraction proposal"):
        delete_document(
            issue.id, document.id, session_factory=file_session_factory
        )

    reject_extraction_proposal(
        proposal.id,
        reviewed_by_email="reviewer@example.com",
        reason="Reviewer rejected the draft.",
        session_factory=file_session_factory,
    )
    assert delete_document(
        issue.id, document.id, session_factory=file_session_factory
    )
    retained = list_extraction_proposals(
        issue_id=issue.id, session_factory=file_session_factory
    )[0]
    assert retained.document_id is None
    assert retained.document_url == document.document_url
    assert retained.source_content_sha256 == digest


def test_reviewed_semantic_duplicate_is_skipped_but_changed_payload_is_allowed(
    file_session_factory, tmp_path: Path
) -> None:
    """Payload identity prevents repeats without freezing future corrections."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    first = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )
    reject_extraction_proposal(
        first.id,
        reviewed_by_email="reviewer@example.com",
        reason="Try extraction again.",
        session_factory=file_session_factory,
    )

    with pytest.raises(IpoValidationError, match="identical proposal"):
        _submit(
            issue.id,
            document.id,
            digest,
            file_session_factory,
            data_dir=tmp_path,
        )

    changed = _submit(
        issue.id,
        document.id,
        digest,
        file_session_factory,
        data_dir=tmp_path,
        payload_overrides={"net_worth": "91"},
    )
    assert changed.status is IpoExtractionProposalStatus.PENDING
    assert changed.semantic_fingerprint != first.semantic_fingerprint


def test_database_enforces_one_pending_proposal_per_document(
    file_session_factory, tmp_path: Path
) -> None:
    """The partial unique index closes concurrent read-before-write races."""
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path
    )

    with pytest.raises(IntegrityError), file_session_factory() as session:
        insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "payload_json": _bound_payload(digest, net_worth="91"),
                "evidence_schema_version": "cited-financial-fact/v1",
                "semantic_fingerprint": "c" * 64,
                "confidence": "high",
                "needs_review_reasons_json": [],
                "model_version": "ipo-010-extractor-v2",
                "agent_model": "claude-sonnet-4-6",
                "source_content_sha256": digest,
                "page_count": 16,
            },
        )
