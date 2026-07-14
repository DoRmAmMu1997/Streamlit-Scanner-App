"""Fail-closed IPO recommendation policy built on the deterministic scorecard."""

from __future__ import annotations

from decimal import Decimal

from backend.ipo.models import (
    Confidence,
    IpoCautionFlagReport,
    IpoRecommendationResult,
    IpoScoreResult,
    Recommendation,
)

APPLY_AND_HOLD = "Apply confidently and consider holding if allotted"
APPLY_FOR_LISTING_GAINS = "Apply primarily for listing gains"
SKIP = "Skip"
# IPO-006: the dedicated sub-label for the fail-closed data-gap branch. It
# keeps a "we could not verify enough" rejection distinguishable from a scored
# "the numbers are bad" rejection in history and on the dashboard.
INSUFFICIENT_VERIFIED_DATA = "Insufficient verified data"

CRITICAL_FACTORS = (
    "business_quality",
    "financial_growth",
    "return_ratios",
    "valuation",
    "promoter_quality",
)
OPTIONAL_FACTORS = ("qib_subscription", "gmp_sentiment")


def build_recommendation(
    score_result: IpoScoreResult,
    *,
    caution_flags: IpoCautionFlagReport | None = None,
) -> IpoRecommendationResult:
    """Apply score bands, mandatory-data rules, and confidence to one receipt.

    The recommendation is deliberately binary. Missing any fundamental factor
    forces ``Not Recommended`` even when the numeric total is high, because a
    partial score must never look like positive investment advice. QIB demand
    and GMP sentiment are optional timing signals, so their absence lowers
    confidence without independently forcing a rejection.

    Beginner note:
        The IPO-006 decision order matters and is deliberate. (1) Missing
        critical data wins: it earns the "Insufficient verified data" sub-label
        because nothing else about the issue can be trusted. (2) Any triggered
        hard caution flag also forces ``Not Recommended`` regardless of score —
        a 95-point company with negative operating cash flow is still a skip.
        (3) Only then do the ordinary score bands apply. Flags that are merely
        ``not_evaluable`` never change the verdict; they ride along in the
        report so reviewers can see what could not be checked.
    """
    missing = set(score_result.missing_data)
    missing_critical = [name for name in CRITICAL_FACTORS if name in missing]
    triggered = caution_flags.triggered if caution_flags is not None else ()

    reasons = list(score_result.reasons)
    # Triggered-flag lines are prepended so consumers cannot overlook a hard
    # red line; the missing-critical line (when present) is prepended after
    # them so it ends up first overall — the strongest explanation leads.
    for flag in reversed(triggered):
        reasons.insert(0, f"Hard caution flag: {flag.name} - {flag.evidence}")

    if missing_critical:
        # Put the safety explanation first so consumers cannot overlook why an
        # apparently adequate numeric score was rejected.
        labels = ", ".join(name.replace("_", " ") for name in missing_critical)
        reasons.insert(0, f"Missing critical data: {labels}.")
        recommendation = Recommendation.NOT_RECOMMENDED
        recommendation_type = INSUFFICIENT_VERIFIED_DATA
    elif triggered:
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
        caution_flags=caution_flags.flags if caution_flags is not None else (),
    )
