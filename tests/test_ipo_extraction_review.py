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
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import pytest
from sqlalchemy.exc import IntegrityError

from backend.ipo.agents import financial_extractor
from backend.ipo.documents import table_extractor
from backend.ipo.documents.table_extractor import ExtractedPage
from backend.ipo.manual_extraction import IpoAmountUnit
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
    load_manual_edit_context,
    reject_extraction_proposal,
    submit_extraction_proposal,
)
from backend.observability import EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED
from backend.storage import SessionFactory
from backend.storage.ipo_repository import (
    insert_ipo_extraction_proposal,
    update_ipo_document_cache_if_source_matches,
)

_NOW = dt.datetime(2026, 7, 13, 10, 0, tzinfo=dt.UTC)


@pytest.fixture(params=("file_session_factory", "postgres_session_factory"), ids=("sqlite", "postgres"))
def approval_session_factory(request: pytest.FixtureRequest) -> SessionFactory:
    """Run authority interleavings against both supported transaction backends.

    Args:
        request: Pytest request selecting an existing disposable storage fixture.

    Returns:
        A caller-owned transaction factory for the selected isolated database.

    Beginner note:
        Reusing the same assertions prevents the PostgreSQL test from proving a
        weaker contract than SQLite. The PostgreSQL fixture uses an explicitly
        configured test server and fails in required CI if that server is absent;
        it never falls back to the application's database URL.
    """
    return cast(SessionFactory, request.getfixturevalue(request.param))


def test_human_save_during_verification_keeps_its_revision(
    approval_session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A human commit during PDF verification must defeat the older proposal.

    Args:
        approval_session_factory: Isolated SQLite or PostgreSQL transactions.
        tmp_path: Private content-addressed PDF cache used only by this test.
        monkeypatch: Installs the deterministic scheduling hook and restores it.

    Beginner note:
        Verification happens outside the approval transaction. A real independent
        human save is inserted into that gap on both databases. Approval must
        reload the manual token under its final lock instead of using the token
        it saw before verification; the human history and pending proposal survive.
    """
    from backend.ipo import repository

    session_factory = approval_session_factory
    issue, document, digest = _cached_document(session_factory, tmp_path)
    context = repository.load_manual_edit_context(issue.id, document.id, session_factory=session_factory)
    proposal = _submit(issue.id, document.id, digest, session_factory, data_dir=tmp_path)
    verify = repository._verify_cited_receipts_from_cached_pdf
    saved = []

    def save_after_verification(*args: Any, **kwargs: Any) -> None:
        """Commit the human correction after genuine deterministic citation checks.

        Args:
            *args: Positional inputs forwarded unchanged to the verifier.
            **kwargs: Keyword inputs forwarded unchanged to the verifier.

        Beginner note:
            Only the scheduling point is intercepted. Both the verification and
            human transaction use production code, so no fake revision counter
            can conceal an approval that accepts stale state.
        """
        verify(*args, **kwargs)
        saved.append(repository.submit_manual_extraction(
            issue.id, repository._proposal_payload_to_manual_data(proposal.payload, document.id),
            entered_by_email="human@example.com", expected_context=context,
            data_dir=tmp_path, session_factory=session_factory,
        ))

    monkeypatch.setattr(repository, "_verify_cited_receipts_from_cached_pdf", save_after_verification)
    with pytest.raises(repository.IpoProposalConflictError) as rejected:
        repository.approve_extraction_proposal(
            proposal.id, reviewed_by_email="reviewer@example.com", data_dir=tmp_path,
            session_factory=session_factory,
        )
    assert rejected.value.code == "manual_revision_changed"
    assert repository.list_manual_extractions(issue.id, session_factory=session_factory) == saved
    assert repository.list_extraction_proposals(issue_id=issue.id, session_factory=session_factory) == [proposal]
    assert repository.load_manual_edit_context(
        issue.id, document.id, session_factory=session_factory,
    ).manual_revision == 1


@pytest.mark.parametrize("change", ("filing_date", "cache_sha", "insert"))
def test_postgres_source_writers_cannot_escape_approval_row_lock(
    postgres_session_factory: SessionFactory, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch, change: str,
) -> None:
    """Require PostgreSQL to refuse competing source writes while approval holds its lock.

    Args:
        postgres_session_factory: Real transactions on a disposable migrated database.
        tmp_path: Private PDF cache containing the proposal's source bytes.
        monkeypatch: Pauses the approval after its real lock has been acquired.
        change: Source mutation path attempted by the competing transaction.

    Beginner note:
        SQLite serializes all writers, which can hide a missing per-issue lock.
        Here an actual approval holds its PostgreSQL row lock while a separate
        transaction attempts each storage write path. PostgreSQL's lock-timeout
        SQLSTATE proves blocking; there is no sleep or elapsed-time assertion.
        The failed source transaction rolls back before approval is released.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    from sqlalchemy import text
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import Session

    from backend.ipo import repository
    from backend.storage import ipo_repository as storage

    issue, document, digest = _cached_document(postgres_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, postgres_session_factory, data_dir=tmp_path)
    held, release = Event(), Event()
    lock = repository.lock_ipo_scoring_state

    def hold_approval(
        session: Session, issue_id: int, *, expected_revision: int | None = None,
    ) -> bool:
        """Pause only after the approval has acquired its real issue-state lock.

        Args:
            session: Approval's real caller-owned transaction.
            issue_id: Issue whose serialization row must be held.
            expected_revision: Optional revision precondition forwarded unchanged.

        Returns:
            The original lock operation's success result after the writer probe.

        Raises:
            AssertionError: The test fails to release the worker within its bound.

        Beginner note:
            The lock is never simulated. The event keeps the real transaction
            open until the other connection has proved it cannot change the source.
        """
        acquired = lock(session, issue_id, expected_revision=expected_revision)
        held.set()
        assert release.wait(30), "Writer probe did not release the approval"
        return acquired

    monkeypatch.setattr(repository, "lock_ipo_scoring_state", hold_approval)
    with ThreadPoolExecutor(max_workers=1) as pool:
        approval = pool.submit(
            repository.approve_extraction_proposal, proposal.id,
            reviewed_by_email="reviewer@example.com", data_dir=tmp_path,
            session_factory=postgres_session_factory,
        )
        try:
            assert held.wait(15), "Approval did not acquire its row lock"
            with pytest.raises(OperationalError) as blocked, postgres_session_factory() as writer:
                # This test-only setting bounds the server wait. If the storage
                # helper omits the shared lock, its unrelated document-row write
                # succeeds and the expected database error is absent.
                writer.execute(text("SET LOCAL lock_timeout = '250ms'"))
                if change == "filing_date":
                    storage.update_ipo_document_row(
                        writer, issue.id, document.id, {"filing_date": dt.date(2026, 10, 1)},
                    )
                elif change == "cache_sha":
                    storage.update_ipo_document_cache_if_source_matches(
                        writer, issue.id, document.id,
                        expected_document_url=document.document_url,
                        expected_document_type=document.document_type,
                        values={"content_sha256": "b" * 64},
                    )
                else:
                    storage.insert_ipo_document(writer, issue.id, {
                        "document_type": "rhp", "document_url": "https://www.sebi.gov.in/new.pdf",
                        "source_confidence": "high", "parse_status": "not_downloaded",
                    })
            assert getattr(blocked.value.orig, "sqlstate", None) == "55P03"
        finally:
            # Assertion failures must release the real transaction too, otherwise
            # executor shutdown would obscure the original test failure.
            release.set()
        assert approval.result(timeout=15).source_content_sha256 == digest
    assert len(repository.list_manual_extractions(issue.id, session_factory=postgres_session_factory)) == 1


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
            filing_date=dt.date(2025, 1, 1),
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
        baseline_context=overrides.pop("baseline_context", load_manual_edit_context(
            issue_id, document_id, session_factory=session_factory)),
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


def test_v2_payload_without_cap_fields_approves_with_trusted_baseline(
    file_session_factory, tmp_path: Path
) -> None:
    """Adding an optional field must not invalidate queued reviews.

    Beginner note:
        A proposal drafted before IPO-011 has no ``price_band_high`` keys in
        its payload at all -- not keys set to null, no keys. The receipt
        verifier rebuilds the model from the payload, so subscripting every
        known field unconditionally raised ``KeyError`` on exactly those rows.
        That was swallowed by a broad except and reported as "receipts do not
        match the verified cached PDF", which reads like tampering and would
        have quietly made every in-flight v2 review un-approvable. This fixture
        explicitly captures current authority before constructing the old payload
        shape; the separate unknown-baseline regression covers legacy rows whose
        authority was never captured.
    """
    issue, document, digest = _cached_document(
        file_session_factory, tmp_path, priced=False
    )
    payload = _bound_payload(digest)
    # Reproduce a real pre-IPO-011 payload: the optional keys never existed.
    payload.pop("price_band_high", None)
    payload.pop("price_band_high_page", None)
    payload["evidence_schema_version"] = "cited-financial-fact/v2"
    assert "price_band_high" not in payload
    context = load_manual_edit_context(issue.id, document.id, session_factory=file_session_factory)

    with file_session_factory() as session:
        legacy = insert_ipo_extraction_proposal(
            session,
            issue.id,
            document.id,
            {
                "status": "pending",
                "document_url_snapshot": document.document_url,
                "baseline_manual_revision": context.manual_revision,
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
        proposal_id = legacy.id

    revision = approve_extraction_proposal(
        proposal_id,
        reviewed_by_email="reviewer@example.com",
        data_dir=tmp_path,
        now=lambda: _NOW,
        session_factory=file_session_factory,
    )

    assert revision is not None
    # A v2 proposal carries no issue terms, so the issue stays unpriced.
    unchanged = get_issue(issue.id, session_factory=file_session_factory)
    assert unchanged is not None
    assert unchanged.price_band_high is None


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

    with pytest.raises(IpoValidationError, match=r"source SHA|baseline is bound to a different issue or source"):
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

    from sqlalchemy import func, select

    from backend.storage.ipo_repository import get_ipo_manual_revision, get_ipo_scoring_state_values
    from backend.storage.models import IpoManualExtraction, IpoManualFinancialPeriod, IpoManualPeerValuation

    before_issue = get_issue(issue.id, session_factory=file_session_factory)

    with file_session_factory() as session:
        state_before = get_ipo_scoring_state_values(session, issue.id)
        manual_before = get_ipo_manual_revision(session, issue.id)
        counts = tuple(session.scalar(select(func.count()).select_from(model)) for model in
                       (IpoManualExtraction, IpoManualFinancialPeriod, IpoManualPeerValuation))

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

    with file_session_factory() as session:
        assert get_ipo_scoring_state_values(session, issue.id) == state_before
        assert get_ipo_manual_revision(session, issue.id) == manual_before
        assert tuple(session.scalar(select(func.count()).select_from(model)) for model in
                     (IpoManualExtraction, IpoManualFinancialPeriod, IpoManualPeerValuation)) == counts
    assert get_issue(issue.id, session_factory=file_session_factory) == before_issue


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


@pytest.mark.parametrize("unknown", [True, False])
def test_unknown_or_stale_baseline_cannot_overwrite_manual_evidence(
    approval_session_factory: SessionFactory, tmp_path: Path, unknown: bool,
):
    """HIGH source matching never authorizes replacement of newer human evidence.

    Args:
        approval_session_factory: Isolated transactions on either supported backend.
        tmp_path: Private cache containing the verified source bytes.
        unknown: Whether the captured proposal lacks a trusted baseline entirely.

    Beginner note:
        Both databases must reject an unknown or consumed manual authority token,
        even when the newer human record has a backdated timestamp. Chronological
        sorting cannot substitute for the separate manual revision counter.
    """
    from backend.ipo import repository

    session_factory = approval_session_factory
    issue, document, digest = _cached_document(session_factory, tmp_path)
    context = repository.load_manual_edit_context(issue.id, document.id, session_factory=session_factory)
    proposal = _submit(issue.id, document.id, digest, session_factory,
                       data_dir=tmp_path, baseline_context=None if unknown else context)
    repository.submit_manual_extraction(
        issue.id, repository._proposal_payload_to_manual_data(proposal.payload, document.id),
        entered_by_email="human@example.com", expected_context=context, data_dir=tmp_path,
        now=lambda: dt.datetime(2000, 1, 1, tzinfo=dt.UTC), session_factory=session_factory,
    )
    with pytest.raises(repository.IpoProposalConflictError) as error:
        approve_extraction_proposal(proposal.id, reviewed_by_email="reviewer@example.com",
                                    accept_authority_change=True,
                                    data_dir=tmp_path, session_factory=session_factory)
    assert error.value.code == ("unknown_manual_baseline" if unknown else "manual_revision_changed")
    assert len(repository.list_manual_extractions(issue.id, session_factory=session_factory)) == 1


@pytest.mark.parametrize("change", ["newer_source", "conflicting_terms"])
def test_automatic_approval_rechecks_authority_before_any_evidence_write(
    file_session_factory, tmp_path: Path, change: str,
) -> None:
    """Reject authority changes that occur after a proposal becomes ready.

    Beginner note:
        A queued proposal can still match its PDF and manual baseline after a
        newer RHP arrives or someone corrects issue terms. Automatic approval
        must re-read those independent authorities under its transaction lock;
        rejection must preserve the pending proposal, terms and empty history.
    """
    from backend.ipo import repository

    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    repository.update_document(issue.id, document.id, IpoDocumentData(
        document_type="rhp", document_url=document.document_url,
        filing_date=dt.date(2025, 1, 1), source_confidence=Confidence.HIGH,
    ), session_factory=file_session_factory)
    proposal = _submit(
        issue.id, document.id, digest, file_session_factory, data_dir=tmp_path,
        payload_overrides={"price_band_high": "242", "price_band_high_page": 1},
    )
    if change == "newer_source":
        create_document(issue.id, IpoDocumentData(
            document_type="rhp", document_url="https://www.sebi.gov.in/filings/newer-rhp.html",
            filing_date=dt.date(2025, 2, 1), source_confidence=Confidence.HIGH,
        ), session_factory=file_session_factory)
        expected_code = "noncurrent_source"
    else:
        repository.update_issue(issue.id, IpoIssueData(
            company_name=issue.company_name, issue_type=issue.issue_type, status=issue.status,
            price_band_low=Decimal("230"), price_band_high=Decimal("244"),
            source_confidence=Confidence.HIGH,
        ), session_factory=file_session_factory)
        expected_code = "conflicting_issue_terms"
    before = get_issue(issue.id, session_factory=file_session_factory)
    with pytest.raises(repository.IpoProposalConflictError) as error:
        approve_extraction_proposal(
            proposal.id, reviewed_by_email="ipo-automation@screener.local",
            require_current_candidate=True, accept_authority_change=True,
            data_dir=tmp_path, session_factory=file_session_factory,
        )
    assert error.value.code == expected_code
    assert get_issue(issue.id, session_factory=file_session_factory) == before
    assert repository.list_manual_extractions(issue.id, session_factory=file_session_factory) == []
    assert list_extraction_proposals(issue_id=issue.id, session_factory=file_session_factory) == [proposal]


@pytest.mark.parametrize("mismatch", [None, "value", "unit", "source", "page", "parse"])
def test_copied_manual_values_are_verified_against_the_selected_pdf(
    file_session_factory, tmp_path: Path, monkeypatch, mismatch: str | None,
) -> None:
    """Rebind copied values to real new-source spans before creating a revision.

    Beginner note:
        Previously a source switch could relabel old values and page numbers.
        A copy now succeeds only when every amount, unit and narrative matches
        the newly selected PDF. One non-core mismatch must fail too, even though
        the AI review path might otherwise classify that partial match as MEDIUM.
    """
    from backend.ipo import repository

    assert callable(getattr(financial_extractor, "verify_manual_copy_citations", None))
    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    data = repository._proposal_payload_to_manual_data(_bound_payload(digest), document.id)
    if mismatch == "value":
        data = replace(data, promoter_holding_pre_issue=Decimal("77"))
    elif mismatch == "unit":
        data = replace(data, financial_amount_unit=IpoAmountUnit.LAKH_INR)
    elif mismatch == "page":
        data = replace(data, objects_of_issue_page=99)
    pages = _unrelated_parsed_pages() if mismatch == "source" else _verified_parsed_pages()
    opened: list[Path] = []

    def parse_selected(path: Path) -> tuple[ExtractedPage, ...]:
        """Return fixture pages while recording the exact selected cache path.

        Args:
            path: Selected verified PDF passed to the worker boundary.

        Returns:
            Fixture pages containing complete or intentionally mismatched facts.

        Raises:
            IpoDocumentParseError: In the injected worker-failure scenario.

        Beginner note:
            Production uses the bounded worker; this seam keeps the regression
            offline while proving the repository selects the new source file.
        """
        opened.append(path)
        if mismatch == "parse":
            raise table_extractor.IpoDocumentParseError("resource_limit", "Fixture parser failure.")
        return pages

    monkeypatch.setattr(financial_extractor, "extract_document_pages", parse_selected)
    context = load_manual_edit_context(issue.id, document.id, session_factory=file_session_factory)
    if mismatch is None:
        result = repository.submit_manual_extraction(
            issue.id, data, entered_by_email="reviewer@example.com", data_dir=tmp_path,
            expected_context=context, verify_copied_citations=True, session_factory=file_session_factory,
        )
        assert result.source_content_sha256 == digest
    else:
        with pytest.raises(IpoValidationError, match="Copied values"):
            repository.submit_manual_extraction(
                issue.id, data, entered_by_email="reviewer@example.com", data_dir=tmp_path,
                expected_context=context, verify_copied_citations=True, session_factory=file_session_factory,
            )
        assert repository.list_manual_extractions(issue.id, session_factory=file_session_factory) == []
    assert opened == [(tmp_path / "ipo" / "documents" / f"{digest}.pdf").resolve()]


def test_copied_manual_verifier_refuses_a_path_outside_the_cache(
    file_session_factory, tmp_path: Path, monkeypatch,
) -> None:
    """Reject an escaped source path before the PDF worker can open anything.

    Beginner note:
        Normal callers pass the downloader's verified relative path. This
        defensive check keeps a future caller from using the copy verifier as
        an arbitrary local-file parser; failed validation must precede parsing.
    """
    from backend.ipo import repository

    _issue, document, digest = _cached_document(file_session_factory, tmp_path)
    data = repository._proposal_payload_to_manual_data(_bound_payload(digest), document.id)

    def must_not_parse(_path: Path) -> tuple[ExtractedPage, ...]:
        """Fail if containment checking allows any worker invocation.

        Beginner note:
            The exception is deliberately outside the verifier's expected parse
            errors, so a forbidden invocation cannot masquerade as safe refusal.
        """
        raise AssertionError("An escaped file path reached the PDF parser")

    monkeypatch.setattr(financial_extractor, "extract_document_pages", must_not_parse)
    with pytest.raises(IpoValidationError, match="Copied values"):
        financial_extractor.verify_manual_copy_citations(data, cache_root=tmp_path, file_path="../outside.pdf")


@pytest.mark.parametrize("change", [
    "filing_date", "content_sha256", "file_path", "parse_status", "document_url", "document_type", "insert",
])
def test_source_mutations_wait_for_final_approval_state_lock(
    file_session_factory, file_db_engine, tmp_path: Path, monkeypatch, change: str,
) -> None:
    """Every approval input writer must serialize, including cache-only updates.

    Beginner note:
        Events stop approval immediately after its state UPDATE and observe the
        competing writer at its own UPDATE. No sleep guesses the interleaving.
        This proves SQLite serialization; PostgreSQL row behavior is a separate gate.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event

    from backend.ipo import repository
    from backend.storage import ipo_repository as storage

    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, file_session_factory, data_dir=tmp_path)
    held, release, attempted, finished = Event(), Event(), Event(), Event()
    original_lock = repository.lock_ipo_scoring_state

    def hold_approval(session, issue_id, **kwargs):
        """Keep approval paused while it holds the real database write boundary."""
        locked = original_lock(session, issue_id, **kwargs)
        held.set()
        assert release.wait(15), "test did not release the approval lock"
        return locked

    def observe_writer(_connection, _cursor, statement, _parameters, _context, _many):
        """Signal immediately before the competing write reaches the database."""
        if current_thread().name.startswith("source-writer") and statement.startswith("UPDATE ipo_scoring_state"):
            attempted.set()

    def mutate():
        """Use production storage boundaries for one independently committed writer."""
        try:
            with file_session_factory() as session:
                if change == "insert":
                    storage.insert_ipo_document(session, issue.id, {
                        "document_type": "rhp", "document_url": "https://www.sebi.gov.in/new.pdf",
                        "source_confidence": "high", "parse_status": "not_downloaded",
                    })
                elif change in {"content_sha256", "file_path", "parse_status"}:
                    value = {"content_sha256": "b" * 64, "file_path": "ipo/documents/changed.pdf",
                             "parse_status": "download_failed"}[change]
                    values: dict[str, Any] = {change: value}
                    if change == "parse_status":
                        # A failed download has no cache metadata. Keep this
                        # fixture a valid competing write so only locking, not
                        # the independent metadata CHECK, determines its outcome.
                        values.update(content_sha256=None, downloaded_at=None, file_path=None, page_count=None)
                    storage.update_ipo_document_cache_if_source_matches(
                        session, issue.id, document.id, expected_document_url=document.document_url,
                        expected_document_type=document.document_type, values=values,
                    )
                else:
                    source_value = {"filing_date": dt.date(2026, 10, 1), "document_url": "https://www.sebi.gov.in/changed.pdf",
                             "document_type": "drhp"}[change]
                    storage.update_ipo_document_row(session, issue.id, document.id, {change: source_value})
        finally:
            finished.set()

    monkeypatch.setattr(repository, "lock_ipo_scoring_state", hold_approval)
    event.listen(file_db_engine, "before_cursor_execute", observe_writer)
    try:
        with ThreadPoolExecutor(max_workers=1) as approvals, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="source-writer",
        ) as writers:
            approval = approvals.submit(repository.approve_extraction_proposal, proposal.id,
                reviewed_by_email="admin@example.com", data_dir=tmp_path, session_factory=file_session_factory)
            assert held.wait(15)
            mutation = writers.submit(mutate)
            try:
                assert attempted.wait(15)
                assert not finished.is_set(), "source write escaped the held approval state lock"
            finally:
                release.set()
            assert approval.result(timeout=15).source_content_sha256 == digest
            mutation.result(timeout=15)
    finally:
        release.set()
        event.remove(file_db_engine, "before_cursor_execute", observe_writer)


@pytest.mark.parametrize("change", ["newer_source", "conflicting_terms"])
def test_human_approval_requires_explicit_authority_change_decision(
    file_session_factory, tmp_path: Path, change: str,
) -> None:
    """A human click must explicitly acknowledge changing source or issue authority.

    Beginner note:
        This permission only resolves chronology/term conflicts; the captured
        manual baseline and verified citations remain mandatory afterwards.
    """
    from backend.ipo import repository

    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    repository.update_document(issue.id, document.id, IpoDocumentData(
        document_type="rhp", document_url=document.document_url,
        filing_date=dt.date(2025, 1, 1), source_confidence=Confidence.HIGH,
    ), session_factory=file_session_factory)
    proposal = _submit(issue.id, document.id, digest, file_session_factory, data_dir=tmp_path,
        payload_overrides={"price_band_high": "242", "price_band_high_page": 1})
    if change == "newer_source":
        create_document(issue.id, IpoDocumentData(document_type="rhp",
            document_url="https://www.sebi.gov.in/newer.pdf", filing_date=dt.date(2025, 2, 1),
            source_confidence=Confidence.HIGH), session_factory=file_session_factory)
    else:
        repository.update_issue(issue.id, replace(issue, price_band_high=Decimal("244")),
                                session_factory=file_session_factory)
    with pytest.raises(repository.IpoProposalConflictError, match="explicit"):
        approve_extraction_proposal(proposal.id, reviewed_by_email="human@example.com",
                                    data_dir=tmp_path, session_factory=file_session_factory)
    approved = approve_extraction_proposal(proposal.id, reviewed_by_email="human@example.com",
        accept_authority_change=True, data_dir=tmp_path, session_factory=file_session_factory)
    assert approved.source_document_id == document.id


def test_two_proposals_with_same_baseline_commit_only_one_revision(
    approval_session_factory: SessionFactory, tmp_path: Path, monkeypatch,
) -> None:
    """Two verified sources cannot both consume the same manual authority token.

    Args:
        approval_session_factory: Independent transactions on the selected database.
        tmp_path: Private cache shared by both proposal-verification workers.
        monkeypatch: Installs the barrier after real citation verification.

    Beginner note:
        The barrier lets both approvals finish slow verification before either
        reaches the final lock. One transaction wins; the other's fresh scalar
        read detects that win even though its own proposal is still pending.
        The same assertion runs on SQLite and PostgreSQL: a SQLite-wide writer
        lock alone is not evidence that PostgreSQL row serialization works.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.ipo import repository

    session_factory = approval_session_factory
    issue, document, digest = _cached_document(session_factory, tmp_path)
    other = create_document(issue.id, IpoDocumentData(document_type="drhp",
        document_url="https://www.sebi.gov.in/other.pdf", filing_date=dt.date(2024, 1, 1),
        source_confidence=Confidence.HIGH), session_factory=session_factory)
    with session_factory() as session:
        update_ipo_document_cache_if_source_matches(session, issue.id, other.id,
            expected_document_url=other.document_url, expected_document_type=other.document_type,
            values={"file_path": f"ipo/documents/{digest}.pdf", "content_sha256": digest,
                    "downloaded_at": _NOW, "parse_status": "pending"})
    proposals = [_submit(issue.id, source.id, digest, session_factory, data_dir=tmp_path)
                 for source in (document, other)]
    both_verified = Barrier(2)
    verify = repository._verify_cited_receipts_from_cached_pdf

    def synchronize(*args, **kwargs):
        """Finish real citation checks before releasing both approval workers.

        Args:
            *args: Positional arguments forwarded to the original verifier.
            **kwargs: Keyword arguments forwarded without changing verification.

        Raises:
            threading.BrokenBarrierError: Both workers do not reach the barrier.

        Beginner note:
            Waiting after verification makes the workers compete for the same
            baseline at publication; sleeping would not establish that ordering.
        """
        verify(*args, **kwargs)
        both_verified.wait(timeout=15)

    monkeypatch.setattr(repository, "_verify_cited_receipts_from_cached_pdf", synchronize)
    def approve(proposal):
        """Expose the winning revision or the losing authority-conflict code.

        Args:
            proposal: One verified proposal captured before the competing writes.

        Returns:
            Its committed manual record, or the typed conflict's stable code.

        Beginner note:
            Catch only the expected authority exception. Unrelated validation
            or database failures must still fail the worker and this regression.
        """
        try:
            return repository.approve_extraction_proposal(proposal.id, reviewed_by_email="human@example.com",
                accept_authority_change=True, data_dir=tmp_path, session_factory=session_factory)
        except repository.IpoProposalConflictError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(approve, proposals))
    assert sum(result == "manual_revision_changed" for result in results) == 1
    assert len(repository.list_manual_extractions(issue.id, session_factory=session_factory)) == 1
    assert repository.load_manual_edit_context(issue.id, document.id,
                                               session_factory=session_factory).manual_revision == 1


@pytest.mark.parametrize("change", ["filing_date", "content_sha256", "file_path", "document_url", "document_type"])
def test_source_mutation_during_verification_rejects_before_manual_insert(
    approval_session_factory: SessionFactory, tmp_path: Path, monkeypatch, change: str,
) -> None:
    """A writer that wins before approval's final lock invalidates its checked source.

    Args:
        approval_session_factory: Real independent transactions on either backend.
        tmp_path: Private PDF cache for the pending proposal.
        monkeypatch: Inserts a committed source mutation after verification.
        change: Authority-bearing source field changed by that transaction.

    Beginner note:
        The interleaving occurs synchronously after PDF receipt verification and
        before the lock; no timing assumptions are needed. Its rejection must
        leave both manual evidence and the original pending proposal intact.
        Both backends must reload current committed source data after locking.
    """
    from backend.ipo import repository
    from backend.storage import ipo_repository as storage

    session_factory = approval_session_factory
    issue, document, digest = _cached_document(session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, session_factory, data_dir=tmp_path)
    verify = repository._verify_cited_receipts_from_cached_pdf
    def mutate_after_verify(*args, **kwargs):
        """Commit a source edit between citation verification and final approval.

        Args:
            *args: Original positional verification arguments.
            **kwargs: Original keyword verification arguments.

        Beginner note:
            A separate real transaction wins this race deterministically. The
            approval must reread its source after taking the lock, before any
            manual row is inserted, rather than trusting these earlier checks.
        """
        verify(*args, **kwargs)
        value = {"filing_date": dt.date(2026, 10, 1), "content_sha256": "b" * 64,
                 "file_path": "ipo/documents/replaced.pdf", "document_url": "https://www.sebi.gov.in/new.pdf",
                 "document_type": "drhp"}[change]
        with session_factory() as session:
            storage.update_ipo_document_row(session, issue.id, document.id, {change: value})
    monkeypatch.setattr(repository, "_verify_cited_receipts_from_cached_pdf", mutate_after_verify)
    with pytest.raises(IpoValidationError, match="changed before approval"):
        approve_extraction_proposal(proposal.id, reviewed_by_email="human@example.com",
            data_dir=tmp_path, session_factory=session_factory)
    assert repository.list_manual_extractions(issue.id, session_factory=session_factory) == []
    assert list_extraction_proposals(issue_id=issue.id, session_factory=session_factory) == [proposal]


def test_approval_rejects_a_source_url_relabelled_after_proposal_submission(
    file_session_factory, tmp_path: Path,
) -> None:
    """The reviewed proposal's source URL must still identify its stored PDF.

    Beginner note:
        Keeping the SHA while correcting the registered URL does not update an
        immutable proposal's provenance. Explicit human authority acceptance
        must not relabel that old proposal as evidence from a different URL.
    """
    from backend.storage.ipo_repository import update_ipo_document_row

    issue, document, digest = _cached_document(file_session_factory, tmp_path)
    proposal = _submit(issue.id, document.id, digest, file_session_factory, data_dir=tmp_path)
    with file_session_factory() as session:
        update_ipo_document_row(session, issue.id, document.id,
                                {"document_url": "https://www.sebi.gov.in/relabelled.pdf"})
    with pytest.raises(IpoValidationError, match="source identity"):
        approve_extraction_proposal(proposal.id, reviewed_by_email="human@example.com",
            accept_authority_change=True, data_dir=tmp_path, session_factory=file_session_factory)



def _competing_cap_claims(file_session_factory, tmp_path, monkeypatch, *, other_cap="244", older=False):
    """Build independent verified PDFs and a delayed same-date draft submission.

    Args:
        file_session_factory: Disposable caller-owned transaction factory.
        tmp_path: Private content-addressed cache root.
        monkeypatch: Restores deterministic parser seams after the regression.
        other_cap: Competing cap, None for an absent claim.
        older: Whether the DRHP genuinely predates the current RHP.

    Returns:
        Owning issue, pending RHP, competing document, and a callable that commits
        the competing pending proposal using a baseline captured before any race.

    Beginner note:
        Each source has separate real cache bytes and matching host receipts.
        Delaying only the public submission lets tests place the competing write
        precisely before or after approval's state lock without timing sleeps.
    """
    from backend.ipo import repository

    issue, rhp, digest = _cached_document(file_session_factory, tmp_path, priced=False)
    other = create_document(issue.id, IpoDocumentData(
        document_type="drhp", document_url="https://www.sebi.gov.in/filings/equivalent-drhp.html",
        filing_date=dt.date(2024, 12, 31) if older else dt.date(2025, 1, 1), source_confidence=Confidence.HIGH,
    ), session_factory=file_session_factory)
    pdf = b"%PDF-1.7\ncompeting prospectus fixture\n%%EOF"
    other_digest = hashlib.sha256(pdf).hexdigest()
    other_path = f"ipo/documents/{other_digest}.pdf"
    (tmp_path / other_path).write_bytes(pdf)
    with file_session_factory() as session:
        assert update_ipo_document_cache_if_source_matches(session, issue.id, other.id,
            expected_document_url=other.document_url, expected_document_type="drhp",
            values={"content_sha256": other_digest, "file_path": other_path,
                    "parse_status": "pending", "downloaded_at": _NOW})
    rhp_pages = _verified_parsed_pages()
    other_pages = tuple(replace(page, text=page.text.replace("Rs 242", f"Rs {other_cap or '242'}"))
                        for page in rhp_pages)
    monkeypatch.setattr(table_extractor, "extract_document_pages",
        lambda path: other_pages if Path(path).name.startswith(other_digest) else rhp_pages)
    selected = _submit(issue.id, rhp.id, digest, file_session_factory, data_dir=tmp_path,
                       payload_overrides={"price_band_high": "242", "price_band_high_page": 1})
    with monkeypatch.context() as receipt_patch:
        receipt_patch.setattr(__import__(__name__, fromlist=["_verified_parsed_pages"]),
                             "_verified_parsed_pages", lambda: other_pages)
        payload = _bound_payload(other_digest, price_band_high=other_cap,
                                 price_band_high_page=1 if other_cap is not None else None)
    baseline = load_manual_edit_context(issue.id, other.id, session_factory=file_session_factory)

    def submit_competitor():
        """Commit through the production insertion path with an original baseline."""
        return repository.submit_extraction_proposal(issue.id, other.id, payload=payload, confidence=Confidence.HIGH,
            needs_review_reasons=(), model_version="ipo-010-extractor-v1", agent_model="fixture-model",
            source_content_sha256=other_digest, page_count=16, baseline_context=baseline,
            data_dir=tmp_path, session_factory=file_session_factory)
    return issue, selected, other, submit_competitor


@pytest.mark.parametrize("other_cap", ["242.00", "244", None])
@pytest.mark.parametrize("reverse_queue", [False, True])
def test_same_date_current_cap_claims_are_order_independent(
    file_session_factory, tmp_path: Path, monkeypatch, other_cap, reverse_queue: bool,
) -> None:
    """Unresolved same-date caps require review; equal or missing caps do not.

    Beginner note:
        The real approver and independent PDF receipts remain active. Reversing
        only the detached queue proves SQL order cannot adjudicate two claims.
        A missing DRHP cap neither erases nor invents the selected RHP's term.
    """
    from backend.ipo.agents import auto_approval

    issue, selected, _other, submit_competitor = _competing_cap_claims(
        file_session_factory, tmp_path, monkeypatch, other_cap=other_cap,
    )
    competitor = submit_competitor()
    queue = [selected, competitor] if reverse_queue else [competitor, selected]
    monkeypatch.setattr(auto_approval, "list_extraction_proposals", lambda **_kwargs: queue)
    result = auto_approval.auto_approve_ready_proposals(issue_id=issue.id, enabled=True,
        data_dir=tmp_path, session_factory=file_session_factory)
    assert result.failed == 0
    if other_cap == "244":
        assert result.approved == ()
        assert result.skipped_needs_review == 2
        current_issue = get_issue(issue.id, session_factory=file_session_factory)
        assert current_issue is not None and current_issue.price_band_high is None
        assert get_latest_manual_profile(issue.id, session_factory=file_session_factory) is None
    else:
        assert result.approved == (selected.id,)
        current_issue = get_issue(issue.id, session_factory=file_session_factory)
        assert current_issue is not None and current_issue.price_band_high == Decimal("242")


@pytest.mark.parametrize("competing_state", ["pending", "older", "rejected"])
def test_strict_approval_checks_only_current_pending_cap_cohort(
    file_session_factory, tmp_path: Path, monkeypatch, competing_state: str,
) -> None:
    """Direct automatic calls cannot bypass conflicts or promote rejected history.

    Beginner note:
        A genuinely older DRHP does not outrank a later RHP, and rejection removes
        a competing proposal from authority. Only the same-date pending case must
        fail, even if an irrelevant human acknowledgement flag is also supplied.
    """
    from backend.ipo import repository

    issue, selected, _other, submit_competitor = _competing_cap_claims(
        file_session_factory, tmp_path, monkeypatch, older=competing_state == "older",
    )
    competing = submit_competitor()
    if competing_state == "rejected":
        reject_extraction_proposal(competing.id, reviewed_by_email="human@example.com", reason="Incorrect terms",
                                   session_factory=file_session_factory)
    def approve():
        """Call the strict automatic boundary for each competing-claim scenario.

        Returns:
            The committed manual record when the remaining cohort is unambiguous.

        Raises:
            IpoProposalConflictError: Pending same-date cap claims still disagree.

        Beginner note:
            The human acknowledgement flag is deliberately also True. It must
            not relax the separately requested automatic-approval policy.
        """
        return approve_extraction_proposal(selected.id, reviewed_by_email="ipo-automation@screener.local",
            require_current_candidate=True, accept_authority_change=True,
            data_dir=tmp_path, session_factory=file_session_factory)
    if competing_state == "pending":
        with pytest.raises(repository.IpoProposalConflictError) as error:
            approve()
        assert error.value.code == "conflicting_proposal_terms"
        assert get_latest_manual_profile(issue.id, session_factory=file_session_factory) is None
    else:
        assert approve().source_document_id == selected.document_id


def test_competing_cap_inserted_during_verification_is_seen_under_final_lock(
    file_session_factory, tmp_path: Path, monkeypatch,
) -> None:
    """A queue-time clean candidate must be rejected after a competing writer wins.

    Beginner note:
        Insert a complete, verified competing proposal between PDF verification
        and the final state lock. Reusing the old queue would miss this claim;
        fresh locked proposal reads must preserve both pending rows and no evidence.
    """
    from backend.ipo import repository

    issue, selected, _other, submit_competitor = _competing_cap_claims(file_session_factory, tmp_path, monkeypatch)
    verify = repository._verify_cited_receipts_from_cached_pdf
    inserted = False
    def insert_after_verification(*args, **kwargs):
        """Publish one competing claim after checking the selected proposal's PDF.

        Args:
            *args: Original positional verification arguments.
            **kwargs: Original keyword verification arguments.

        Beginner note:
            Public submission also verifies its PDF through this callback. Set
            the one-shot guard first to avoid recursion while retaining real
            verification and persistence for both proposals.
        """
        nonlocal inserted
        verify(*args, **kwargs)
        if not inserted:
            inserted = True
            submit_competitor()
    monkeypatch.setattr(repository, "_verify_cited_receipts_from_cached_pdf", insert_after_verification)
    with pytest.raises(repository.IpoProposalConflictError) as error:
        approve_extraction_proposal(selected.id, reviewed_by_email="ipo-automation@screener.local",
            require_current_candidate=True, data_dir=tmp_path, session_factory=file_session_factory)
    assert error.value.code == "conflicting_proposal_terms"
    assert len(list_extraction_proposals(issue_id=issue.id, session_factory=file_session_factory)) == 2
    assert get_latest_manual_profile(issue.id, session_factory=file_session_factory) is None


def test_competing_proposal_writer_waits_for_approval_state_lock(
    file_session_factory, file_db_engine, tmp_path: Path, monkeypatch,
) -> None:
    """A later proposal insertion cannot enter approval's already-locked cohort.

    Beginner note:
        Events prove the public writer reaches its state UPDATE while approval
        holds that lock, then cannot finish until approval commits. Its original
        baseline remains zero after the winner advances it, preventing a later
        pass from treating the delayed proposal as fresh manual authority.
    """
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event, current_thread

    from sqlalchemy import event

    from backend.ipo import repository

    issue, selected, _other, submit_competitor = _competing_cap_claims(file_session_factory, tmp_path, monkeypatch)
    held, release, attempted, finished = Event(), Event(), Event(), Event()
    lock = repository.lock_ipo_scoring_state
    def hold_approval(session, issue_id, **kwargs):
        """Hold the real approval lock until the competing writer has attempted it.

        Args:
            session: Caller-owned transaction forwarded to the real lock helper.
            issue_id: Issue whose authority row serializes these operations.
            **kwargs: Revision conditions forwarded unchanged.

        Returns:
            The real lock helper's success flag.

        Raises:
            AssertionError: The controlling test does not release the worker.

        Beginner note:
            Only the approval thread pauses, and only after acquiring the lock.
            The public insertion thread must wait on that actual database lock.
        """
        locked = lock(session, issue_id, **kwargs)
        if current_thread().name.startswith("cohort-approval"):
            held.set()
            assert release.wait(15)
        return locked
    def observe_writer(_conn, _cursor, statement, _parameters, _context, _many):
        """Signal that the writer is about to execute its authority-row UPDATE.

        Args:
            _conn: SQLAlchemy connection supplied by the event hook.
            _cursor: Driver cursor supplied by the event hook.
            statement: SQL text inspected only for the known lock operation.
            _parameters: Bound values, intentionally neither read nor logged.
            _context: SQLAlchemy execution context, unused by the signal.
            _many: Whether the call is an executemany operation, unused here.

        Beginner note:
            This event proves an attempt, not a committed write. The separate
            finished event must remain unset while approval retains its lock.
        """
        if current_thread().name.startswith("cohort-writer") and statement.startswith("UPDATE ipo_scoring_state"):
            attempted.set()
    def write():
        """Submit the competing proposal and signal termination on either outcome.

        Returns:
            The real submitted proposal, still carrying its captured baseline.

        Beginner note:
            The finally signal also fires on errors. Future.result still raises
            those errors, so a failed writer cannot masquerade as a safe wait.
        """
        try:
            return submit_competitor()
        finally:
            finished.set()
    monkeypatch.setattr(repository, "lock_ipo_scoring_state", hold_approval)
    event.listen(file_db_engine, "before_cursor_execute", observe_writer)
    try:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cohort-approval") as approvals, ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="cohort-writer",
        ) as writers:
            approved = approvals.submit(approve_extraction_proposal, selected.id,
                reviewed_by_email="ipo-automation@screener.local", require_current_candidate=True,
                data_dir=tmp_path, session_factory=file_session_factory)
            assert held.wait(15)
            written = writers.submit(write)
            try:
                assert attempted.wait(15)
                assert not finished.is_set()
            finally:
                release.set()
            assert approved.result(timeout=15).source_document_id == selected.document_id
            competing = written.result(timeout=15)
    finally:
        release.set()
        event.remove(file_db_engine, "before_cursor_execute", observe_writer)
    assert competing.baseline_manual_revision == 0
    assert load_manual_edit_context(issue.id, selected.document_id,
                                    session_factory=file_session_factory).manual_revision == 1
    with pytest.raises(repository.IpoProposalConflictError) as error:
        approve_extraction_proposal(competing.id, reviewed_by_email="ipo-automation@screener.local",
            require_current_candidate=True, data_dir=tmp_path, session_factory=file_session_factory)
    assert error.value.code == "manual_revision_changed"
