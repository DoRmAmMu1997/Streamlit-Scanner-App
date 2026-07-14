"""IPO-006 scoring-service tests: evidence assembly, fingerprints, idempotency.

Beginner note:
``rescore_issue`` is the bridge between stored evidence and the immutable
evaluation history, and its fingerprint is what makes the screener job safe
to re-run. These tests use the real repository stack on a file-backed
database — the same engine pragmas production uses — so the round trip they
pin (derive -> flags -> score -> persist -> skip) is the real one.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualPeriodData,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    Confidence,
    IpoDocumentData,
    IpoDocumentParseStatus,
    IpoEnrichmentSignalData,
    IpoEnrichmentSignalType,
    IpoIssueData,
    IpoIssueType,
    IpoStatus,
    IpoSubscriptionData,
)
from backend.ipo.repository import (
    create_document,
    create_issue,
    create_subscription,
    record_enrichment_signals,
    submit_manual_extraction,
    update_issue,
)
from backend.ipo.scoring.service import (
    SCREENER_MODEL_VERSION,
    compute_inputs_fingerprint,
    rescore_issue,
)
from backend.storage.ipo_repository import update_ipo_document_cache_if_source_matches

_AS_OF = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)


def _issue_data(**overrides: Any) -> IpoIssueData:
    """Build the reusable issue payload used by the scenarios below."""
    values: dict[str, Any] = {
        "company_name": "Example Ltd",
        "issue_type": IpoIssueType.MAINBOARD,
        "status": IpoStatus.RHP_FILED,
        "source_confidence": Confidence.HIGH,
        "price_band_low": Decimal("230"),
        "price_band_high": Decimal("242"),
    }
    values.update(overrides)
    return IpoIssueData(**values)


def _profile_data(source_document_id: int) -> IpoManualExtractionData:
    """Build one complete healthy-company submission in crore INR."""
    periods = tuple(
        IpoManualPeriodData(
            period_end=dt.date(year, 3, 31),
            revenue=Decimal(str(100 * (year - 2022))),
            revenue_page=10,
            ebitda=Decimal(str(25 * (year - 2022))),
            ebitda_page=10,
            pat=Decimal(str(12 * (year - 2022))),
            pat_page=10,
            profit_before_tax=Decimal(str(15 * (year - 2022))),
            profit_before_tax_page=10,
            finance_cost=Decimal("2"),
            finance_cost_page=10,
        )
        for year in (2023, 2024, 2025)
    )
    return IpoManualExtractionData(
        source_document_id=source_document_id,
        financial_amount_unit=IpoAmountUnit.CRORE_INR,
        issue_amount_unit=IpoAmountUnit.CRORE_INR,
        equity_share_unit=IpoShareUnit.CRORE_SHARES,
        periods=periods,
        net_worth=Decimal("180"),
        net_worth_page=11,
        total_debt=Decimal("20"),
        total_debt_page=11,
        cash=Decimal("30"),
        cash_page=11,
        cash_flow_from_operations=Decimal("40"),
        cash_flow_from_operations_page=11,
        equity_shares=Decimal("1.8"),
        equity_shares_page=12,
        eps=Decimal("20"),
        eps_page=12,
        nav_book_value=Decimal("100"),
        nav_book_value_page=12,
        objects_of_issue="Capacity expansion and repayment of borrowings.",
        objects_of_issue_page=13,
        fresh_issue_amount=Decimal("300"),
        fresh_issue_amount_page=13,
        ofs_amount=Decimal("100"),
        ofs_amount_page=13,
        promoter_holding_pre_issue=Decimal("72"),
        promoter_holding_pre_issue_page=14,
        promoter_holding_post_issue=Decimal("58"),
        promoter_holding_post_issue_page=14,
        total_assets=Decimal("260"),
        total_assets_page=15,
        current_liabilities=Decimal("40"),
        current_liabilities_page=15,
        post_issue_equity_shares=Decimal("2"),
        post_issue_equity_shares_page=15,
        peers=(
            IpoPeerValuationData(
                company_name="Peer One Ltd",
                source_page=16,
                metrics={"pe": Decimal("25")},
            ),
        ),
    )


def _scored_issue(file_session_factory, data_dir: Path):
    """Create an issue with a verified cached RHP and one manual revision."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)
    document = create_document(
        issue.id,
        IpoDocumentData(
            document_type="rhp",
            document_url="https://www.sebi.gov.in/filings/example-rhp.html",
            source_confidence=Confidence.HIGH,
        ),
        session_factory=file_session_factory,
    )
    pdf_bytes = b"%PDF-1.7\nscoring service fixture\n%%EOF"
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
    submit_manual_extraction(
        issue.id,
        _profile_data(document.id),
        entered_by_email="admin@example.com",
        data_dir=data_dir,
        session_factory=file_session_factory,
    )
    return issue


def test_rescore_persists_a_complete_ipo_006_evaluation(
    file_session_factory, tmp_path: Path
) -> None:
    """A complete profile scores end to end with flags and a fingerprint."""
    issue = _scored_issue(file_session_factory, tmp_path)

    outcome = rescore_issue(
        issue.id, as_of=_AS_OF, session_factory=file_session_factory
    )

    assert outcome.status == "evaluated"
    evaluation = outcome.evaluation
    assert evaluation is not None
    assert evaluation.model_version == SCREENER_MODEL_VERSION
    assert evaluation.inputs_fingerprint is not None
    assert len(evaluation.inputs_fingerprint) == 64
    # The full seven-flag report rides with the verdict for auditability.
    assert len(evaluation.result.caution_flags) == 7
    # Factors derived from documents carry provenance in their reasons.
    assert any("ipo-ratio-v1" in reason for reason in evaluation.result.reasons)
    # QIB and GMP evidence is absent, so the verdict degrades its confidence
    # instead of failing: both are optional factors.
    assert evaluation.result.confidence is Confidence.LOW
    assert set(evaluation.result.missing_data) == {"qib_subscription", "gmp_sentiment"}


def test_rescore_is_idempotent_until_an_input_changes(
    file_session_factory, tmp_path: Path
) -> None:
    """Unchanged evidence skips; a real change re-scores with a new fingerprint."""
    issue = _scored_issue(file_session_factory, tmp_path)
    first = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert first.status == "evaluated"

    second = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert second.status == "skipped_unchanged"
    assert second.evaluation is not None
    assert first.evaluation is not None
    assert second.evaluation.score_id == first.evaluation.score_id

    update_issue(
        issue.id,
        _issue_data(price_band_high=Decimal("300")),
        session_factory=file_session_factory,
    )
    third = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert third.status == "evaluated"
    assert third.evaluation is not None
    assert third.evaluation.inputs_fingerprint != first.evaluation.inputs_fingerprint


def test_new_subscription_and_enrichment_change_the_fingerprint(
    file_session_factory, tmp_path: Path
) -> None:
    """Fresh demand or web observations re-open an already-scored issue."""
    issue = _scored_issue(file_session_factory, tmp_path)
    rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)

    create_subscription(
        issue.id,
        IpoSubscriptionData(
            captured_at=_AS_OF,
            qib_multiple=Decimal("22"),
            source_confidence=Confidence.HIGH,
        ),
        session_factory=file_session_factory,
    )
    with_subscription = rescore_issue(
        issue.id, as_of=_AS_OF, session_factory=file_session_factory
    )
    assert with_subscription.status == "evaluated"
    assert with_subscription.evaluation is not None
    assert "qib_subscription" not in with_subscription.evaluation.result.missing_data

    record_enrichment_signals(
        issue.id,
        [
            IpoEnrichmentSignalData(
                signal_type=IpoEnrichmentSignalType.GMP,
                captured_at=_AS_OF,
                query_text="Example Ltd IPO GMP grey market premium",
                payload=({"title": "GMP report"},),
                parsed_value=Decimal("25"),
                quarantined=False,
                confidence=Confidence.LOW,
                source_policy="serpapi-low-confidence-v1",
            )
        ],
        session_factory=file_session_factory,
    )
    with_gmp = rescore_issue(
        issue.id, as_of=_AS_OF, session_factory=file_session_factory
    )
    assert with_gmp.status == "evaluated"
    assert with_gmp.evaluation is not None
    assert with_gmp.evaluation.result.missing_data == ()
    assert with_gmp.evaluation.result.confidence is Confidence.HIGH


def test_issue_without_a_profile_reports_insufficient_inputs(
    file_session_factory,
) -> None:
    """No verified evidence means no evaluation row — the queue handles it."""
    issue = create_issue(_issue_data(), session_factory=file_session_factory)

    outcome = rescore_issue(
        issue.id, as_of=_AS_OF, session_factory=file_session_factory
    )

    assert outcome.status == "insufficient_inputs"
    assert outcome.evaluation is None
    assert outcome.missing == ("manual_extraction",)


def test_fingerprint_hashes_time_derived_facts_not_the_clock(
    file_session_factory, tmp_path: Path
) -> None:
    """Two runs at different instants inside the same windows hash identically."""
    issue = _scored_issue(file_session_factory, tmp_path)
    from backend.ipo.repository import (
        get_issue,
        get_latest_ipo_ratios,
        get_latest_manual_profile,
    )
    from backend.ipo.scoring.factor_derivation import IpoFactorInputs

    def inputs_at(as_of: dt.datetime) -> IpoFactorInputs:
        """Assemble the same evidence bundle at one injected instant."""
        loaded_issue = get_issue(issue.id, session_factory=file_session_factory)
        assert loaded_issue is not None
        return IpoFactorInputs(
            issue=loaded_issue,
            profile=get_latest_manual_profile(
                issue.id, session_factory=file_session_factory
            ),
            ratios=get_latest_ipo_ratios(
                issue.id, session_factory=file_session_factory
            ),
            subscription=None,
            as_of=as_of,
            enrichment=(),
        )

    morning = compute_inputs_fingerprint(inputs_at(_AS_OF))
    evening = compute_inputs_fingerprint(inputs_at(_AS_OF + dt.timedelta(hours=6)))

    assert morning == evening
