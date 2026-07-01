"""IPO-001 binary verdict and JSON-contract tests."""

from __future__ import annotations

import json
from decimal import Decimal

import pytest

from backend.ipo.models import Confidence, IpoScoreResult, Recommendation
from backend.ipo.verdict import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    SKIP,
    build_recommendation,
)


def _score_result(
    score: str,
    *,
    missing_data: tuple[str, ...] = (),
) -> IpoScoreResult:
    """Provide the score result step used by the IPO workflow."""
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
    """Verify that verdict uses the exact pdf score bands."""
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
    """Verify that missing critical data forces a fail closed verdict."""
    result = build_recommendation(_score_result("90", missing_data=(critical_factor,)))

    assert result.recommendation is Recommendation.NOT_RECOMMENDED
    assert result.recommendation_type == SKIP
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
    """Verify that confidence reflects factor completeness."""
    assert build_recommendation(
        _score_result("78", missing_data=missing_data)
    ).confidence is confidence


def test_json_contract_has_exact_keys_and_json_native_values() -> None:
    """Verify that json contract has exact keys and json native values."""
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
    }
    assert json.loads(json.dumps(payload)) == payload

