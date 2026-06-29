"""IPO-001 deterministic scorecard tests."""

from __future__ import annotations

from decimal import Decimal

from backend.ipo.models import FactorAssessment, IpoScoreInput
from backend.ipo.scorecard import PDF_WEIGHTS, score_ipo


def _factor(score: object | None, reason: str | None = None) -> FactorAssessment:
    return FactorAssessment(score=score, reason=reason)


def _input(**overrides: object) -> IpoScoreInput:
    values: dict[str, object] = {
        "company_name": "Example Ltd",
        "business_quality": _factor(100, "Market-leading business"),
        "financial_growth": _factor(50, "Moderate financial growth"),
        "return_ratios": _factor(80, "Healthy ROE and ROCE"),
        "valuation": _factor(60, "Fair relative valuation"),
        "qib_subscription": _factor(70, "Good institutional demand"),
        "promoter_quality": _factor(90, "Experienced promoters"),
        "gmp_sentiment": _factor(40, "Measured market sentiment"),
        "source_documents": ("https://www.sebi.gov.in/example-rhp.pdf",),
    }
    values.update(overrides)
    return IpoScoreInput(**values)


def test_pdf_weights_are_the_authoritative_100_point_framework() -> None:
    assert PDF_WEIGHTS == {
        "business_quality": 25,
        "financial_growth": 20,
        "return_ratios": 15,
        "valuation": 15,
        "qib_subscription": 10,
        "promoter_quality": 10,
        "gmp_sentiment": 5,
    }
    assert sum(PDF_WEIGHTS.values()) == 100


def test_scorecard_applies_pdf_weights_without_mutating_the_input() -> None:
    score_input = _input()

    result = score_ipo(score_input)

    assert result.score == Decimal("74.00")
    assert result.contributions == {
        "business_quality": Decimal("25.00"),
        "financial_growth": Decimal("10.00"),
        "return_ratios": Decimal("12.00"),
        "valuation": Decimal("9.00"),
        "qib_subscription": Decimal("7.00"),
        "promoter_quality": Decimal("9.00"),
        "gmp_sentiment": Decimal("2.00"),
    }
    assert score_input.business_quality.score == Decimal("100")


def test_missing_optional_factors_contribute_zero_without_weight_renormalization() -> None:
    result = score_ipo(
        _input(
            financial_growth=_factor(100),
            return_ratios=_factor(100),
            valuation=_factor(100),
            promoter_quality=_factor(100),
            qib_subscription=_factor(None),
            gmp_sentiment=_factor(None),
        )
    )

    assert result.score == Decimal("85.00")
    assert result.missing_data == ("qib_subscription", "gmp_sentiment")
    assert result.contributions["qib_subscription"] == Decimal("0.00")
    assert result.contributions["gmp_sentiment"] == Decimal("0.00")


def test_scorecard_rounds_half_up_to_two_decimal_places() -> None:
    result = score_ipo(
        _input(
            business_quality=_factor(0),
            financial_growth=_factor(0),
            return_ratios=_factor(0),
            valuation=_factor(0),
            qib_subscription=_factor(0),
            promoter_quality=_factor(0),
            gmp_sentiment=_factor("1.11"),
        )
    )

    assert result.score == Decimal("0.06")


def test_scorecard_preserves_reasons_in_pdf_factor_order() -> None:
    result = score_ipo(_input())

    assert result.reasons == (
        "Market-leading business",
        "Moderate financial growth",
        "Healthy ROE and ROCE",
        "Fair relative valuation",
        "Good institutional demand",
        "Experienced promoters",
        "Measured market sentiment",
    )
    assert result.source_documents == ("https://www.sebi.gov.in/example-rhp.pdf",)

