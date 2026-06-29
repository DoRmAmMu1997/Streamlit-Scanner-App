"""Deterministic implementation of the IPO PDF's 100-point scorecard."""

from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal

from backend.ipo.models import IpoScoreInput, IpoScoreResult

PDF_WEIGHTS: dict[str, int] = {
    "business_quality": 25,
    "financial_growth": 20,
    "return_ratios": 15,
    "valuation": 15,
    "qib_subscription": 10,
    "promoter_quality": 10,
    "gmp_sentiment": 5,
}

_PENNY = Decimal("0.01")
_HUNDRED = Decimal(100)


def score_ipo(score_input: IpoScoreInput) -> IpoScoreResult:
    """Apply the fixed PDF weights and return an immutable scoring receipt.

    Missing values earn zero points and are never renormalized. Renormalizing
    would let incomplete evidence masquerade as a full 100-point assessment.
    """
    raw_total = Decimal(0)
    contributions: dict[str, Decimal] = {}
    missing_data: list[str] = []
    reasons: list[str] = []

    for factor_name, weight in PDF_WEIGHTS.items():
        assessment = getattr(score_input, factor_name)
        if assessment.score is None:
            contribution = Decimal(0)
            missing_data.append(factor_name)
        else:
            contribution = assessment.score * Decimal(weight) / _HUNDRED
            raw_total += contribution
        contributions[factor_name] = contribution.quantize(_PENNY, rounding=ROUND_HALF_UP)
        if assessment.reason:
            reasons.append(assessment.reason)

    return IpoScoreResult(
        company_name=score_input.company_name,
        score=raw_total.quantize(_PENNY, rounding=ROUND_HALF_UP),
        contributions=contributions,
        reasons=tuple(reasons),
        missing_data=tuple(missing_data),
        source_documents=score_input.source_documents,
    )

