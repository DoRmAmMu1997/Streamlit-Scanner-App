"""IPO-006 scoring-service tests: evidence assembly, fingerprints, idempotency.

Beginner note:
``rescore_issue`` is the bridge between stored evidence and the immutable
evaluation history, and its fingerprint is what makes the screener job safe
to re-run. These tests use the real repository stack on a file-backed
database — the same engine pragmas production uses — so the round trip they
pin (derive -> flags -> score -> persist -> skip) is the real one.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import hashlib
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

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
    load_ipo_factor_inputs_snapshot,
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


def _input_revision(session, issue_id: int) -> int:
    """Require the state fixture and return its authoritative scalar revision.

    Beginner note:
        SQL scalar reads avoid identity-map caching when checking mutation tokens.
    """
    from backend.storage.ipo_repository import get_ipo_scoring_state_values

    state = get_ipo_scoring_state_values(session, issue_id)
    assert state is not None
    return state[0]


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


def test_return_to_a_selects_old_receipt_as_current(file_session_factory, tmp_path: Path) -> None:
    """Beginner note: newest history incorrectly remained B after A was reused."""
    from backend.ipo.repository import get_latest_evaluation, list_evaluations
    from backend.ipo.scoring import service

    issue = _scored_issue(file_session_factory, tmp_path)
    first = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    update_issue(issue.id, _issue_data(price_band_high=Decimal("300")), session_factory=file_session_factory)
    second = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    update_issue(issue.id, _issue_data(), session_factory=file_session_factory)
    third = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert first.evaluation == third.evaluation
    assert len(list_evaluations(issue.id, session_factory=file_session_factory)) == 2
    assert get_latest_evaluation(issue.id, session_factory=file_session_factory) == second.evaluation
    assert hasattr(service, "get_current_evaluation"), "Need verified current selection separate from history"
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert current.evaluation == first.evaluation
    assert current.fresh
    assert current.last_verified_at is not None


def test_legacy_claimed_fingerprint_cannot_certify_invented_scorecard(
    file_session_factory, tmp_path: Path, caplog
) -> None:
    """Reject a fabricated legacy scorecard even when its fingerprint is genuine.

    Beginner note:
        The old reuse path selected a caller's 100-point score instead of the
        result derived from stored evidence. A failed verification must preserve
        both the immutable history and the unverified current-selection state.
    """
    from backend.ipo import repository
    from backend.ipo.models import FactorAssessment, IpoValidationError
    from backend.ipo.scoring import service
    from backend.ipo.scoring.factor_derivation import derive_score_input
    from backend.ipo.scoring.score_model import score_ipo
    from backend.storage.ipo_repository import get_ipo_scoring_state_values

    issue = _scored_issue(file_session_factory, tmp_path)
    snapshot = repository.load_ipo_scoring_snapshot(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    legitimate = derive_score_input(snapshot.inputs)
    invented_factor = FactorAssessment(Decimal("100"), "Caller supplied assessment")
    invented = dataclasses.replace(
        legitimate,
        business_quality=invented_factor,
        financial_growth=invented_factor,
        return_ratios=invented_factor,
        valuation=invented_factor,
        qib_subscription=invented_factor,
        promoter_quality=invented_factor,
        gmp_sentiment=invented_factor,
    )
    legacy = repository.evaluate_issue(issue.id, invented,
        inputs_fingerprint=compute_inputs_fingerprint(snapshot.inputs), model_version=SCREENER_MODEL_VERSION,
        session_factory=file_session_factory)
    assert legacy.result.score != score_ipo(legitimate).score
    with file_session_factory() as session:
        state_before = get_ipo_scoring_state_values(session, issue.id)
    with pytest.raises(IpoValidationError, match="payload"):
        rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    with file_session_factory() as session:
        assert get_ipo_scoring_state_values(session, issue.id) == state_before
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == [legacy]
    assert not service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh
    assert not any("ipo_issue_scored" in record.message for record in caplog.records)


@pytest.mark.parametrize(("half", "field", "replacement"), [
    *(("score", name, Decimal("37.25")) for name in (
        "business_quality", "financial_growth", "return_ratios", "valuation",
        "qib_subscription", "promoter_quality", "gmp_sentiment", "total_score",
    )),
    ("score", "contributions_json", {}),
    ("score", "breakdown_json", []),
    ("score", "missing_data_json", ["business_quality"]),
    ("score", "reasons_json", ["Caller supplied score explanation"]),
    ("recommendation", "recommendation", "Not Recommended"),
    ("recommendation", "recommendation_type", "Skip"),
    ("recommendation", "confidence", "high"),
    ("recommendation", "reasons_json", ["Caller supplied verdict explanation"]),
    ("recommendation", "missing_data_json", ["business_quality"]),
    ("recommendation", "source_documents_json", []),
    ("recommendation", "caution_flags_json", []),
])
def test_reuse_compares_complete_receipt_not_only_total_or_identity(
    file_session_factory, tmp_path: Path, half, field, replacement
) -> None:
    """Reject each changed receipt component without certifying or rewriting it.

    Beginner note:
        The old path compared identity alone. The fixture changes one field at
        a time while retaining the fingerprint, proving that factors, verdicts,
        reasons and source receipts all participate in verification. The chosen
        replacement must actually differ from the fixture's stored value.
    """
    from backend.ipo import repository
    from backend.ipo.models import IpoValidationError
    from backend.ipo.scoring.caution_flags import evaluate_caution_flags
    from backend.ipo.scoring.factor_derivation import derive_score_input
    from backend.storage.ipo_repository import get_ipo_evaluation_rows, get_ipo_scoring_state_values

    issue = _scored_issue(file_session_factory, tmp_path)
    inputs = repository.load_ipo_factor_inputs_snapshot(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    legacy = repository.evaluate_issue(issue.id, derive_score_input(inputs),
        caution_flags=evaluate_caution_flags(inputs), inputs_fingerprint=compute_inputs_fingerprint(inputs),
        model_version=SCREENER_MODEL_VERSION, session_factory=file_session_factory)
    with file_session_factory() as session:
        rows = get_ipo_evaluation_rows(session, issue.id, legacy.score_id)
        assert rows is not None
        row = rows[0] if half == "score" else rows[1]
        assert getattr(row, field) != replacement, "Fixture must change actual persisted semantics"
        setattr(row, field, replacement)
    history_before = repository.list_evaluations(issue.id, session_factory=file_session_factory)
    with file_session_factory() as session:
        state_before = get_ipo_scoring_state_values(session, issue.id)
    with pytest.raises(IpoValidationError, match="payload"):
        rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == history_before
    with file_session_factory() as session:
        assert get_ipo_scoring_state_values(session, issue.id) == state_before


def test_matching_legacy_receipt_can_be_verified_with_reordered_source_set(
    file_session_factory, tmp_path: Path
) -> None:
    """Verify a matching historical pair despite harmless source-set reordering.

    Beginner note:
        Refusing every old pair would break idempotency and A-B-A reuse. Source
        URLs describe a set, so changing their order must still select the same
        immutable receipt and leave its original calculation timestamp intact.
    """
    from backend.ipo import repository
    from backend.ipo.scoring import service
    from backend.ipo.scoring.caution_flags import evaluate_caution_flags
    from backend.ipo.scoring.factor_derivation import derive_score_input

    issue = _scored_issue(file_session_factory, tmp_path)
    create_document(issue.id, IpoDocumentData(document_type="drhp",
        document_url="https://www.sebi.gov.in/filings/older-drhp.html", source_confidence=Confidence.HIGH),
        session_factory=file_session_factory)
    inputs = repository.load_ipo_factor_inputs_snapshot(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    score_input = derive_score_input(inputs)
    legacy = repository.evaluate_issue(issue.id,
        dataclasses.replace(score_input, source_documents=tuple(reversed(score_input.source_documents))),
        caution_flags=evaluate_caution_flags(inputs), inputs_fingerprint=compute_inputs_fingerprint(inputs),
        model_version=SCREENER_MODEL_VERSION, session_factory=file_session_factory)
    outcome = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert outcome.status == "skipped_unchanged" and outcome.evaluation == legacy
    assert service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh


def test_registered_sources_enter_semantic_identity(file_session_factory, tmp_path: Path) -> None:
    """Beginner note: missing source URLs in the hash reused an incomplete receipt."""
    issue = _scored_issue(file_session_factory, tmp_path)
    first = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    create_document(issue.id, IpoDocumentData(document_type="drhp",
        document_url="https://www.sebi.gov.in/filings/older-drhp.html", source_confidence=Confidence.HIGH),
        session_factory=file_session_factory)
    second = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert second.status == "evaluated"
    assert second.evaluation != first.evaluation


def test_dashboard_refresh_reuses_calculation_but_is_current(file_session_factory, tmp_path: Path) -> None:
    """Beginner note: evidence newer than scored_at is valid after reverification."""
    from backend.ipo.dashboard import build_dashboard_snapshot
    from backend.ipo.scoring.service import get_current_evaluation

    issue = _scored_issue(file_session_factory, tmp_path)
    first = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    before = get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    update_issue(issue.id, _issue_data(), session_factory=file_session_factory)
    assert not get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh
    second = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    after = get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert before.last_verified_at is not None and after.last_verified_at is not None
    assert before.last_verified_at <= after.last_verified_at
    assert first.evaluation is not None
    assert first.evaluation == second.evaluation
    row = build_dashboard_snapshot(now=_AS_OF, session_factory=file_session_factory).rows[0]
    assert not row.evaluation_stale
    assert row.calculated_at == first.evaluation.scored_at
    assert row.last_verified_at == after.last_verified_at


def test_publication_reloads_changed_inputs_and_rolls_back_late_failure(
    file_session_factory, tmp_path: Path, monkeypatch
) -> None:
    """Beginner note: a CAS prevents committing a receipt for overwritten inputs."""
    from backend.ipo import repository
    from backend.ipo.scoring import service

    issue = _scored_issue(file_session_factory, tmp_path)
    real = service.derive_score_input
    calls = []

    def change_once(inputs):
        """Commit newer evidence after the first snapshot, before publication."""
        calls.append(inputs.issue.price_band_high)
        if len(calls) == 1:
            update_issue(issue.id, _issue_data(price_band_high=Decimal("300")), session_factory=file_session_factory)
        return real(inputs)

    monkeypatch.setattr(service, "derive_score_input", change_once)
    outcome = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert calls == [Decimal("242"), Decimal("300")]
    assert len(repository.list_evaluations(issue.id, session_factory=file_session_factory)) == 1
    assert service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh

    update_issue(issue.id, _issue_data(price_band_high=Decimal("320")), session_factory=file_session_factory)

    def fail_selection(*args, **kwargs):
        """Fail after pair insertion to prove the outer rollback includes history."""
        raise ValueError("late publication failure")

    monkeypatch.setattr(repository, "select_ipo_current_evaluation", fail_selection)
    with pytest.raises(ValueError, match="late publication"):
        rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == [outcome.evaluation]
    assert not service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh


def test_snapshot_aba_mutation_and_total_retry_budget(file_session_factory, tmp_path: Path, monkeypatch) -> None:
    """Beginner note: equal final prices cannot conceal an intervening A-B-A write."""
    from backend.ipo import repository
    from backend.ipo.scoring.state import IpoScoringConflictError

    issue = _scored_issue(file_session_factory, tmp_path)
    real = repository.get_latest_ipo_manual_extraction
    calls = []

    def change_during_read(session, issue_id):
        """Restore the same price through two commits while input reads are open."""
        profile = real(session, issue_id)
        calls.append(issue_id)
        update_issue(issue.id, _issue_data(price_band_high=Decimal("300")), session_factory=file_session_factory)
        update_issue(issue.id, _issue_data(), session_factory=file_session_factory)
        return profile

    monkeypatch.setattr(repository, "get_latest_ipo_manual_extraction", change_during_read)
    with pytest.raises(IpoScoringConflictError, match="three attempts"):
        rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert calls == [issue.id] * 3
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == []


def test_same_input_concurrent_publication_is_idempotent(file_session_factory, tmp_path: Path, monkeypatch) -> None:
    """Beginner note: real concurrent transactions must retain exactly one pair."""
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from backend.ipo import repository
    from backend.ipo.scoring import service

    issue = _scored_issue(file_session_factory, tmp_path)
    barrier = Barrier(2)
    real = service.load_ipo_scoring_snapshot

    def synchronized_snapshot(*args, **kwargs):
        """Release both real scoring transactions with the same detached snapshot."""
        snapshot = real(*args, **kwargs)
        barrier.wait(timeout=15)
        return snapshot

    monkeypatch.setattr(service, "load_ipo_scoring_snapshot", synchronized_snapshot)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(rescore_issue, issue.id, as_of=_AS_OF, session_factory=file_session_factory)
                   for _ in range(2)]
        outcomes = [future.result(timeout=30) for future in futures]
    assert {outcome.status for outcome in outcomes} == {"evaluated", "skipped_unchanged"}
    assert outcomes[0].evaluation == outcomes[1].evaluation
    assert len(repository.list_evaluations(issue.id, session_factory=file_session_factory)) == 1


def test_snapshot_and_publication_share_three_attempts(file_session_factory, tmp_path: Path, monkeypatch) -> None:
    """Beginner note: separate retry loops could multiply the promised three-attempt limit."""
    from backend.ipo import repository
    from backend.ipo.scoring import service
    from backend.ipo.scoring.state import IpoScoringConflictError

    issue = _scored_issue(file_session_factory, tmp_path)
    previous = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    real_snapshot = service.load_ipo_scoring_snapshot
    real_derive = service.derive_score_input
    snapshots, publications = [], []

    def conflicting_snapshot(*args, **kwargs):
        """Spend the first attempt on assembly, leaving only two publication tries."""
        snapshots.append(1)
        if len(snapshots) == 1:
            raise IpoScoringConflictError("snapshot conflict")
        return real_snapshot(*args, **kwargs)

    def conflicting_calculation(inputs):
        """Commit a real input mutation before each attempted publication."""
        publications.append(1)
        update_issue(issue.id, _issue_data(price_band_high=Decimal(300 + len(publications))),
                     session_factory=file_session_factory)
        return real_derive(inputs)

    monkeypatch.setattr(service, "load_ipo_scoring_snapshot", conflicting_snapshot)
    monkeypatch.setattr(service, "derive_score_input", conflicting_calculation)
    with pytest.raises(IpoScoringConflictError, match="three attempts"):
        rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert len(snapshots) == 3 and len(publications) == 2
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == [previous.evaluation]
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert not current.fresh and current.evaluation == previous.evaluation


def test_clock_only_expiry_near_close_and_model_freshness(file_session_factory, tmp_path: Path, monkeypatch) -> None:
    """Beginner note: clock/model changes invalidate receipts without DB mutations."""
    from backend.ipo.scoring import service

    issue = _scored_issue(file_session_factory, tmp_path)
    signal = IpoEnrichmentSignalData(signal_type=IpoEnrichmentSignalType.GMP, captured_at=_AS_OF,
        query_text="Example Ltd IPO GMP", payload=({"title": "GMP report"},), parsed_value=Decimal("25"),
        quarantined=False, confidence=Confidence.LOW, source_policy="serpapi-low-confidence-v2")
    record_enrichment_signals(issue.id, [signal], session_factory=file_session_factory)
    first = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    threshold = _AS_OF + dt.timedelta(days=5)
    assert service.get_current_evaluation(issue.id, as_of=threshold, session_factory=file_session_factory).fresh
    assert not service.get_current_evaluation(issue.id, as_of=threshold + dt.timedelta(microseconds=1),
                                              session_factory=file_session_factory).fresh
    rescore_issue(issue.id, as_of=threshold + dt.timedelta(microseconds=1), session_factory=file_session_factory)
    record_enrichment_signals(issue.id, [dataclasses.replace(signal, captured_at=threshold)],
                              session_factory=file_session_factory)
    restored = rescore_issue(issue.id, as_of=threshold, session_factory=file_session_factory)
    assert restored.evaluation == first.evaluation
    monkeypatch.setattr(service, "SCREENER_MODEL_VERSION", "future-model")
    current = service.get_current_evaluation(issue.id, as_of=threshold, session_factory=file_session_factory)
    assert current.reason == "model_changed"


def test_production_clock_crossing_rolls_back_first_pair(file_session_factory, tmp_path: Path, monkeypatch) -> None:
    """Beginner note: input CAS alone misses crossing midnight during calculation."""
    from backend.ipo import repository
    from backend.ipo.scoring import service

    issue = _scored_issue(file_session_factory, tmp_path)
    update_issue(issue.id, _issue_data(status=IpoStatus.OPEN, close_date=dt.date(2026, 7, 15)),
                 session_factory=file_session_factory)
    before = dt.datetime(2026, 7, 13, 23, 59, 59, tzinfo=dt.UTC)
    after = before + dt.timedelta(seconds=1)
    clocks = iter([before, after, after, after])
    monkeypatch.setattr(service, "_utc_now", lambda: next(clocks))
    outcome = rescore_issue(issue.id, session_factory=file_session_factory)
    assert repository.list_evaluations(issue.id, session_factory=file_session_factory) == [outcome.evaluation]
    assert service.get_current_evaluation(issue.id, as_of=after, session_factory=file_session_factory).fresh
    assert not service.get_current_evaluation(issue.id, as_of=before, session_factory=file_session_factory).fresh
    with pytest.raises(ValueError, match="timezone-aware"):
        rescore_issue(issue.id, as_of=before.replace(tzinfo=None), session_factory=file_session_factory)


@pytest.mark.parametrize("operation", [
    "issue", "document_insert", "document_values", "document_row", "document_delete",
    "subscription_insert", "subscription_update", "subscription_delete",
])
def test_storage_writers_invalidate_and_roll_back_atomically(file_session_factory, tmp_path: Path, operation) -> None:
    """Beginner note: facade-only invalidation missed ingestion and rollback paths."""
    from backend.storage import ipo_repository as storage

    issue = _scored_issue(file_session_factory, tmp_path)
    with file_session_factory() as session:
        document = storage.insert_ipo_document(session, issue.id, {
            "document_type": "drhp", "document_url": "https://www.sebi.gov.in/delete-me",
            "source_confidence": "high",
        })
        document_id = document.id
        subscription_id = storage.insert_ipo_subscription(session, issue.id, {
            "captured_at": _AS_OF, "qib_multiple": Decimal("20"), "source_confidence": "high",
        }).id
        before = _input_revision(session, issue.id)

    def mutate(session):
        """Exercise production SQL writers, including ingestion's direct helper."""
        if operation == "issue":
            storage.update_ipo_issue_row(session, issue.id, {"price_band_high": Decimal("300")})
        elif operation == "document_insert":
            storage.insert_ipo_document(session, issue.id, {"document_type": "drhp",
                "document_url": "https://www.sebi.gov.in/new-source", "source_confidence": "high"})
        elif operation == "document_values":
            existing_document = storage.get_ipo_document(session, issue.id, document_id)
            assert existing_document is not None
            storage.update_ipo_document_values(
                session, existing_document, {"document_url": "https://www.sebi.gov.in/revised-source"}
            )
        elif operation == "document_row":
            storage.update_ipo_document_row(session, issue.id, document_id, {"document_type": "rhp"})
        elif operation == "document_delete":
            storage.delete_ipo_document_row(session, issue.id, document_id)
        elif operation == "subscription_insert":
            storage.insert_ipo_subscription(session, issue.id, {"captured_at": _AS_OF + dt.timedelta(hours=1),
                "qib_multiple": Decimal("30"), "source_confidence": "high"})
        elif operation == "subscription_update":
            storage.update_ipo_subscription_row(session, issue.id, subscription_id, {"qib_multiple": Decimal("30")})
        else:
            storage.delete_ipo_subscription_row(session, issue.id, subscription_id)

    with pytest.raises(RuntimeError, match="abort"), file_session_factory() as session:
        mutate(session)
        assert _input_revision(session, issue.id) > before
        raise RuntimeError("abort")
    with file_session_factory() as session:
        assert _input_revision(session, issue.id) == before
        stored_issue = storage.get_ipo_issue(session, issue.id)
        stored_document = storage.get_ipo_document(session, issue.id, document_id)
        stored_subscription = storage.get_ipo_subscription(session, issue.id, subscription_id)
        assert stored_issue is not None and stored_issue.price_band_high == Decimal("242")
        assert stored_document is not None and stored_document.document_type == "drhp"
        assert stored_subscription is not None and stored_subscription.qib_multiple == Decimal("20")
        mutate(session)
    with file_session_factory() as session:
        assert _input_revision(session, issue.id) > before


def test_manual_and_enrichment_writers_invalidate_but_cache_does_not(file_session_factory, tmp_path: Path) -> None:
    """Beginner note: every approved profile and real last-seen refresh needs verification."""
    from backend.ipo import repository
    from backend.ipo.scoring import service
    from backend.storage import ipo_repository as storage

    issue = _scored_issue(file_session_factory, tmp_path)
    document = repository.list_documents(issue.id, session_factory=file_session_factory)[0]
    first = service.rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    before = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    with file_session_factory() as session:
        stored_document = storage.get_ipo_document(session, issue.id, document.id)
        assert stored_document is not None
        storage.update_ipo_document_values(session, stored_document,
                                           {"downloaded_at": _AS_OF + dt.timedelta(days=100)})
    assert service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh
    submit_manual_extraction(issue.id, _profile_data(document.id), entered_by_email="admin@example.com",
                             data_dir=tmp_path, session_factory=file_session_factory)
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert not current.fresh
    assert current.snapshot.state.input_revision > before.snapshot.state.input_revision
    assert rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory).evaluation == first.evaluation
    signal = IpoEnrichmentSignalData(signal_type=IpoEnrichmentSignalType.GMP, captured_at=_AS_OF,
        query_text="Example Ltd IPO GMP", payload=({"title": "GMP report"},), parsed_value=Decimal("25"),
        quarantined=False, confidence=Confidence.LOW, source_policy="serpapi-low-confidence-v2")
    record_enrichment_signals(issue.id, [signal], session_factory=file_session_factory)
    rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    record_enrichment_signals(issue.id, [dataclasses.replace(signal, captured_at=_AS_OF + dt.timedelta(hours=1))],
                              session_factory=file_session_factory)
    assert not service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory).fresh
    with file_session_factory() as session:
        existing = storage.list_ipo_enrichment_signal_rows(session, issue.id)[0]
        values = {column.name: getattr(existing, column.name) for column in existing.__table__.columns
                  if column.name not in {"id", "issue_id"}}
        values["semantic_hash"] = "a" * 64
        values["captured_at"] = _AS_OF + dt.timedelta(hours=2)
        before_revision = _input_revision(session, issue.id)
        storage.insert_ipo_enrichment_signals(session, issue.id, [values])
        assert _input_revision(session, issue.id) > before_revision


def test_current_pointer_ownership_and_score_deletion_fail_closed(file_session_factory, tmp_path: Path) -> None:
    """Beginner note: the score FK alone cannot prove that a pointer belongs to its issue."""
    from backend.ipo import repository
    from backend.ipo.scoring import service
    from backend.storage.models import IpoScoringState

    issue = _scored_issue(file_session_factory, tmp_path)
    other = create_issue(_issue_data(company_name="Other Ltd"), session_factory=file_session_factory)
    result = rescore_issue(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert result.evaluation is not None
    with file_session_factory() as session:
        state = session.get(IpoScoringState, other.id)
        state.current_score_id = result.evaluation.score_id
        state.evaluated_revision = state.input_revision
        state.last_verified_at = _AS_OF
    current = service.get_current_evaluation(other.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert not current.fresh and current.evaluation is None
    assert repository.delete_evaluation(issue.id, result.evaluation.score_id, session_factory=file_session_factory)
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=file_session_factory)
    assert not current.fresh and current.snapshot.state.current_score_id is None
    repository.delete_issue(other.id, session_factory=file_session_factory)
    with file_session_factory() as session:
        assert session.get(IpoScoringState, other.id) is None


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


def test_fingerprint_excludes_volatile_database_row_ids(
    file_session_factory, tmp_path: Path
) -> None:
    """Equivalent evidence hashes identically even when persistence ids differ."""
    issue = _scored_issue(file_session_factory, tmp_path)
    create_subscription(
        issue.id,
        IpoSubscriptionData(
            captured_at=_AS_OF,
            qib_multiple=Decimal("22"),
            source_confidence=Confidence.HIGH,
        ),
        session_factory=file_session_factory,
    )
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
                source_policy="serpapi-low-confidence-v2",
            )
        ],
        session_factory=file_session_factory,
    )
    original = load_ipo_factor_inputs_snapshot(
        issue.id,
        as_of=_AS_OF,
        session_factory=file_session_factory,
    )
    assert original.profile is not None
    assert original.ratios is not None
    assert original.subscription is not None
    assert original.enrichment

    renumbered = dataclasses.replace(
        original,
        issue=dataclasses.replace(original.issue, id=999),
        profile=dataclasses.replace(
            original.profile,
            id=998,
            issue_id=999,
            source_document_id=997,
        ),
        ratios=dataclasses.replace(
            original.ratios,
            extraction_id=998,
            issue_id=999,
        ),
        subscription=dataclasses.replace(
            original.subscription,
            id=996,
            issue_id=999,
        ),
        enrichment=tuple(
            dataclasses.replace(signal, id=995 - index, issue_id=999)
            for index, signal in enumerate(original.enrichment)
        ),
    )

    assert compute_inputs_fingerprint(renumbered) == compute_inputs_fingerprint(
        original
    )


def test_enrichment_freshness_refresh_does_not_duplicate_evaluation(
    file_session_factory, tmp_path: Path
) -> None:
    """Re-seeing identical still-fresh web evidence refreshes, but does not rescore."""
    issue = _scored_issue(file_session_factory, tmp_path)
    signal = IpoEnrichmentSignalData(
        signal_type=IpoEnrichmentSignalType.GMP,
        captured_at=_AS_OF,
        query_text="Example Ltd IPO GMP grey market premium",
        payload=({"title": "GMP report"},),
        parsed_value=Decimal("25"),
        quarantined=False,
        confidence=Confidence.LOW,
        source_policy="serpapi-low-confidence-v2",
    )
    record_enrichment_signals(
        issue.id,
        [signal],
        session_factory=file_session_factory,
    )
    first = rescore_issue(
        issue.id,
        as_of=_AS_OF,
        session_factory=file_session_factory,
    )
    assert first.status == "evaluated"

    refreshed_at = _AS_OF + dt.timedelta(hours=2)
    record_enrichment_signals(
        issue.id,
        [dataclasses.replace(signal, captured_at=refreshed_at)],
        session_factory=file_session_factory,
    )
    second = rescore_issue(
        issue.id,
        as_of=refreshed_at,
        session_factory=file_session_factory,
    )

    assert second.status == "skipped_unchanged"
    assert second.evaluation is not None
    assert first.evaluation is not None
    assert second.evaluation.score_id == first.evaluation.score_id
