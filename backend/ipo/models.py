"""Typed, framework-independent contracts for the IPO subsystem.

Beginner note:
These objects describe values moving through IPO scoring. They deliberately do
not import SQLAlchemy or Streamlit: scoring remains usable in jobs, tests, and a
future UI, while database table shapes stay inside ``backend.storage``.
"""

from __future__ import annotations

import datetime as dt
import enum
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from types import MappingProxyType
from typing import Any, TypeVar
from urllib.parse import urlsplit, urlunsplit

from backend.security import redact_text
from backend.url_safety import is_safe_http_url


class IpoValidationError(ValueError):
    """Raised when an IPO domain value cannot satisfy the public contract."""


class IpoIssueType(enum.StrEnum):
    """Supported Indian IPO market segments."""

    MAINBOARD = "mainboard"
    SME = "sme"


class IpoStatus(enum.StrEnum):
    """Lifecycle states used by the IPO issue table."""

    DRHP_FILED = "drhp_filed"
    RHP_FILED = "rhp_filed"
    OPEN = "open"
    CLOSED = "closed"
    LISTED = "listed"


class Confidence(enum.StrEnum):
    """Completeness-derived confidence attached to a recommendation."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class FinancialPeriodType(enum.StrEnum):
    """Supported financial statement periods."""

    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class Recommendation(enum.StrEnum):
    """The deliberately binary IPO decision contract."""

    RECOMMENDED = "Recommended"
    NOT_RECOMMENDED = "Not Recommended"


_EnumT = TypeVar("_EnumT", bound=enum.Enum)


def _parse_enum(value: object, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    if isinstance(value, enum_type):
        return value
    text = str(value).strip()
    # Try the canonical value first so enums whose members are not lowercase
    # (e.g. ``Recommendation`` -> "Recommended") still parse, then fall back to
    # a case-normalized match for the lowercase enums callers usually supply.
    for candidate in (text, text.lower()):
        try:
            return enum_type(candidate)
        except ValueError:
            continue
    allowed = ", ".join(str(member.value) for member in enum_type)
    raise IpoValidationError(f"{field_name} must be one of: {allowed}.")


def _optional_money(value: object | None, field_name: str) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IpoValidationError(f"{field_name} must be a numeric INR amount.") from exc
    if not parsed.is_finite() or parsed < 0:
        raise IpoValidationError(f"{field_name} must be a finite non-negative INR amount.")
    return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _optional_safe_url(value: object | None, field_name: str) -> str | None:
    if value is None:
        return None
    # Query strings and fragments are not needed for provenance identity and
    # commonly carry access tokens. Redact before validation/error reporting,
    # then retain only the stable public document location.
    url = str(redact_text(str(value).strip()))
    if not is_safe_http_url(url):
        raise IpoValidationError(f"Unsafe {field_name}: {url!r}.")
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc, parsed.path, "", ""))


def _score_decimal(value: Any) -> Decimal:
    """Convert one factor score to a finite Decimal in the inclusive 0..100 range.

    The result is quantized to two decimals (half-up) to match the ``Numeric(5, 2)``
    storage columns. Without this, SQLite would persist the raw input verbatim while
    Postgres rounds it, so the same score could read back differently per backend.
    """
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise IpoValidationError("Factor scores must be numeric values from 0 to 100.") from exc
    if not parsed.is_finite() or parsed < 0 or parsed > 100:
        raise IpoValidationError("Factor scores must be finite values from 0 to 100.")
    return parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class FactorAssessment:
    """One normalized factor score and its short evidence-based explanation.

    ``None`` means the factor is genuinely unavailable. A known weak factor is
    represented by score ``0`` instead, preserving the distinction between
    negative evidence and missing evidence.
    """

    score: Decimal | None
    reason: str | None = None

    def __post_init__(self) -> None:
        if self.score is not None:
            object.__setattr__(self, "score", _score_decimal(self.score))
        cleaned_reason = (
            str(redact_text(str(self.reason).strip())) if self.reason is not None else None
        )
        object.__setattr__(self, "reason", cleaned_reason or None)


@dataclass(frozen=True)
class IpoScoreInput:
    """All normalized evidence required by the IPO-001 scorecard."""

    company_name: str
    business_quality: FactorAssessment
    financial_growth: FactorAssessment
    return_ratios: FactorAssessment
    valuation: FactorAssessment
    qib_subscription: FactorAssessment
    promoter_quality: FactorAssessment
    gmp_sentiment: FactorAssessment
    source_documents: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        cleaned_company = str(self.company_name).strip()
        if not cleaned_company:
            raise IpoValidationError("company_name is required.")
        object.__setattr__(self, "company_name", cleaned_company)

        documents: list[str] = []
        for value in self.source_documents:
            url = _optional_safe_url(value, "source document URL")
            if url is None:
                raise IpoValidationError("source document URL is required.")
            if url not in documents:
                documents.append(url)
        object.__setattr__(self, "source_documents", tuple(documents))


@dataclass(frozen=True)
class IpoScoreResult:
    """Deterministic scorecard output before verdict policy is applied."""

    company_name: str
    score: Decimal
    contributions: Mapping[str, Decimal]
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    source_documents: tuple[str, ...]

    def __post_init__(self) -> None:
        # A frozen dataclass does not freeze a nested dict by itself. Copying into
        # a read-only proxy prevents a caller from rewriting the audit breakdown.
        object.__setattr__(
            self,
            "contributions",
            MappingProxyType(dict(self.contributions)),
        )


@dataclass(frozen=True)
class IpoRecommendationResult:
    """Final IPO-001 output contract, including a JSON-native serializer."""

    company_name: str
    score: Decimal
    recommendation: Recommendation
    recommendation_type: str
    confidence: Confidence
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    source_documents: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the exact public JSON shape promised by IPO-001."""
        numeric_score: int | float = (
            int(self.score)
            if self.score == self.score.to_integral_value()
            else float(self.score)
        )
        return {
            "company_name": self.company_name,
            "score": numeric_score,
            "recommendation": self.recommendation.value,
            "recommendation_type": self.recommendation_type,
            "confidence": self.confidence.value,
            "reasons": list(self.reasons),
            "missing_data": list(self.missing_data),
            "source_documents": list(self.source_documents),
        }


@dataclass(frozen=True)
class IpoIssueData:
    """Validated create/update payload for an IPO issue."""

    company_name: str
    issue_type: IpoIssueType
    status: IpoStatus
    source_confidence: Confidence
    open_date: dt.date | None = None
    close_date: dt.date | None = None
    price_band_low: Decimal | None = None
    price_band_high: Decimal | None = None
    lot_size: int | None = None
    fresh_issue_amount: Decimal | None = None
    ofs_amount: Decimal | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        company = str(self.company_name).strip()
        if not company:
            raise IpoValidationError("company_name is required.")
        object.__setattr__(self, "company_name", company)
        object.__setattr__(self, "issue_type", _parse_enum(self.issue_type, IpoIssueType, "issue_type"))
        object.__setattr__(self, "status", _parse_enum(self.status, IpoStatus, "status"))
        object.__setattr__(
            self,
            "source_confidence",
            _parse_enum(self.source_confidence, Confidence, "source_confidence"),
        )
        for field_name in (
            "price_band_low",
            "price_band_high",
            "fresh_issue_amount",
            "ofs_amount",
        ):
            object.__setattr__(
                self,
                field_name,
                _optional_money(getattr(self, field_name), field_name),
            )
        if self.lot_size is not None and self.lot_size <= 0:
            raise IpoValidationError("lot_size must be positive when provided.")
        if self.open_date and self.close_date and self.close_date < self.open_date:
            raise IpoValidationError("close_date cannot be before open_date.")
        if (
            self.price_band_low is not None
            and self.price_band_high is not None
            and self.price_band_high < self.price_band_low
        ):
            raise IpoValidationError("price_band_high cannot be below price_band_low.")
        object.__setattr__(self, "source_url", _optional_safe_url(self.source_url, "source_url"))


@dataclass(frozen=True)
class IpoIssueRecord:
    """Detached issue row returned by the public repository."""

    id: int
    company_name: str
    issue_type: IpoIssueType
    status: IpoStatus
    source_confidence: Confidence
    open_date: dt.date | None
    close_date: dt.date | None
    price_band_low: Decimal | None
    price_band_high: Decimal | None
    lot_size: int | None
    fresh_issue_amount: Decimal | None
    ofs_amount: Decimal | None
    source_url: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True)
class IpoDocumentData:
    """Validated create/update payload for an IPO source document."""

    document_type: str
    document_url: str
    source_confidence: Confidence
    source_url: str | None = None

    def __post_init__(self) -> None:
        document_type = str(self.document_type).strip().lower()
        if not document_type:
            raise IpoValidationError("document_type is required.")
        object.__setattr__(self, "document_type", document_type)
        document_url = _optional_safe_url(self.document_url, "document_url")
        if document_url is None:
            raise IpoValidationError("document_url is required.")
        object.__setattr__(self, "document_url", document_url)
        object.__setattr__(self, "source_url", _optional_safe_url(self.source_url, "source_url"))
        object.__setattr__(
            self,
            "source_confidence",
            _parse_enum(self.source_confidence, Confidence, "source_confidence"),
        )


@dataclass(frozen=True)
class IpoDocumentRecord:
    """Detached IPO document row."""

    id: int
    issue_id: int
    document_type: str
    document_url: str
    source_url: str | None
    source_confidence: Confidence
    created_at: dt.datetime


@dataclass(frozen=True)
class IpoFinancialData:
    """Validated create/update payload for one financial period."""

    period_end: dt.date
    period_type: FinancialPeriodType
    metrics: Mapping[str, Any]
    source_confidence: Confidence
    source_document_id: int | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.period_end, dt.date):
            raise IpoValidationError("period_end must be a date.")
        object.__setattr__(
            self,
            "period_type",
            _parse_enum(self.period_type, FinancialPeriodType, "period_type"),
        )
        if not isinstance(self.metrics, Mapping):
            raise IpoValidationError("metrics must be a mapping.")
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))
        if self.source_document_id is not None and self.source_document_id <= 0:
            raise IpoValidationError("source_document_id must be positive when provided.")
        object.__setattr__(self, "source_url", _optional_safe_url(self.source_url, "source_url"))
        object.__setattr__(
            self,
            "source_confidence",
            _parse_enum(self.source_confidence, Confidence, "source_confidence"),
        )


@dataclass(frozen=True)
class IpoFinancialRecord:
    """Detached financial-period row."""

    id: int
    issue_id: int
    period_end: dt.date
    period_type: FinancialPeriodType
    metrics: Mapping[str, Any]
    source_document_id: int | None
    source_url: str | None
    source_confidence: Confidence
    created_at: dt.datetime
    updated_at: dt.datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class IpoSubscriptionData:
    """Validated create/update payload for a subscription snapshot."""

    captured_at: dt.datetime
    source_confidence: Confidence
    qib_multiple: Decimal | None = None
    nii_multiple: Decimal | None = None
    retail_multiple: Decimal | None = None
    total_multiple: Decimal | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.captured_at, dt.datetime) or self.captured_at.tzinfo is None:
            raise IpoValidationError("captured_at must be a timezone-aware datetime.")
        object.__setattr__(self, "captured_at", self.captured_at.astimezone(dt.UTC))
        for field_name in (
            "qib_multiple",
            "nii_multiple",
            "retail_multiple",
            "total_multiple",
        ):
            value = getattr(self, field_name)
            if value is None:
                continue
            try:
                parsed = Decimal(str(value))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise IpoValidationError(f"{field_name} must be numeric.") from exc
            if not parsed.is_finite() or parsed < 0:
                raise IpoValidationError(f"{field_name} must be finite and non-negative.")
            object.__setattr__(
                self,
                field_name,
                parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
            )
        object.__setattr__(self, "source_url", _optional_safe_url(self.source_url, "source_url"))
        object.__setattr__(
            self,
            "source_confidence",
            _parse_enum(self.source_confidence, Confidence, "source_confidence"),
        )


@dataclass(frozen=True)
class IpoSubscriptionRecord:
    """Detached subscription snapshot row."""

    id: int
    issue_id: int
    captured_at: dt.datetime
    qib_multiple: Decimal | None
    nii_multiple: Decimal | None
    retail_multiple: Decimal | None
    total_multiple: Decimal | None
    source_url: str | None
    source_confidence: Confidence
    created_at: dt.datetime


@dataclass(frozen=True)
class IpoEvaluationRecord:
    """Detached immutable score/recommendation pair."""

    issue_id: int
    score_id: int
    recommendation_id: int
    model_version: str
    scored_at: dt.datetime
    result: IpoRecommendationResult
