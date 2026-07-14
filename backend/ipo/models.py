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
    UNKNOWN = "unknown"


class SebiFilingCategory(enum.StrEnum):
    """Official SEBI listing categories scanned by IPO-002."""

    DRHP = "drhp"
    RHP = "rhp"
    FINAL_OFFER = "final_offer"


class IpoDocumentParseStatus(enum.StrEnum):
    """Download/parse lifecycle recorded for an IPO source document.

    Beginner note:
    IPO-003 only downloads trusted PDF bytes; it does not inspect their pages.
    ``pending`` therefore means "downloaded and waiting for a future parser",
    while ``not_downloaded`` and ``download_failed`` contain no cache metadata.
    """

    NOT_DOWNLOADED = "not_downloaded"
    PENDING = "pending"
    DOWNLOAD_FAILED = "download_failed"


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


class IpoEnrichmentSignalType(enum.StrEnum):
    """Topics the IPO-009 web-enrichment collector may observe.

    Beginner note:
    These are sentiment and red-flag topics only. There is deliberately no
    member for revenue, profit, or any other financial-statement figure: web
    search results must never be able to masquerade as document evidence.
    """

    GMP = "gmp"
    NEWS = "news"
    PROMOTER_REPUTATION = "promoter_reputation"
    LITIGATION_RED_FLAG = "litigation_red_flag"
    ANCHOR_COMMENTARY = "anchor_commentary"
    BROKERAGE_REVIEW = "brokerage_review"
    PEER_DISCOVERY = "peer_discovery"


class IpoExtractionProposalStatus(enum.StrEnum):
    """Review lifecycle of one AI-proposed prospectus extraction (IPO-010).

    Beginner note:
    ``pending`` proposals are invisible to scoring. Only an administrator's
    approval — which replays the manual-extraction validation path — turns a
    proposal into evidence; ``rejected`` keeps the record for audit without
    ever exposing its numbers downstream.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class IpoCautionFlagStatus(enum.StrEnum):
    """Outcome of evaluating one hard caution flag against the evidence.

    Beginner note:
    Three states matter because two kinds of "not triggered" exist. A rule that
    ran and found nothing is ``not_triggered``; a rule whose required evidence
    was absent is ``not_evaluable`` and must never silently pass as clean.
    """

    TRIGGERED = "triggered"
    NOT_TRIGGERED = "not_triggered"
    NOT_EVALUABLE = "not_evaluable"


@dataclass(frozen=True)
class IpoCautionFlag:
    """One hard caution flag's outcome with its deterministic evidence line."""

    name: str
    status: IpoCautionFlagStatus
    evidence: str

    def __post_init__(self) -> None:
        """Normalize the flag identity, parse the status, and redact evidence."""
        name = str(self.name).strip()
        if not name:
            raise IpoValidationError("caution flag name is required.")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "status",
            _parse_enum(self.status, IpoCautionFlagStatus, "caution flag status"),
        )
        object.__setattr__(self, "evidence", str(redact_text(str(self.evidence).strip())))


@dataclass(frozen=True)
class IpoCautionFlagReport:
    """The complete, fixed-order outcome of every hard caution flag.

    Beginner note:
    The report always contains all flags — including the ones that did not
    fire and the ones that could not be evaluated — so a stored verdict can be
    audited for what was checked, not merely for what triggered.
    """

    version: str
    flags: tuple[IpoCautionFlag, ...]

    @property
    def triggered(self) -> tuple[IpoCautionFlag, ...]:
        """Return only the flags that actually fired, preserving catalog order."""
        return tuple(
            flag for flag in self.flags if flag.status is IpoCautionFlagStatus.TRIGGERED
        )


_EnumT = TypeVar("_EnumT", bound=enum.Enum)


def _parse_enum(value: object, enum_type: type[_EnumT], field_name: str) -> _EnumT:
    """Accept an enum or friendly text while returning one strict enum member."""
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
    """Normalize an optional non-negative INR amount to two decimal places."""
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
    """Validate optional public provenance and remove query/fragment secrets."""
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


def _safe_url_with_query(value: object, field_name: str) -> str:
    """Validate a public URL while retaining a non-secret listing query string."""
    url = str(redact_text(str(value).strip()))
    if not is_safe_http_url(url):
        raise IpoValidationError(f"Unsafe {field_name}: {url!r}.")
    parsed = urlsplit(url)
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), parsed.path, parsed.query, ""))


def _optional_company_key(value: object | None) -> str | None:
    """Validate the bounded normalized company identity used by IPO-002."""
    if value is None:
        return None
    key = str(value).strip()
    if not key or len(key) > 255:
        raise IpoValidationError("sebi_company_key must contain 1 to 255 characters.")
    return key


def _optional_record_hash(value: object | None) -> str | None:
    """Validate IPO-002's filing-event hash, not the downloaded PDF digest.

    ``None`` keeps manual legacy documents valid. A supplied value must be a
    complete lowercase SHA-256 hexadecimal string so ingestion can use it as an
    idempotent record identity.
    """
    if value is None:
        return None
    fingerprint = str(value).strip().lower()
    if len(fingerprint) != 64 or any(character not in "0123456789abcdef" for character in fingerprint):
        raise IpoValidationError("record_hash must be a 64-character SHA-256 hexadecimal digest.")
    return fingerprint


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
        """Quantize a known score and redact its optional human explanation."""
        if self.score is not None:
            object.__setattr__(self, "score", _score_decimal(self.score))
        cleaned_reason = (
            str(redact_text(str(self.reason).strip())) if self.reason is not None else None
        )
        object.__setattr__(self, "reason", cleaned_reason or None)


@dataclass(frozen=True)
class IpoScoreInput:
    """Collect seven factor assessments and deduplicated public provenance.

    This DTO is the complete, database-independent input to deterministic
    scoring. Missing evidence is represented inside each ``FactorAssessment``
    rather than by omitting a field, keeping the 100-point contract stable.
    """

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
        """Clean the company name and canonicalize unique source-document URLs."""
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
    """Preserve the numeric receipt before recommendation policy is applied."""

    company_name: str
    score: Decimal
    contributions: Mapping[str, Decimal]
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    source_documents: tuple[str, ...]

    def __post_init__(self) -> None:
        """Freeze the nested contribution mapping as well as the outer record."""
        # A frozen dataclass does not freeze a nested dict by itself. Copying into
        # a read-only proxy prevents a caller from rewriting the audit breakdown.
        object.__setattr__(
            self,
            "contributions",
            MappingProxyType(dict(self.contributions)),
        )


@dataclass(frozen=True)
class IpoRecommendationResult:
    """Final IPO-001 output contract, including a JSON-native serializer.

    IPO-006 appends the caution-flag report to the same contract. The field
    defaults to an empty tuple so legacy ipo-001-v1 evaluations, which predate
    hard caution flags, deserialize unchanged.
    """

    company_name: str
    score: Decimal
    recommendation: Recommendation
    recommendation_type: str
    confidence: Confidence
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    source_documents: tuple[str, ...]
    caution_flags: tuple[IpoCautionFlag, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the exact public JSON shape promised by IPO-001 and IPO-006."""
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
            "caution_flags": [
                {
                    "name": flag.name,
                    "status": flag.status.value,
                    "evidence": flag.evidence,
                }
                for flag in self.caution_flags
            ],
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
    sebi_company_key: str | None = None

    def __post_init__(self) -> None:
        """Normalize issue enums, money, chronology, lot size, and provenance."""
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
        object.__setattr__(self, "sebi_company_key", _optional_company_key(self.sebi_company_key))


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
    sebi_company_key: str | None
    created_at: dt.datetime
    updated_at: dt.datetime


@dataclass(frozen=True)
class IpoDocumentData:
    """Accept source metadata while deliberately excluding trusted cache fields.

    Callers may register URLs and IPO-002 identity, but only IPO-003's downloader
    can create content hash, path, timestamp, and parse-status provenance.
    """

    document_type: str
    document_url: str
    source_confidence: Confidence
    source_url: str | None = None
    filing_date: dt.date | None = None
    record_hash: str | None = None

    def __post_init__(self) -> None:
        """Normalize document identity, URLs, confidence, date, and record hash."""
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
        if self.filing_date is not None and not isinstance(self.filing_date, dt.date):
            raise IpoValidationError("filing_date must be a date when provided.")
        object.__setattr__(self, "record_hash", _optional_record_hash(self.record_hash))


@dataclass(frozen=True)
class IpoDocumentRecord:
    """Expose metadata and trusted download provenance after the session closes.

    ``record_hash`` identifies the SEBI listing event; ``content_sha256`` proves
    the exact cached bytes. Keeping both prevents metadata identity from being
    confused with file integrity.
    """

    id: int
    issue_id: int
    document_type: str
    document_url: str
    source_url: str | None
    source_confidence: Confidence
    filing_date: dt.date | None
    record_hash: str | None
    content_sha256: str | None
    downloaded_at: dt.datetime | None
    file_path: str | None
    page_count: int | None
    parse_status: IpoDocumentParseStatus
    created_at: dt.datetime


@dataclass(frozen=True)
class SebiFiling:
    """One filing row parsed from an official SEBI listing page."""

    category: SebiFilingCategory
    title: str
    filing_date: dt.date
    document_url: str
    source_url: str

    def __post_init__(self) -> None:
        """Validate one hostile listing row before normalization/persistence."""
        object.__setattr__(
            self,
            "category",
            _parse_enum(self.category, SebiFilingCategory, "category"),
        )
        title = str(self.title).strip()
        if not title:
            raise IpoValidationError("title is required.")
        object.__setattr__(self, "title", title)
        if not isinstance(self.filing_date, dt.date):
            raise IpoValidationError("filing_date must be a date.")
        document_url = _optional_safe_url(self.document_url, "document_url")
        if document_url is None:
            raise IpoValidationError("document_url is required.")
        object.__setattr__(self, "document_url", document_url)
        object.__setattr__(self, "source_url", _safe_url_with_query(self.source_url, "source_url"))


@dataclass(frozen=True)
class IpoFilingData:
    """Normalized, persistence-ready SEBI filing identity."""

    company_name: str
    sebi_company_key: str
    issue_type: IpoIssueType
    status: IpoStatus
    document_type: str
    filing_date: dt.date
    document_url: str
    source_url: str
    record_hash: str

    def __post_init__(self) -> None:
        """Enforce the canonical, persistence-ready identity for one filing."""
        company_name = str(self.company_name).strip()
        if not company_name:
            raise IpoValidationError("company_name is required.")
        object.__setattr__(self, "company_name", company_name)
        company_key = _optional_company_key(self.sebi_company_key)
        if company_key is None:
            raise IpoValidationError("sebi_company_key is required.")
        object.__setattr__(self, "sebi_company_key", company_key)
        object.__setattr__(self, "issue_type", _parse_enum(self.issue_type, IpoIssueType, "issue_type"))
        object.__setattr__(self, "status", _parse_enum(self.status, IpoStatus, "status"))
        document_type = str(self.document_type).strip().lower()
        # The allowed document types are exactly the SEBI listing categories, so
        # derive the set from the enum rather than duplicating the contract here.
        allowed_types = {category.value for category in SebiFilingCategory}
        if document_type not in allowed_types:
            allowed = ", ".join(sorted(allowed_types))
            raise IpoValidationError(f"document_type must be one of: {allowed}.")
        object.__setattr__(self, "document_type", document_type)
        if not isinstance(self.filing_date, dt.date):
            raise IpoValidationError("filing_date must be a date.")
        document_url = _optional_safe_url(self.document_url, "document_url")
        if document_url is None:
            raise IpoValidationError("document_url is required.")
        object.__setattr__(self, "document_url", document_url)
        object.__setattr__(self, "source_url", _safe_url_with_query(self.source_url, "source_url"))
        fingerprint = _optional_record_hash(self.record_hash)
        if fingerprint is None:
            raise IpoValidationError("record_hash is required.")
        object.__setattr__(self, "record_hash", fingerprint)


@dataclass(frozen=True)
class IpoIngestionSummary:
    """Counts returned after one category is atomically persisted."""

    received: int = 0
    issues_created: int = 0
    issues_updated: int = 0
    documents_created: int = 0
    documents_updated: int = 0
    unchanged: int = 0


@dataclass(frozen=True)
class IpoFinancialData:
    """Carry flexible period metrics plus optional document provenance.

    Raw-metric extraction is intentionally deferred; a read-only mapping lets
    later fields evolve without a migration while preventing caller mutation.
    """

    period_end: dt.date
    period_type: FinancialPeriodType
    metrics: Mapping[str, Any]
    source_confidence: Confidence
    source_document_id: int | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        """Validate period metadata and freeze a defensive copy of metrics."""
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
        """Prevent mutation of metrics returned from a closed ORM session."""
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
        """Normalize UTC capture time and non-negative demand multiples."""
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
class IpoEnrichmentSignalData:
    """Validated insert payload for one low-confidence web observation.

    Beginner note:
    The collector builds this after quarantine scanning and GMP parsing, so a
    row can only reach storage in the shape the schema promises: bounded text,
    a parsed enum type, an explicit low/medium/high confidence, and a stamped
    source policy that marks the row as web-sourced forever.
    """

    signal_type: IpoEnrichmentSignalType
    captured_at: dt.datetime
    query_text: str
    payload: tuple[Mapping[str, Any], ...]
    parsed_value: Decimal | None
    quarantined: bool
    confidence: Confidence
    source_policy: str

    def __post_init__(self) -> None:
        """Normalize enums, bound text fields, and quantize the parsed value."""
        object.__setattr__(
            self,
            "signal_type",
            _parse_enum(self.signal_type, IpoEnrichmentSignalType, "signal_type"),
        )
        if not isinstance(self.captured_at, dt.datetime) or self.captured_at.tzinfo is None:
            raise IpoValidationError("captured_at must be a timezone-aware datetime.")
        query_text = str(self.query_text).strip()
        if not query_text or len(query_text) > 255:
            raise IpoValidationError("query_text must contain 1 to 255 characters.")
        object.__setattr__(self, "query_text", query_text)
        object.__setattr__(
            self,
            "payload",
            tuple(MappingProxyType(dict(entry)) for entry in self.payload),
        )
        if self.parsed_value is not None:
            parsed = Decimal(str(self.parsed_value))
            if not parsed.is_finite():
                raise IpoValidationError("parsed_value must be finite when provided.")
            object.__setattr__(
                self, "parsed_value", parsed.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
            )
        object.__setattr__(self, "quarantined", bool(self.quarantined))
        object.__setattr__(
            self, "confidence", _parse_enum(self.confidence, Confidence, "confidence")
        )
        source_policy = str(self.source_policy).strip()
        if not source_policy or len(source_policy) > 40:
            raise IpoValidationError("source_policy must contain 1 to 40 characters.")
        object.__setattr__(self, "source_policy", source_policy)


@dataclass(frozen=True)
class IpoEnrichmentSignalRecord:
    """Detached low-confidence web enrichment observation (IPO-009).

    Beginner note:
    ``payload`` entries carry search-result metadata (title, link, source,
    snippet, matched keywords). A quarantined signal had its untrusted text
    replaced by the blocked-evidence marker before storage, so this record can
    circulate safely; the raw hostile text is never reachable from here.
    """

    id: int
    issue_id: int
    signal_type: IpoEnrichmentSignalType
    captured_at: dt.datetime
    query_text: str
    payload: tuple[Mapping[str, Any], ...]
    parsed_value: Decimal | None
    quarantined: bool
    confidence: Confidence
    source_policy: str
    created_at: dt.datetime

    def __post_init__(self) -> None:
        """Freeze payload entries so a detached record stays read-only."""
        object.__setattr__(
            self,
            "payload",
            tuple(MappingProxyType(dict(entry)) for entry in self.payload),
        )


@dataclass(frozen=True)
class IpoExtractionProposalRecord:
    """Detached AI extraction proposal awaiting or past human review (IPO-010).

    Beginner note:
    ``payload`` is the exact manual-extraction-shaped dict the agent proposed
    (every value paired with its prospectus page citation). It is data under
    review, never evidence: approval reconstructs and re-validates it through
    the same strict domain types a hand-entered submission uses.
    """

    id: int
    issue_id: int
    document_id: int
    company_name: str
    document_url: str
    status: IpoExtractionProposalStatus
    payload: Mapping[str, Any]
    confidence: Confidence
    needs_review_reasons: tuple[str, ...]
    model_version: str
    agent_model: str
    source_content_sha256: str
    page_count: int
    created_at: dt.datetime
    reviewed_by_email: str | None
    reviewed_at: dt.datetime | None
    review_note: str | None
    manual_extraction_id: int | None

    def __post_init__(self) -> None:
        """Freeze the proposed payload so a detached record stays read-only."""
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class IpoEvaluationRecord:
    """Detached immutable score/recommendation pair.

    ``inputs_fingerprint`` (IPO-006) is the SHA-256 of exactly the evidence the
    scoring service consumed; legacy ipo-001-v1 rows carry ``None``.
    """

    issue_id: int
    score_id: int
    recommendation_id: int
    model_version: str
    scored_at: dt.datetime
    result: IpoRecommendationResult
    inputs_fingerprint: str | None = None
