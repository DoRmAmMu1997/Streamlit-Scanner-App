"""Persistence and service tests for IPO-004/005 immutable extraction revisions.

Beginner note:
The repository is where database ownership, cached-byte provenance, and the
authenticated actor meet. These tests use a real file-backed SQLite database
and a real hash-named PDF so they exercise the same transaction and filesystem
boundaries as production without making a network request.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from backend.ipo.financials.ratio_engine import IpoRatioName
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    Confidence,
    IpoDocumentData,
    IpoDocumentParseStatus,
    IpoIssueData,
    IpoIssueType,
    IpoStatus,
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    create_document,
    create_issue,
    delete_document,
    get_latest_ipo_ratios,
    get_latest_manual_profile,
    get_manual_extraction,
    list_manual_extractions,
    submit_manual_extraction,
)
from backend.storage.ipo_repository import update_ipo_document_cache_if_source_matches


def _payload(*, source_document_id: int, net_worth: str = "80") -> IpoManualExtractionData:
    """Build one complete revision suitable for repository scenarios.

    Beginner note:
        Repository tests focus on transactions and provenance, so this helper keeps
        the domain payload valid and lets each scenario vary only the source row or
        business fact it needs to exercise.
    """
    periods = tuple(
        IpoManualPeriodData(
            period_end=dt.date(year, 3, 31),
            revenue=Decimal(str(100 + year - 2023)),
            revenue_page=100 + year - 2023,
            ebitda=Decimal("20"),
            ebitda_page=110 + year - 2023,
            pat=Decimal("10"),
            pat_page=120 + year - 2023,
            profit_before_tax=Decimal("12"),
            profit_before_tax_page=123 + year - 2023,
            finance_cost=Decimal("2"),
            finance_cost_page=126 + year - 2023,
        )
        for year in (2023, 2024, 2025)
    )
    return IpoManualExtractionData(
        source_document_id=source_document_id,
        financial_amount_unit=IpoAmountUnit.CRORE_INR,
        issue_amount_unit=IpoAmountUnit.CRORE_INR,
        equity_share_unit=IpoShareUnit.LAKH_SHARES,
        periods=periods,
        net_worth=Decimal(net_worth),
        net_worth_page=130,
        total_debt=Decimal("12"),
        total_debt_page=131,
        cash=Decimal("5"),
        cash_page=132,
        cash_flow_from_operations=Decimal("14"),
        cash_flow_from_operations_page=133,
        equity_shares=Decimal("50"),
        equity_shares_page=134,
        eps=Decimal("2.50"),
        eps_page=135,
        nav_book_value=Decimal("18.75"),
        nav_book_value_page=136,
        objects_of_issue="Build a plant and repay borrowings.",
        objects_of_issue_page=137,
        fresh_issue_amount=Decimal("300"),
        fresh_issue_amount_page=138,
        ofs_amount=Decimal("0"),
        ofs_amount_page=139,
        promoter_holding_pre_issue=Decimal("75.25"),
        promoter_holding_pre_issue_page=140,
        promoter_holding_post_issue=Decimal("56.44"),
        promoter_holding_post_issue_page=141,
        total_assets=Decimal("150"),
        total_assets_page=142,
        current_liabilities=Decimal("45"),
        current_liabilities_page=143,
        post_issue_equity_shares=Decimal("60"),
        post_issue_equity_shares_page=144,
        peers=(
            IpoPeerValuationData(
                company_name="Peer One Ltd",
                source_page=210,
                metrics={
                    IpoPeerMetric.EPS: Decimal("8.25"),
                    IpoPeerMetric.PE: Decimal("21.40"),
                },
            ),
        ),
    )


def _cached_document(file_session_factory, data_dir: Path, *, document_type: str = "rhp"):
    """Create an issue and document row backed by verified local PDF bytes.

    Beginner note:
        Writing real hash-addressed bytes is important here: a metadata-only fixture
        would skip the containment and SHA-256 verification that protects a production
        manual extraction from stale or substituted source files.
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
            document_type=document_type,
            document_url=f"https://www.sebi.gov.in/filings/example-{document_type}.html",
            source_url="https://www.sebi.gov.in/filings/public-issues",
            source_confidence=Confidence.HIGH,
            record_hash=("a" if document_type == "rhp" else "b") * 64,
        ),
        session_factory=file_session_factory,
    )
    pdf_bytes = b"%PDF-1.7\nmanual extraction fixture\n%%EOF"
    digest = hashlib.sha256(pdf_bytes).hexdigest()
    relative_path = f"ipo/documents/{digest}.pdf"
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
                "file_path": relative_path,
                "page_count": None,
                "parse_status": IpoDocumentParseStatus.PENDING.value,
            },
        )
    return issue, document, digest, absolute_path


def test_submit_manual_extraction_persists_complete_detached_revision(
    file_session_factory, tmp_path: Path
) -> None:
    """One submission atomically returns sourced periods, peers, and canonical values.

    Beginner note:
        This happy-path test checks the detached record, unit conversions, actor,
        source digest, and audit metadata together so a successful insert cannot
        silently omit one part of the immutable revision.
    """
    issue, document, digest, _path = _cached_document(file_session_factory, tmp_path)
    # dict[str, Any] so nested payload fields (e.g. metadata) stay inspectable
    # in the assertions below without per-field casts (QUAL-007).
    events: list[dict[str, Any]] = []

    def _record_audit(**kwargs: Any) -> bool:
        """Capture the audit payload and report success like the real sink."""
        events.append(kwargs)
        return True

    created = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id),
        entered_by_email=" ADMIN@Example.com ",
        data_dir=tmp_path,
        now=lambda: dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC),
        audit_recorder=_record_audit,
        session_factory=file_session_factory,
    )

    assert created.issue_id == issue.id
    assert created.source_document_id == document.id
    assert created.source_content_sha256 == digest
    assert created.entered_by_email == "admin@example.com"
    assert created.submitted_at == dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC)
    assert len(created.periods) == 3
    assert created.peers[0].metrics[IpoPeerMetric.PE] == Decimal("21.4000")
    assert created.net_worth_inr == Decimal("800000000.0000")
    assert created.equity_shares_canonical == Decimal("5000000.0000")
    assert created.periods[-1].profit_before_tax == Decimal("12.0000")
    assert created.periods[-1].finance_cost == Decimal("2.0000")
    assert created.total_assets == Decimal("150.0000")
    assert created.current_liabilities == Decimal("45.0000")
    assert created.post_issue_equity_shares == Decimal("60.0000")
    assert created.period_values_inr()[-1] == {
        "period_end": dt.date(2025, 3, 31),
        "revenue_inr": Decimal("1020000000.0000"),
        "ebitda_inr": Decimal("200000000.0000"),
        "pat_inr": Decimal("100000000.0000"),
        "profit_before_tax_inr": Decimal("120000000.0000"),
        "finance_cost_inr": Decimal("20000000.0000"),
    }
    assert created.canonical_values == {
        "net_worth_inr": Decimal("800000000.0000"),
        "total_debt_inr": Decimal("120000000.0000"),
        "cash_inr": Decimal("50000000.0000"),
        "cash_flow_from_operations_inr": Decimal("140000000.0000"),
        "equity_shares": Decimal("5000000.0000"),
        "eps_inr_per_share": Decimal("2.5000"),
        "nav_book_value_inr_per_share": Decimal("18.7500"),
        "fresh_issue_amount_inr": Decimal("3000000000.0000"),
        "ofs_amount_inr": Decimal("0.0000"),
        "promoter_holding_pre_issue_pct": Decimal("75.2500"),
        "promoter_holding_post_issue_pct": Decimal("56.4400"),
        "total_assets_inr": Decimal("1500000000.0000"),
        "current_liabilities_inr": Decimal("450000000.0000"),
        "post_issue_equity_shares": Decimal("6000000.0000"),
    }
    assert get_manual_extraction(
        issue.id, created.id, session_factory=file_session_factory
    ) == created
    assert events[0]["event"] == "ipo_manual_extraction_submitted"
    assert events[0]["user_email"] == "admin@example.com"
    assert "objects_of_issue" not in events[0]["metadata"]


def test_latest_ratio_analysis_uses_one_detached_issue_and_revision_snapshot(
    file_session_factory, tmp_path: Path
) -> None:
    """The public facade should calculate latest ratios without persisting them.

    Beginner note:
    The database stores source facts, not derived ratios. Re-running this service
    is therefore deterministic and cannot create duplicate ratio-history rows.
    """
    issue, document, digest, _path = _cached_document(file_session_factory, tmp_path)
    created = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id),
        entered_by_email="admin@example.com",
        data_dir=tmp_path,
        now=lambda: dt.datetime(2026, 7, 3, 9, tzinfo=dt.UTC),
        audit_recorder=lambda **_kwargs: True,
        session_factory=file_session_factory,
    )

    first = get_latest_ipo_ratios(issue.id, session_factory=file_session_factory)
    second = get_latest_ipo_ratios(issue.id, session_factory=file_session_factory)

    assert first == second
    assert first is not None
    assert first.extraction_id == created.id
    assert first.source_content_sha256 == digest
    assert first.price_band_high == Decimal("242.0000")
    assert first.issue_updated_at == issue.updated_at
    assert first.ratios[IpoRatioName.REVENUE_CAGR].value == Decimal("0.9950")


def test_latest_ratio_analysis_distinguishes_empty_and_unknown_issues(
    file_session_factory, tmp_path: Path
) -> None:
    """A known issue without evidence is empty; an unknown id is an ownership error.

    Beginner note:
        ``None`` means a known IPO has no manual profile yet; an exception means the
        parent IPO does not exist. A UI can therefore show an honest empty state
        without hiding a bad or cross-issue identifier.
    """
    issue, _document, _digest, _path = _cached_document(file_session_factory, tmp_path)

    assert get_latest_ipo_ratios(issue.id, session_factory=file_session_factory) is None
    with pytest.raises(IpoNotFoundError, match="was not found"):
        get_latest_ipo_ratios(999_999, session_factory=file_session_factory)


def test_corrections_append_and_latest_profile_is_deterministic(
    file_session_factory, tmp_path: Path
) -> None:
    """A correction creates history; newest timestamp then id selects the profile."""
    issue, document, _digest, _path = _cached_document(file_session_factory, tmp_path)
    first = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id, net_worth="80"),
        entered_by_email="admin@example.com",
        data_dir=tmp_path,
        now=lambda: dt.datetime(2026, 7, 1, 9, tzinfo=dt.UTC),
        audit_recorder=lambda **_kwargs: True,
        session_factory=file_session_factory,
    )
    second = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id, net_worth="81"),
        entered_by_email="admin@example.com",
        data_dir=tmp_path,
        now=lambda: dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC),
        audit_recorder=lambda **_kwargs: True,
        session_factory=file_session_factory,
    )

    assert [row.id for row in list_manual_extractions(
        issue.id, session_factory=file_session_factory
    )] == [second.id, first.id]
    latest = get_latest_manual_profile(issue.id, session_factory=file_session_factory)
    assert latest is not None
    assert latest.id == second.id
    assert latest.net_worth == Decimal("81.0000")


def test_submit_rejects_uncached_wrong_type_and_corrupt_documents(
    file_session_factory, tmp_path: Path
) -> None:
    """Only intact, cached DRHP/RHP bytes may become manual provenance."""
    issue, document, _digest, path = _cached_document(file_session_factory, tmp_path)
    path.write_bytes(b"%PDF-1.7\nchanged")
    with pytest.raises(IpoValidationError, match="verified cached PDF"):
        submit_manual_extraction(
            issue.id,
            _payload(source_document_id=document.id),
            entered_by_email="admin@example.com",
            data_dir=tmp_path,
            audit_recorder=lambda **_kwargs: True,
            session_factory=file_session_factory,
        )

    other_issue, final_document, _digest, _path = _cached_document(
        file_session_factory, tmp_path / "other", document_type="final_offer"
    )
    with pytest.raises(IpoValidationError, match="only a cached DRHP or RHP"):
        submit_manual_extraction(
            other_issue.id,
            _payload(source_document_id=final_document.id),
            entered_by_email="admin@example.com",
            data_dir=tmp_path / "other",
            audit_recorder=lambda **_kwargs: True,
            session_factory=file_session_factory,
        )


def test_audit_failure_does_not_rollback_authoritative_revision(
    file_session_factory, tmp_path: Path
) -> None:
    """The extraction remains committed when the secondary audit sink is unavailable."""
    issue, document, _digest, _path = _cached_document(file_session_factory, tmp_path)

    def broken_audit(**_kwargs):
        """Simulate an unavailable best-effort audit database."""
        raise RuntimeError("audit unavailable")

    created = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id),
        entered_by_email="admin@example.com",
        data_dir=tmp_path,
        audit_recorder=broken_audit,
        session_factory=file_session_factory,
    )

    assert get_manual_extraction(
        issue.id, created.id, session_factory=file_session_factory
    ) == created


def test_document_deletion_retains_immutable_source_snapshots(
    file_session_factory, tmp_path: Path
) -> None:
    """Deleting mutable metadata nulls its FK but preserves URL and hashes."""
    issue, document, digest, _path = _cached_document(file_session_factory, tmp_path)
    created = submit_manual_extraction(
        issue.id,
        _payload(source_document_id=document.id),
        entered_by_email="admin@example.com",
        data_dir=tmp_path,
        audit_recorder=lambda **_kwargs: True,
        session_factory=file_session_factory,
    )

    assert delete_document(
        issue.id, document.id, session_factory=file_session_factory
    ) is True
    retained = get_manual_extraction(
        issue.id, created.id, session_factory=file_session_factory
    )

    assert retained is not None
    assert retained.source_document_id is None
    assert retained.source_document_url == document.document_url
    assert retained.source_record_hash == document.record_hash
    assert retained.source_content_sha256 == digest
