"""IPO-001/IPO-006 binary verdict and JSON-contract tests."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from backend.ipo.models import (
    Confidence,
    IpoCautionFlag,
    IpoCautionFlagReport,
    IpoCautionFlagStatus,
    IpoScoreResult,
    Recommendation,
)
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    INSUFFICIENT_VERIFIED_DATA,
    SKIP,
    build_recommendation,
)


def _report(*statuses: tuple[str, IpoCautionFlagStatus]) -> IpoCautionFlagReport:
    """Build a small caution-flag report fixture for verdict scenarios."""
    return IpoCautionFlagReport(
        version="ipo-006-flags-v1",
        flags=tuple(
            IpoCautionFlag(name=name, status=status, evidence=f"evidence for {name}")
            for name, status in statuses
        ),
    )


def _score_result(
    score: str,
    *,
    missing_data: tuple[str, ...] = (),
) -> IpoScoreResult:
    """Build the reusable score result fixture used by the scenarios below."""
    return IpoScoreResult(
        company_name="Example Ltd",
        score=Decimal(score),
        contributions={},
        reasons=("Strong revenue growth", "Reasonable valuation versus peers"),
        missing_data=missing_data,
        source_documents=("https://www.sebi.gov.in/example-rhp.pdf",),
    )


@pytest.mark.parametrize(
    ("score", "recommendation", "recommendation_type"),
    [
        ("100", Recommendation.RECOMMENDED, APPLY_AND_HOLD),
        ("80", Recommendation.RECOMMENDED, APPLY_AND_HOLD),
        ("79.99", Recommendation.RECOMMENDED, APPLY_FOR_LISTING_GAINS),
        ("65", Recommendation.RECOMMENDED, APPLY_FOR_LISTING_GAINS),
        ("64.99", Recommendation.NOT_RECOMMENDED, SKIP),
        ("0", Recommendation.NOT_RECOMMENDED, SKIP),
    ],
)
def test_verdict_uses_the_exact_pdf_score_bands(
    score: str,
    recommendation: Recommendation,
    recommendation_type: str,
) -> None:
    """Pin verdict uses the exact pdf score bands as an executable IPO regression contract."""
    result = build_recommendation(_score_result(score))

    assert result.recommendation is recommendation
    assert result.recommendation_type == recommendation_type


@pytest.mark.parametrize(
    "critical_factor",
    [
        "business_quality",
        "financial_growth",
        "return_ratios",
        "valuation",
        "promoter_quality",
    ],
)
def test_missing_critical_data_forces_a_fail_closed_verdict(critical_factor: str) -> None:
    """Pin missing critical data forces a fail closed verdict as an executable IPO regression contract.

    IPO-006 renames this branch's sub-label from ``Skip`` to the dedicated
    "Insufficient verified data" type so a data-gap rejection is
    distinguishable from a scored rejection in history and on the dashboard.
    """
    result = build_recommendation(_score_result("90", missing_data=(critical_factor,)))

    assert result.recommendation is Recommendation.NOT_RECOMMENDED
    assert result.recommendation_type == INSUFFICIENT_VERIFIED_DATA
    assert result.confidence is Confidence.LOW
    assert result.reasons[0].startswith("Missing critical data:")
    assert critical_factor.replace("_", " ") in result.reasons[0]


@pytest.mark.parametrize(
    ("missing_data", "confidence"),
    [
        ((), Confidence.HIGH),
        (("qib_subscription",), Confidence.MEDIUM),
        (("gmp_sentiment",), Confidence.MEDIUM),
        (("qib_subscription", "gmp_sentiment"), Confidence.LOW),
    ],
)
def test_confidence_reflects_factor_completeness(
    missing_data: tuple[str, ...], confidence: Confidence
) -> None:
    """Pin confidence reflects factor completeness as an executable IPO regression contract."""
    assert build_recommendation(
        _score_result("78", missing_data=missing_data)
    ).confidence is confidence


def test_json_contract_has_exact_keys_and_json_native_values() -> None:
    """Pin json contract has exact keys and json native values as an executable IPO regression contract."""
    payload = build_recommendation(_score_result("78")).to_dict()

    assert payload == {
        "company_name": "Example Ltd",
        "score": 78,
        "recommendation": "Recommended",
        "recommendation_type": "Apply primarily for listing gains",
        "confidence": "high",
        "reasons": ["Strong revenue growth", "Reasonable valuation versus peers"],
        "missing_data": [],
        "source_documents": ["https://www.sebi.gov.in/example-rhp.pdf"],
        "caution_flags": [],
    }
    assert json.loads(json.dumps(payload)) == payload


def test_triggered_caution_flag_forces_not_recommended_at_any_score() -> None:
    """A hard caution flag overrides even a near-perfect numeric score."""
    report = _report(
        ("negative_operating_cash_flow_despite_profits", IpoCautionFlagStatus.TRIGGERED),
        ("very_expensive_valuation", IpoCautionFlagStatus.NOT_TRIGGERED),
    )

    result = build_recommendation(_score_result("95"), caution_flags=report)

    assert result.recommendation is Recommendation.NOT_RECOMMENDED
    assert result.recommendation_type == SKIP
    assert result.reasons[0].startswith("Hard caution flag:")
    assert "negative_operating_cash_flow_despite_profits" in result.reasons[0]
    assert result.caution_flags == report.flags


def test_missing_critical_data_outranks_a_triggered_flag() -> None:
    """Insufficient data is the stronger sub-label; flag reasons still appear."""
    report = _report(("very_expensive_valuation", IpoCautionFlagStatus.TRIGGERED))

    result = build_recommendation(
        _score_result("90", missing_data=("valuation",)), caution_flags=report
    )

    assert result.recommendation is Recommendation.NOT_RECOMMENDED
    assert result.recommendation_type == INSUFFICIENT_VERIFIED_DATA
    assert any(reason.startswith("Hard caution flag:") for reason in result.reasons)


def test_untriggered_report_leaves_the_score_bands_untouched() -> None:
    """Not-triggered and not-evaluable flags never change the verdict."""
    report = _report(
        ("very_expensive_valuation", IpoCautionFlagStatus.NOT_TRIGGERED),
        ("litigation_or_auditor_red_flag", IpoCautionFlagStatus.NOT_EVALUABLE),
    )

    result = build_recommendation(_score_result("81"), caution_flags=report)

    assert result.recommendation is Recommendation.RECOMMENDED
    assert result.recommendation_type == APPLY_AND_HOLD
    assert result.caution_flags == report.flags


def test_to_dict_serializes_caution_flags() -> None:
    """The JSON contract carries the full flag report for auditability."""
    report = _report(("loss_making_no_credible_path", IpoCautionFlagStatus.TRIGGERED))

    payload = build_recommendation(_score_result("40"), caution_flags=report).to_dict()

    assert payload["caution_flags"] == [
        {
            "name": "loss_making_no_credible_path",
            "status": "triggered",
            "evidence": "evidence for loss_making_no_credible_path",
        }
    ]
    assert json.loads(json.dumps(payload)) == payload

