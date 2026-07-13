"""IPO-006 factor derivation tests.

Beginner note:
Factor derivation is the bridge the IPO-001 design deferred: it turns typed
ratio receipts and verified evidence into the seven 0-100 factor scores the
scorecard consumes. These tests pin every band boundary, the None-versus-zero
rule (missing evidence versus known-weak evidence), and the deterministic
reason strings that make each factor auditable.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pytest

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
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
    IpoIssueRecord,
    IpoIssueType,
    IpoStatus,
    IpoSubscriptionRecord,
)
from backend.ipo.scoring.factor_derivation import (
    FACTOR_MODEL_VERSION,
    GMP_SIGNAL_MAX_AGE_DAYS,
    IpoFactorInputs,
    derive_score_input,
)

_AS_OF = dt.datetime(2026, 7, 13, 12, 0, tzinfo=dt.UTC)
_SHA = "b" * 64


def _issue(**overrides: Any) -> IpoIssueRecord:
    """Build the reusable detached issue fixture used by the scenarios below."""
    values: dict[str, Any] = {
        "id": 1,
        "company_name": "Example Ltd",
        "issue_type": IpoIssueType.MAINBOARD,
        "status": IpoStatus.OPEN,
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
                company_name="Peer One Ltd",
                source_page=15,
                metrics={"pe": Decimal("20"), "ev_ebitda": Decimal("10")},
            ),
            IpoPeerValuationData(
                company_name="Peer Two Ltd",
                source_page=15,
                metrics={"pe": Decimal("30")},
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
    """Build one hand-crafted ratio receipt for a targeted factor scenario."""
    return IpoRatioReceipt(
        name=name,
        value=Decimal(value) if value is not None else None,
        status=status,
        formula="test formula",
        explanation=explanation,
    )


def _ratios(*receipts: IpoRatioReceipt) -> IpoRatioAnalysis:
    """Assemble a partial ratio snapshot; absent names read as missing inputs."""
    reconciliation = IpoPerShareReconciliation(
        computed=Decimal("20"),
        reported=Decimal("20"),
        difference=Decimal("0"),
        materially_different=False,
    )
    return IpoRatioAnalysis(
        formula_version="ipo-ratio-v1",
        extraction_id=7,
        issue_id=1,
        source_content_sha256=_SHA,
        price_band_high=Decimal("100"),
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


def _gmp_signal(
    parsed: str | None,
    *,
    signal_id: int = 1,
    age_days: int = 0,
    quarantined: bool = False,
) -> IpoEnrichmentSignalRecord:
    """Build one detached GMP observation captured ``age_days`` before as-of."""
    return IpoEnrichmentSignalRecord(
        id=signal_id,
        issue_id=1,
        signal_type=IpoEnrichmentSignalType.GMP,
        captured_at=_AS_OF - dt.timedelta(days=age_days),
        query_text="Example Ltd IPO GMP grey market premium",
        payload=({"title": "result"},),
        parsed_value=Decimal(parsed) if parsed is not None else None,
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
            _receipt(IpoRatioName.REVENUE_CAGR, "27.40"),
            _receipt(IpoRatioName.PAT_CAGR, "18.20"),
            _receipt(IpoRatioName.ROE, "18"),
            _receipt(IpoRatioName.PRICE_TO_EARNINGS, "20"),
            _receipt(IpoRatioName.EBITDA_MARGIN, "26"),
            _receipt(IpoRatioName.PAT_MARGIN, "12"),
            _receipt(IpoRatioName.CFO_TO_PAT, "0.90"),
        ),
        "subscription": _subscription("12"),
        "as_of": _AS_OF,
        "enrichment": (),
    }
    values.update(overrides)
    return IpoFactorInputs(**values)


def test_model_version_constant_is_stable() -> None:
    """Pin the version string so silent threshold edits fail loudly in review."""
    assert FACTOR_MODEL_VERSION == "ipo-006-factors-v1"


@pytest.mark.parametrize(
    ("cagr", "expected"),
    [
        ("25", "100.00"),
        ("24.99", "75.00"),
        ("15", "75.00"),
        ("14.99", "50.00"),
        ("8", "50.00"),
        ("7.99", "25.00"),
        ("0", "25.00"),
        ("-0.01", "0.00"),
    ],
)
def test_financial_growth_band_boundaries_are_half_open(cagr: str, expected: str) -> None:
    """Each growth band includes its lower bound and excludes its upper bound."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(IpoRatioName.REVENUE_CAGR, cagr),
            _receipt(IpoRatioName.PAT_CAGR, cagr),
        )
    )

    result = derive_score_input(inputs)
    assert result.financial_growth.score == Decimal(expected)


def test_financial_growth_averages_revenue_and_pat_subscores() -> None:
    """Revenue CAGR 27.40 banded 100 and PAT CAGR 18.20 banded 75 average to 87.50."""
    result = derive_score_input(_inputs())

    assert result.financial_growth.score == Decimal("87.50")
    assert result.financial_growth.reason is not None
    assert "ipo-ratio-v1" in result.financial_growth.reason
    assert "extraction #7" in result.financial_growth.reason


def test_undefined_pat_cagr_is_known_weak_not_missing() -> None:
    """A loss-base CAGR earns a zero sub-score instead of hiding as missing."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(IpoRatioName.REVENUE_CAGR, "27.40"),
            _receipt(
                IpoRatioName.PAT_CAGR,
                None,
                IpoRatioStatus.UNDEFINED,
                explanation="FY1 PAT is not positive.",
            ),
        )
    )

    result = derive_score_input(inputs)
    assert result.financial_growth.score == Decimal("50.00")
    assert result.financial_growth.reason is not None
    assert "FY1 PAT is not positive." in result.financial_growth.reason


def test_missing_core_ratio_receipt_makes_the_factor_missing() -> None:
    """Absent evidence must surface as None so the verdict can fail closed."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(
                IpoRatioName.REVENUE_CAGR,
                None,
                IpoRatioStatus.MISSING_INPUTS,
            ),
            _receipt(IpoRatioName.PAT_CAGR, "18.20"),
        )
    )

    result = derive_score_input(inputs)
    assert result.financial_growth.score is None
    assert result.financial_growth.reason is not None


def test_no_ratio_snapshot_leaves_every_document_factor_missing() -> None:
    """Without ratios, only demand and sentiment factors can still be judged."""
    result = derive_score_input(_inputs(ratios=None))

    assert result.financial_growth.score is None
    assert result.return_ratios.score is None
    assert result.valuation.score is None
    assert result.business_quality.score is None
    assert result.qib_subscription.score is not None


def test_no_profile_leaves_promoter_factor_missing_and_documents_empty() -> None:
    """The promoter factor and source documents both need a verified revision."""
    result = derive_score_input(_inputs(profile=None, ratios=None))

    assert result.promoter_quality.score is None
    assert result.source_documents == ()


def test_return_ratios_use_roe_core_and_roce_optional() -> None:
    """ROE alone scores its band; a computed ROCE is averaged in when present."""
    roe_only = derive_score_input(_inputs())
    assert roe_only.return_ratios.score == Decimal("75.00")

    with_roce = derive_score_input(
        _inputs(
            ratios=_ratios(
                _receipt(IpoRatioName.REVENUE_CAGR, "27.40"),
                _receipt(IpoRatioName.PAT_CAGR, "18.20"),
                _receipt(IpoRatioName.ROE, "18"),
                _receipt(IpoRatioName.ROCE, "22"),
            )
        )
    )
    assert with_roce.return_ratios.score == Decimal("87.50")


def test_valuation_scores_the_pe_premium_against_the_peer_median() -> None:
    """P/E 20 against a 25 peer median is a 0.80 premium banded at 80."""
    result = derive_score_input(_inputs())

    assert result.valuation.score == Decimal("80.00")
    assert result.valuation.reason is not None
    assert "peer" in result.valuation.reason.lower()


def test_valuation_negative_earnings_pe_is_known_weak() -> None:
    """An undefined P/E from negative earnings is bad evidence, not missing."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(
                IpoRatioName.PRICE_TO_EARNINGS,
                None,
                IpoRatioStatus.UNDEFINED,
                explanation="Computed EPS is not positive.",
            )
        )
    )

    result = derive_score_input(inputs)
    assert result.valuation.score == Decimal("0.00")


def test_valuation_without_price_band_or_peer_pe_is_missing() -> None:
    """No issue price or no peer P/E leaves valuation honestly unscored."""
    no_pe_receipt = derive_score_input(_inputs(ratios=_ratios()))
    assert no_pe_receipt.valuation.score is None

    no_peer_pe = derive_score_input(
        _inputs(
            profile=_profile(
                peers=(
                    IpoPeerValuationData(
                        company_name="Peer One Ltd",
                        source_page=15,
                        metrics={"ronw": Decimal("14")},
                    ),
                )
            )
        )
    )
    assert no_peer_pe.valuation.score is None


def test_valuation_averages_ev_to_ebitda_premium_when_available() -> None:
    """A computed EV/EBITDA with a peer median joins the P/E premium average."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(IpoRatioName.PRICE_TO_EARNINGS, "20"),
            _receipt(IpoRatioName.EV_TO_EBITDA, "8"),
        )
    )

    result = derive_score_input(inputs)
    # P/E premium 20/25 = 0.80 -> 80; EV/EBITDA premium 8/10 = 0.80 -> 80.
    assert result.valuation.score == Decimal("80.00")


def test_business_quality_averages_core_margins_and_cash_conversion() -> None:
    """Margins 26/12 and CFO conversion 0.90 average to 83.33 without coverage."""
    result = derive_score_input(_inputs())

    assert result.business_quality.score == Decimal("83.33")


def test_business_quality_includes_interest_coverage_when_computed() -> None:
    """A computed interest coverage joins the three core sub-scores."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(IpoRatioName.EBITDA_MARGIN, "26"),
            _receipt(IpoRatioName.PAT_MARGIN, "12"),
            _receipt(IpoRatioName.CFO_TO_PAT, "0.90"),
            _receipt(IpoRatioName.INTEREST_COVERAGE, "6"),
        )
    )

    result = derive_score_input(inputs)
    assert result.business_quality.score == Decimal("81.25")


def test_business_quality_undefined_cash_conversion_is_known_weak() -> None:
    """CFO/PAT undefined by a loss year scores zero rather than hiding."""
    inputs = _inputs(
        ratios=_ratios(
            _receipt(IpoRatioName.EBITDA_MARGIN, "26"),
            _receipt(IpoRatioName.PAT_MARGIN, "12"),
            _receipt(
                IpoRatioName.CFO_TO_PAT,
                None,
                IpoRatioStatus.UNDEFINED,
                explanation="FY3 PAT is not positive.",
            ),
        )
    )

    result = derive_score_input(inputs)
    assert result.business_quality.score == Decimal("58.33")


def test_promoter_quality_averages_holding_and_ofs_share() -> None:
    """Post-issue holding 55% (80) and OFS share 0.25 (60) average to 70."""
    result = derive_score_input(_inputs())

    assert result.promoter_quality.score == Decimal("70.00")
    assert result.promoter_quality.reason is not None
    assert "manual extraction #7" in result.promoter_quality.reason


def test_promoter_quality_pure_ofs_earns_the_bottom_ofs_band() -> None:
    """A 100% offer-for-sale issue scores zero on the OFS sub-input."""
    inputs = _inputs(
        profile=_profile(fresh_issue_amount=Decimal("0"), ofs_amount=Decimal("400"))
    )

    result = derive_score_input(inputs)
    # Holding 55 -> 80; pure OFS -> 0; mean 40.
    assert result.promoter_quality.score == Decimal("40.00")


@pytest.mark.parametrize(
    ("qib", "expected"),
    [
        ("50", "100.00"),
        ("49.99", "85.00"),
        ("20", "85.00"),
        ("10", "70.00"),
        ("3", "55.00"),
        ("1", "35.00"),
        ("0.99", "0.00"),
    ],
)
def test_qib_subscription_band_boundaries(qib: str, expected: str) -> None:
    """QIB demand bands include their lower bound and exclude their upper."""
    result = derive_score_input(_inputs(subscription=_subscription(qib)))

    assert result.qib_subscription.score == Decimal(expected)


def test_qib_subscription_without_snapshot_or_breakdown_is_missing() -> None:
    """No snapshot, or a snapshot without the QIB column, stays missing."""
    assert derive_score_input(_inputs(subscription=None)).qib_subscription.score is None
    assert (
        derive_score_input(_inputs(subscription=_subscription(None))).qib_subscription.score
        is None
    )


def test_gmp_sentiment_uses_the_median_of_recent_clean_signals() -> None:
    """The factor reads the median parsed GMP across fresh, unquarantined rows."""
    inputs = _inputs(
        enrichment=(
            _gmp_signal("10", signal_id=1),
            _gmp_signal("20", signal_id=2, age_days=1),
            _gmp_signal("30", signal_id=3, age_days=2),
        )
    )

    result = derive_score_input(inputs)
    assert result.gmp_sentiment.score == Decimal("75.00")
    assert result.gmp_sentiment.reason is not None
    assert "low-confidence web source" in result.gmp_sentiment.reason


def test_gmp_sentiment_ignores_stale_quarantined_and_unparsed_signals() -> None:
    """Old, quarantined, or unparseable observations never fabricate a score."""
    inputs = _inputs(
        enrichment=(
            _gmp_signal("25", signal_id=1, age_days=GMP_SIGNAL_MAX_AGE_DAYS + 1),
            _gmp_signal("25", signal_id=2, quarantined=True),
            _gmp_signal(None, signal_id=3),
        )
    )

    result = derive_score_input(inputs)
    assert result.gmp_sentiment.score is None


def test_gmp_sentiment_negative_premium_is_known_weak() -> None:
    """A grey-market discount is negative evidence and scores zero."""
    inputs = _inputs(enrichment=(_gmp_signal("-5"),))

    result = derive_score_input(inputs)
    assert result.gmp_sentiment.score == Decimal("0.00")


def test_score_input_carries_company_name_and_source_documents() -> None:
    """The derived input names the issue and cites the verified revision URL."""
    result = derive_score_input(_inputs())

    assert result.company_name == "Example Ltd"
    assert result.source_documents == ("https://www.sebi.gov.in/filings/example-rhp",)


def test_every_factor_carries_a_reason_even_when_missing() -> None:
    """Missing factors still explain themselves for the dashboard queue."""
    result = derive_score_input(_inputs(profile=None, ratios=None, subscription=None))

    for factor_name in (
        "business_quality",
        "financial_growth",
        "return_ratios",
        "valuation",
        "qib_subscription",
        "promoter_quality",
        "gmp_sentiment",
    ):
        assessment = getattr(result, factor_name)
        assert assessment.score is None
        assert assessment.reason


def test_derivation_is_deterministic_for_identical_inputs() -> None:
    """Two derivations of the same evidence produce identical score inputs."""
    assert derive_score_input(_inputs()) == derive_score_input(_inputs())
