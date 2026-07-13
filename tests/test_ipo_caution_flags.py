"""IPO-006 hard caution flag tests.

Beginner note:
Every flag has three possible outcomes, and the third one is the point of the
design: a rule whose evidence is absent reports ``not_evaluable`` instead of
quietly passing. These tests pin each rule's trigger condition, its clean
outcome, and its honest "cannot tell" outcome.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

from backend.ipo.financials.ratio_engine import (
    IpoPerShareReconciliation,
    IpoRatioAnalysis,
    IpoRatioName,
    IpoRatioReceipt,
    IpoRatioStatus,
)
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionRecord,
    IpoManualPeriodData,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    Confidence,
    IpoCautionFlagStatus,
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
    IpoIssueRecord,
    IpoIssueType,
    IpoStatus,
    IpoSubscriptionRecord,
)
from backend.ipo.scoring.caution_flags import (
    CAUTION_FLAG_ORDER,
    CAUTION_FLAGS_VERSION,
    FLAG_ENTIRELY_OFS_WEAK_GROWTH,
    FLAG_HIGH_DEBT_NO_REDUCTION_USE,
    FLAG_LITIGATION_RED_FLAG,
    FLAG_LOSS_MAKING_NO_PATH,
    FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
    FLAG_VERY_EXPENSIVE_VALUATION,
    FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE,
    evaluate_caution_flags,
)
from backend.ipo.scoring.factor_derivation import IpoFactorInputs

_AS_OF = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)
_SHA = "a" * 64


def _issue(**overrides: Any) -> IpoIssueRecord:
    """Build the reusable detached issue fixture used by the scenarios below."""
    values: dict[str, Any] = {
        "id": 1,
        "company_name": "Example Ltd",
        "issue_type": IpoIssueType.MAINBOARD,
        "status": IpoStatus.RHP_FILED,
        "source_confidence": Confidence.HIGH,
        "open_date": dt.date(2026, 7, 10),
        "close_date": dt.date(2026, 7, 15),
        "price_band_low": Decimal("95"),
        "price_band_high": Decimal("100"),
        "lot_size": 150,
        "fresh_issue_amount": Decimal("3000000000"),
        "ofs_amount": Decimal("1000000000"),
        "source_url": "https://www.sebi.gov.in/filings/example",
        "sebi_company_key": "example",
        "created_at": _AS_OF,
        "updated_at": _AS_OF,
    }
    values.update(overrides)
    return IpoIssueRecord(**values)


def _period(year: int, *, revenue: str, ebitda: str, pat: str) -> IpoManualPeriodData:
    """Build one sourced annual period; pages are constant test provenance."""
    return IpoManualPeriodData(
        period_end=dt.date(year, 3, 31),
        revenue=Decimal(revenue),
        revenue_page=10,
        ebitda=Decimal(ebitda),
        ebitda_page=10,
        pat=Decimal(pat),
        pat_page=10,
        profit_before_tax=Decimal(pat),
        profit_before_tax_page=10,
        finance_cost=Decimal("5"),
        finance_cost_page=10,
    )


def _profile(**overrides: Any) -> IpoManualExtractionRecord:
    """Build a complete detached manual-extraction fixture in crore INR."""
    values: dict[str, Any] = {
        "id": 7,
        "issue_id": 1,
        "source_document_id": 3,
        "source_document_url": "https://www.sebi.gov.in/filings/example-rhp",
        "source_record_hash": None,
        "source_content_sha256": _SHA,
        "financial_amount_unit": IpoAmountUnit.CRORE_INR,
        "issue_amount_unit": IpoAmountUnit.CRORE_INR,
        "equity_share_unit": IpoShareUnit.LAKH_SHARES,
        "periods": (
            _period(2024, revenue="100", ebitda="25", pat="12"),
            _period(2025, revenue="120", ebitda="30", pat="15"),
            _period(2026, revenue="150", ebitda="38", pat="20"),
        ),
        "net_worth": Decimal("160"),
        "net_worth_page": 11,
        "total_debt": Decimal("40"),
        "total_debt_page": 11,
        "cash": Decimal("20"),
        "cash_page": 11,
        "cash_flow_from_operations": Decimal("18"),
        "cash_flow_from_operations_page": 11,
        "equity_shares": Decimal("100"),
        "equity_shares_page": 12,
        "eps": Decimal("20"),
        "eps_page": 12,
        "nav_book_value": Decimal("160"),
        "nav_book_value_page": 12,
        "objects_of_issue": "Funding working capital and general corporate purposes",
        "objects_of_issue_page": 13,
        "fresh_issue_amount": Decimal("300"),
        "fresh_issue_amount_page": 13,
        "ofs_amount": Decimal("100"),
        "ofs_amount_page": 13,
        "promoter_holding_pre_issue": Decimal("70"),
        "promoter_holding_pre_issue_page": 14,
        "promoter_holding_post_issue": Decimal("55"),
        "promoter_holding_post_issue_page": 14,
        "peers": (
            IpoPeerValuationData(
                company_name="Peer One Ltd", source_page=15, metrics={"pe": Decimal("25")}
            ),
        ),
        "entered_by_email": "admin@example.com",
        "submitted_at": _AS_OF,
    }
    values.update(overrides)
    return IpoManualExtractionRecord(**values)


def _receipt(
    name: IpoRatioName,
    value: str | None,
    status: IpoRatioStatus = IpoRatioStatus.COMPUTED,
    explanation: str = "",
) -> IpoRatioReceipt:
    """Build one hand-crafted ratio receipt for a targeted flag scenario."""
    return IpoRatioReceipt(
        name=name,
        value=Decimal(value) if value is not None else None,
        status=status,
        formula="test formula",
        explanation=explanation,
    )


def _ratios(*receipts: IpoRatioReceipt, price_band_high: str | None = "100") -> IpoRatioAnalysis:
    """Assemble a partial ratio snapshot; absent names read as missing inputs."""
    reconciliation = IpoPerShareReconciliation(
        computed=Decimal("20"), reported=Decimal("20"), difference=Decimal("0"), materially_different=False
    )
    return IpoRatioAnalysis(
        formula_version="ipo-ratio-v1",
        extraction_id=7,
        issue_id=1,
        source_content_sha256=_SHA,
        price_band_high=Decimal(price_band_high) if price_band_high is not None else None,
        issue_updated_at=_AS_OF,
        ratios={receipt.name: receipt for receipt in receipts},
        eps_reconciliation=reconciliation,
        book_value_reconciliation=reconciliation,
    )


def _subscription(qib: str | None) -> IpoSubscriptionRecord:
    """Build one detached demand snapshot with only the QIB column populated."""
    return IpoSubscriptionRecord(
        id=1,
        issue_id=1,
        captured_at=_AS_OF,
        qib_multiple=Decimal(qib) if qib is not None else None,
        nii_multiple=None,
        retail_multiple=None,
        total_multiple=None,
        source_url=None,
        source_confidence=Confidence.HIGH,
        created_at=_AS_OF,
    )


def _signal(
    signal_type: IpoEnrichmentSignalType,
    *,
    matched_keywords: tuple[str, ...] = (),
    quarantined: bool = False,
) -> IpoEnrichmentSignalRecord:
    """Build one detached enrichment signal carrying only keyword metadata."""
    return IpoEnrichmentSignalRecord(
        id=1,
        issue_id=1,
        signal_type=signal_type,
        captured_at=_AS_OF,
        query_text="Example Ltd IPO litigation",
        payload=({"title": "result", "matched_keywords": list(matched_keywords)},),
        parsed_value=None,
        quarantined=quarantined,
        confidence=Confidence.LOW,
        source_policy="serpapi-low-confidence-v1",
        created_at=_AS_OF,
    )


def _inputs(**overrides: Any) -> IpoFactorInputs:
    """Build complete factor inputs; scenarios override one piece of evidence."""
    values: dict[str, Any] = {
        "issue": _issue(),
        "profile": _profile(),
        "ratios": _ratios(
            _receipt(IpoRatioName.REVENUE_CAGR, "22.47"),
            _receipt(IpoRatioName.PRICE_TO_EARNINGS, "20"),
            _receipt(IpoRatioName.DEBT_TO_EQUITY, "0.25"),
        ),
        "subscription": _subscription("12"),
        "as_of": _AS_OF,
        "enrichment": (),
    }
    values.update(overrides)
    return IpoFactorInputs(**values)


def _flag(report: Any, name: str) -> Any:
    """Return one named flag from a report regardless of catalog position."""
    return next(flag for flag in report.flags if flag.name == name)


def test_report_always_contains_every_flag_in_catalog_order() -> None:
    """Pin the report shape: all seven flags, fixed order, stamped version."""
    report = evaluate_caution_flags(_inputs())

    assert report.version == CAUTION_FLAGS_VERSION
    assert tuple(flag.name for flag in report.flags) == CAUTION_FLAG_ORDER
    assert len(report.flags) == 7
    assert all(flag.evidence for flag in report.flags)


def test_entirely_ofs_with_weak_growth_triggers() -> None:
    """A pure offer-for-sale plus weak revenue growth is a hard warning."""
    inputs = _inputs(
        profile=_profile(fresh_issue_amount=Decimal("0"), ofs_amount=Decimal("400")),
        ratios=_ratios(_receipt(IpoRatioName.REVENUE_CAGR, "3.10")),
    )

    flag = _flag(evaluate_caution_flags(inputs), FLAG_ENTIRELY_OFS_WEAK_GROWTH)
    assert flag.status is IpoCautionFlagStatus.TRIGGERED

    healthy = _flag(evaluate_caution_flags(_inputs()), FLAG_ENTIRELY_OFS_WEAK_GROWTH)
    assert healthy.status is IpoCautionFlagStatus.NOT_TRIGGERED

    unknown = _flag(
        evaluate_caution_flags(_inputs(profile=None, ratios=None)),
        FLAG_ENTIRELY_OFS_WEAK_GROWTH,
    )
    assert unknown.status is IpoCautionFlagStatus.NOT_EVALUABLE


def test_entirely_ofs_with_undefined_growth_also_triggers() -> None:
    """An undefined CAGR (loss or zero base) cannot rescue a pure OFS issue."""
    inputs = _inputs(
        profile=_profile(fresh_issue_amount=Decimal("0"), ofs_amount=Decimal("400")),
        ratios=_ratios(
            _receipt(
                IpoRatioName.REVENUE_CAGR,
                None,
                IpoRatioStatus.UNDEFINED,
                explanation="FY1 revenue is zero.",
            )
        ),
    )

    flag = _flag(evaluate_caution_flags(inputs), FLAG_ENTIRELY_OFS_WEAK_GROWTH)
    assert flag.status is IpoCautionFlagStatus.TRIGGERED


def test_very_expensive_valuation_uses_peer_pe_median() -> None:
    """A P/E premium above 1.5x the peer median triggers; cheaper does not."""
    expensive = _inputs(ratios=_ratios(_receipt(IpoRatioName.PRICE_TO_EARNINGS, "40")))
    flag = _flag(evaluate_caution_flags(expensive), FLAG_VERY_EXPENSIVE_VALUATION)
    assert flag.status is IpoCautionFlagStatus.TRIGGERED
    assert "1.6" in flag.evidence  # 40 / 25 = 1.6x premium

    fair = _inputs(ratios=_ratios(_receipt(IpoRatioName.PRICE_TO_EARNINGS, "30")))
    assert (
        _flag(evaluate_caution_flags(fair), FLAG_VERY_EXPENSIVE_VALUATION).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    no_pe = _inputs(ratios=_ratios())
    assert (
        _flag(evaluate_caution_flags(no_pe), FLAG_VERY_EXPENSIVE_VALUATION).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_weak_qib_demand_flag_respects_the_near_close_window() -> None:
    """The demand flag only judges once the book is about to close or closed."""
    near_close = _inputs(
        issue=_issue(status=IpoStatus.OPEN, close_date=dt.date(2026, 7, 14)),
        subscription=_subscription("0.60"),
    )
    assert (
        _flag(evaluate_caution_flags(near_close), FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE).status
        is IpoCautionFlagStatus.TRIGGERED
    )

    missing_snapshot = _inputs(
        issue=_issue(status=IpoStatus.CLOSED, close_date=dt.date(2026, 7, 12)),
        subscription=None,
    )
    assert (
        _flag(
            evaluate_caution_flags(missing_snapshot), FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE
        ).status
        is IpoCautionFlagStatus.TRIGGERED
    )

    strong = _inputs(
        issue=_issue(status=IpoStatus.CLOSED, close_date=dt.date(2026, 7, 12)),
        subscription=_subscription("45"),
    )
    assert (
        _flag(evaluate_caution_flags(strong), FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    too_early = _inputs(
        issue=_issue(status=IpoStatus.OPEN, close_date=dt.date(2026, 7, 20)),
        subscription=None,
    )
    assert (
        _flag(evaluate_caution_flags(too_early), FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )

    pre_listing = _inputs(issue=_issue(status=IpoStatus.DRHP_FILED, close_date=None))
    assert (
        _flag(evaluate_caution_flags(pre_listing), FLAG_WEAK_QIB_DEMAND_NEAR_CLOSE).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_negative_operating_cash_flow_despite_profits_triggers() -> None:
    """Reported profit with negative CFO is the classic earnings-quality flag."""
    inputs = _inputs(profile=_profile(cash_flow_from_operations=Decimal("-5")))
    assert (
        _flag(
            evaluate_caution_flags(inputs), FLAG_NEGATIVE_CFO_DESPITE_PROFITS
        ).status
        is IpoCautionFlagStatus.TRIGGERED
    )

    assert (
        _flag(
            evaluate_caution_flags(_inputs()), FLAG_NEGATIVE_CFO_DESPITE_PROFITS
        ).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    assert (
        _flag(
            evaluate_caution_flags(_inputs(profile=None)),
            FLAG_NEGATIVE_CFO_DESPITE_PROFITS,
        ).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_high_debt_without_debt_reduction_use_reads_objects_of_issue() -> None:
    """High leverage triggers unless the objects name debt repayment."""
    leveraged = _inputs(ratios=_ratios(_receipt(IpoRatioName.DEBT_TO_EQUITY, "2.10")))
    assert (
        _flag(evaluate_caution_flags(leveraged), FLAG_HIGH_DEBT_NO_REDUCTION_USE).status
        is IpoCautionFlagStatus.TRIGGERED
    )

    repaying = _inputs(
        profile=_profile(
            objects_of_issue="Repayment of certain outstanding borrowings and general corporate purposes"
        ),
        ratios=_ratios(_receipt(IpoRatioName.DEBT_TO_EQUITY, "2.10")),
    )
    assert (
        _flag(evaluate_caution_flags(repaying), FLAG_HIGH_DEBT_NO_REDUCTION_USE).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    modest = _inputs(ratios=_ratios(_receipt(IpoRatioName.DEBT_TO_EQUITY, "0.40")))
    assert (
        _flag(evaluate_caution_flags(modest), FLAG_HIGH_DEBT_NO_REDUCTION_USE).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    unknown = _inputs(ratios=_ratios())
    assert (
        _flag(evaluate_caution_flags(unknown), FLAG_HIGH_DEBT_NO_REDUCTION_USE).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_litigation_flag_reads_only_clean_keyword_matched_signals() -> None:
    """Keyword-matched web signals trigger; quarantined text never does."""
    matched = _inputs(
        enrichment=(
            _signal(
                IpoEnrichmentSignalType.LITIGATION_RED_FLAG,
                matched_keywords=("litigation", "sebi order"),
            ),
        )
    )
    flag = _flag(evaluate_caution_flags(matched), FLAG_LITIGATION_RED_FLAG)
    assert flag.status is IpoCautionFlagStatus.TRIGGERED
    assert "litigation" in flag.evidence

    clean = _inputs(
        enrichment=(_signal(IpoEnrichmentSignalType.LITIGATION_RED_FLAG),)
    )
    assert (
        _flag(evaluate_caution_flags(clean), FLAG_LITIGATION_RED_FLAG).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    quarantined = _inputs(
        enrichment=(
            _signal(
                IpoEnrichmentSignalType.LITIGATION_RED_FLAG,
                matched_keywords=("fraud",),
                quarantined=True,
            ),
        )
    )
    assert (
        _flag(evaluate_caution_flags(quarantined), FLAG_LITIGATION_RED_FLAG).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    no_enrichment = _inputs(enrichment=())
    assert (
        _flag(evaluate_caution_flags(no_enrichment), FLAG_LITIGATION_RED_FLAG).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_loss_making_with_no_credible_path_reads_the_pat_trend() -> None:
    """A widening latest-year loss triggers; a narrowing loss does not."""
    worsening = _inputs(
        profile=_profile(
            periods=(
                _period(2024, revenue="100", ebitda="5", pat="-2"),
                _period(2025, revenue="120", ebitda="4", pat="-4"),
                _period(2026, revenue="150", ebitda="3", pat="-9"),
            )
        )
    )
    assert (
        _flag(evaluate_caution_flags(worsening), FLAG_LOSS_MAKING_NO_PATH).status
        is IpoCautionFlagStatus.TRIGGERED
    )

    narrowing = _inputs(
        profile=_profile(
            periods=(
                _period(2024, revenue="100", ebitda="5", pat="-9"),
                _period(2025, revenue="120", ebitda="4", pat="-4"),
                _period(2026, revenue="150", ebitda="3", pat="-2"),
            )
        )
    )
    assert (
        _flag(evaluate_caution_flags(narrowing), FLAG_LOSS_MAKING_NO_PATH).status
        is IpoCautionFlagStatus.NOT_TRIGGERED
    )

    profitable = _flag(evaluate_caution_flags(_inputs()), FLAG_LOSS_MAKING_NO_PATH)
    assert profitable.status is IpoCautionFlagStatus.NOT_TRIGGERED

    assert (
        _flag(evaluate_caution_flags(_inputs(profile=None)), FLAG_LOSS_MAKING_NO_PATH).status
        is IpoCautionFlagStatus.NOT_EVALUABLE
    )


def test_reports_are_deterministic_for_identical_inputs() -> None:
    """Two evaluations of the same evidence produce byte-identical reports."""
    first = evaluate_caution_flags(_inputs())
    second = evaluate_caution_flags(_inputs())

    assert first == second
