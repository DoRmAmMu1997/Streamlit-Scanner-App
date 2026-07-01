"""Fail-closed IPO recommendation policy built on the deterministic scorecard."""

from __future__ import annotations

from decimal import Decimal

from backend.ipo.models import (
    Confidence,
    IpoRecommendationResult,
    IpoScoreResult,
    Recommendation,
)

APPLY_AND_HOLD = "Apply confidently and consider holding if allotted"
APPLY_FOR_LISTING_GAINS = "Apply primarily for listing gains"
SKIP = "Skip"

CRITICAL_FACTORS = (
    "business_quality",
    "financial_growth",
    "return_ratios",
    "valuation",
    "promoter_quality",
)
OPTIONAL_FACTORS = ("qib_subscription", "gmp_sentiment")


def build_recommendation(score_result: IpoScoreResult) -> IpoRecommendationResult:
    """Apply score bands, mandatory-data rules, and confidence to one receipt.

    The recommendation is deliberately binary. Missing any fundamental factor
    forces ``Not Recommended`` even when the numeric total is high, because a
    partial score must never look like positive investment advice. QIB demand
    and GMP sentiment are optional timing signals, so their absence lowers
    confidence without independently forcing a rejection.
    """
    missing = set(score_result.missing_data)
    missing_critical = [name for name in CRITICAL_FACTORS if name in missing]

    reasons = list(score_result.reasons)
    if missing_critical:
        # Put the safety explanation first so consumers cannot overlook why an
        # apparently adequate numeric score was rejected.
        labels = ", ".join(name.replace("_", " ") for name in missing_critical)
        reasons.insert(0, f"Missing critical data: {labels}.")
        recommendation = Recommendation.NOT_RECOMMENDED
        recommendation_type = SKIP
    elif score_result.score >= Decimal(80):
        recommendation = Recommendation.RECOMMENDED
        recommendation_type = APPLY_AND_HOLD
    elif score_result.score >= Decimal(65):
        recommendation = Recommendation.RECOMMENDED
        recommendation_type = APPLY_FOR_LISTING_GAINS
    else:
        recommendation = Recommendation.NOT_RECOMMENDED
        recommendation_type = SKIP

    missing_optional_count = sum(name in missing for name in OPTIONAL_FACTORS)
    # Confidence describes evidence completeness, not recommendation strength.
    # A well-supported rejection can therefore still carry high confidence.
    if missing_critical or missing_optional_count == len(OPTIONAL_FACTORS):
        confidence = Confidence.LOW
    elif missing_optional_count == 1:
        confidence = Confidence.MEDIUM
    else:
        confidence = Confidence.HIGH

    return IpoRecommendationResult(
        company_name=score_result.company_name,
        score=score_result.score,
        recommendation=recommendation,
        recommendation_type=recommendation_type,
        confidence=confidence,
        reasons=tuple(reasons),
        missing_data=score_result.missing_data,
        source_documents=score_result.source_documents,
    )

