"""Exercise the public IPO domain and package-export contracts.

Beginner note:
These tests intentionally import both concrete models and the convenient
``backend.ipo`` facade. That catches a common packaging mistake where a feature
works internally but its supported public name was never re-exported.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend import ipo
from backend.ipo.models import (
    Confidence,
    FactorAssessment,
    IpoDocumentParseStatus,
    IpoIssueType,
    IpoScoreInput,
    IpoStatus,
    IpoValidationError,
    Recommendation,
    _parse_enum,
)


def test_document_parse_status_has_only_the_ipo_003_lifecycle_values() -> None:
    """Keep download state small until a later sprint actually parses PDFs."""
    assert {status.value for status in IpoDocumentParseStatus} == {
        "not_downloaded",
        "pending",
        "download_failed",
    }


def _assessment(score: object = 80, reason: str = "Evidence-backed assessment") -> FactorAssessment:
    """Build the reusable assessment fixture used by the scenarios below."""
    return FactorAssessment(score=score, reason=reason)


def _score_input(**overrides: object) -> IpoScoreInput:
    """Build the reusable score input fixture used by the scenarios below."""
    values: dict[str, object] = {
        "company_name": " Example Ltd ",
        "business_quality": _assessment(),
        "financial_growth": _assessment(),
        "return_ratios": _assessment(),
        "valuation": _assessment(),
        "qib_subscription": _assessment(),
        "promoter_quality": _assessment(),
        "gmp_sentiment": _assessment(),
        "source_documents": ("https://www.sebi.gov.in/example-rhp.pdf",),
    }
    values.update(overrides)
    return IpoScoreInput(**values)


def test_factor_assessment_normalizes_a_finite_score() -> None:
    """Pin factor assessment normalizes a finite score as an executable IPO regression contract."""
    assessment = FactorAssessment(score="78.25", reason="  Strong growth  ")

    assert assessment.score == Decimal("78.25")
    assert assessment.reason == "Strong growth"


def test_factor_assessment_quantizes_score_half_up_to_two_decimals() -> None:
    # Quantizing to two decimals matches the Numeric(5, 2) score columns so a
    # value reads back identically on SQLite (verbatim) and Postgres (rounded).
    """Pin factor assessment quantizes score half up to two decimals as an executable IPO regression contract."""
    assert FactorAssessment(score="78.255").score == Decimal("78.26")
    assert FactorAssessment(score="78.254").score == Decimal("78.25")


def test_parse_enum_accepts_exact_then_case_normalized_values() -> None:
    # Lowercase enums tolerate any input casing the caller supplies...
    """Pin parse enum accepts exact then case normalized values as an executable IPO regression contract."""
    assert _parse_enum("MAINBOARD", IpoIssueType, "issue_type") is IpoIssueType.MAINBOARD
    assert _parse_enum("High", Confidence, "source_confidence") is Confidence.HIGH
    # ...while a non-lowercase enum still parses from its canonical value, which
    # the previous unconditional lower() would have rejected.
    assert (
        _parse_enum("Recommended", Recommendation, "recommendation")
        is Recommendation.RECOMMENDED
    )
    with pytest.raises(IpoValidationError):
        _parse_enum("rumoured", IpoStatus, "status")


def test_factor_assessment_redacts_secret_shaped_reason_text() -> None:
    """Pin factor assessment redacts secret shaped reason text as an executable IPO regression contract."""
    assessment = FactorAssessment(
        score=78,
        reason="Verified from provider response: api_key=supersecret",
    )

    assert assessment.reason == "Verified from provider response: api_key=***REDACTED***"


@pytest.mark.parametrize("score", [-1, 100.01, "NaN", "Infinity", object()])
def test_factor_assessment_rejects_invalid_scores(score: object) -> None:
    """Pin factor assessment rejects invalid scores as an executable IPO regression contract."""
    with pytest.raises(IpoValidationError):
        FactorAssessment(score=score, reason="Bad score")


def test_score_input_normalizes_company_and_source_documents() -> None:
    """Pin score input normalizes company and source documents as an executable IPO regression contract."""
    score_input = _score_input()

    assert score_input.company_name == "Example Ltd"
    assert score_input.source_documents == ("https://www.sebi.gov.in/example-rhp.pdf",)


def test_score_input_strips_query_and_fragment_from_provenance_urls() -> None:
    """Pin score input strips query and fragment from provenance urls as an executable IPO regression contract."""
    score_input = _score_input(
        source_documents=(
            "https://www.sebi.gov.in/example-rhp.pdf?api_key=supersecret#page=7",
        ),
    )

    assert score_input.source_documents == ("https://www.sebi.gov.in/example-rhp.pdf",)


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/rhp.pdf",
        "http://127.0.0.1/rhp.pdf",
        "https://user:password@example.com/rhp.pdf",
    ],
)
def test_score_input_rejects_unsafe_document_urls(url: str) -> None:
    """Pin score input rejects unsafe document urls as an executable IPO regression contract."""
    with pytest.raises(IpoValidationError):
        _score_input(source_documents=(url,))


def test_public_ipo_package_exports_the_domain_and_repository_contract() -> None:
    """Keep every supported IPO contract reachable from the stable package facade.

    Beginner note:
        A public export is part of the feature contract. Checking the IPO-005 names
        here prevents a refactor from leaving the engine implemented but unreachable
        through the import path used by other subsystems.
    """
    expected = {
        "CAUTION_FLAGS_VERSION",
        "CAUTION_FLAG_ORDER",
        "Confidence",
        "ENRICHMENT_SOURCE_POLICY",
        "FACTOR_MODEL_VERSION",
        "FactorAssessment",
        "FinancialPeriodType",
        "INSUFFICIENT_VERIFIED_DATA",
        "IpoCautionFlag",
        "IpoCautionFlagReport",
        "IpoCautionFlagStatus",
        "IpoDocumentData",
        "IpoDocumentDownloadError",
        "IpoDocumentDownloadErrorCode",
        "IpoDocumentDownloadResult",
        "IpoDocumentParseStatus",
        "IpoDocumentRecord",
        "IpoEnrichmentOutcome",
        "IpoEnrichmentSignalData",
        "IpoEnrichmentSignalRecord",
        "IpoEnrichmentSignalType",
        "IpoEvaluationRecord",
        "IpoExtractionProposalRecord",
        "IpoExtractionProposalStatus",
        "IpoFactorInputs",
        "IpoFinancialData",
        "IpoFinancialRecord",
        "IpoFilingData",
        "IpoIngestionSummary",
        "IpoAmountUnit",
        "IpoManualExtractionData",
        "IpoManualExtractionRecord",
        "IpoManualPeriodData",
        "IpoPerShareReconciliation",
        "IpoPeerMetric",
        "IpoPeerValuationData",
        "IpoRatioAnalysis",
        "IpoRatioName",
        "IpoRatioReceipt",
        "IpoRatioStatus",
        "IpoShareUnit",
        "IpoIssueData",
        "IpoIssueRecord",
        "IpoIssueType",
        "IpoNotFoundError",
        "IpoRecommendationResult",
        "IpoScoreInput",
        "IpoScoreResult",
        "IpoStatus",
        "IpoSubscriptionData",
        "IpoSubscriptionRecord",
        "IpoValidationError",
        "Recommendation",
        "SebiFiling",
        "SebiFilingCategory",
        "approve_extraction_proposal",
        "build_recommendation",
        "calculate_ipo_ratios",
        "collect_enrichment_signals",
        "create_document",
        "create_financial",
        "create_issue",
        "create_subscription",
        "delete_document",
        "download_document",
        "delete_evaluation",
        "delete_financial",
        "delete_issue",
        "delete_subscription",
        "derive_score_input",
        "evaluate_caution_flags",
        "evaluate_issue",
        "fetch_sebi_filings",
        "get_document",
        "get_evaluation",
        "get_financial",
        "get_issue",
        "get_latest_recommendation",
        "get_latest_filing_date",
        "get_latest_manual_profile",
        "get_latest_ipo_ratios",
        "get_manual_extraction",
        "get_subscription",
        "list_documents",
        "list_enrichment_signals",
        "list_evaluations",
        "list_extraction_proposals",
        "list_financials",
        "list_issues",
        "list_manual_extractions",
        "list_subscriptions",
        "ingest_filings",
        "record_enrichment_signals",
        "reject_extraction_proposal",
        "score_ipo",
        "submit_extraction_proposal",
        "submit_manual_extraction",
        "update_document",
        "update_financial",
        "update_issue",
        "update_subscription",
    }

    assert set(ipo.__all__) == expected
    assert all(hasattr(ipo, name) for name in expected)
