"""Convert normalized IPO evidence into the PDF's deterministic 100-point score.

This module performs arithmetic only: it does not fetch evidence, decide whether
an IPO is investable, or talk to the database. Keeping scoring pure makes every
result reproducible from the seven frozen factor assessments stored with it.
"""

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

    Each factor arrives on a 0-100 scale, so multiplying it by its PDF weight
    and dividing by 100 yields that factor's contribution. Missing values earn
    zero points and are never renormalized: reweighting the available factors
    would let incomplete evidence masquerade as a complete assessment.

    Decimal arithmetic and ``ROUND_HALF_UP`` make the persisted two-decimal
    receipt stable and familiar to financial users; binary floating-point and
    Python's default half-even rounding could otherwise shift boundary values.
    """
    raw_total = Decimal(0)
    contributions: dict[str, Decimal] = {}
    missing_data: list[str] = []
    reasons: list[str] = []

    for factor_name, weight in PDF_WEIGHTS.items():
        # Attribute names intentionally match PDF_WEIGHTS and IpoScoreInput.
        # This ordered loop keeps contributions, missing labels, and reasons in
        # the same authoritative factor order.
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

