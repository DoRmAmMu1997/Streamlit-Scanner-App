"""Typed, framework-independent contracts for the IPO subsystem.

Beginner note:
These objects describe values moving through IPO scoring. They deliberately do
not import SQLAlchemy or Streamlit: scoring remains usable in jobs, tests, and a
future UI, while database table shapes stay inside ``backend.storage``.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import re
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


@dataclass(frozen=True)
class CitedFinancialFact:
    """One exact financial value bound to immutable document provenance.

    Beginner note:
        The model's JSON is only a draft. This host-created fact records the
        exact printed token and its original text-line/table-cell identity so
        approval can distinguish verified evidence from untrusted raw fields.
    """

    field_name: str
    value: Decimal
    unit: str | None
    unit_multiplier: Decimal
    period_end: dt.date | None
    document_sha256: str
    page_number: int
    location: str
    source_token: str
    confidence: Confidence
    verification_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate and normalize the immutable numeric evidence anchor.

        Beginner note:
            Construction is the last opportunity to reject contradictory
            provenance before a fact is stored. A finite value, positive scale,
            valid document digest, positive page, and concrete source location
            are required together.
        """
        if not self.field_name.strip():
            raise IpoValidationError("Cited financial fact field_name is required.")
        if not self.value.is_finite() or not self.unit_multiplier.is_finite():
            raise IpoValidationError("Cited financial fact decimals must be finite.")
        if self.unit_multiplier <= 0:
            raise IpoValidationError("Cited financial fact unit multiplier must be positive.")
        digest = self.document_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise IpoValidationError("Cited financial fact document SHA-256 is invalid.")
        if self.page_number < 1:
            raise IpoValidationError("Cited financial fact page number must be positive.")
        if not self.location.strip() or not self.source_token.strip():
            raise IpoValidationError("Cited financial fact source identity is required.")
        object.__setattr__(self, "document_sha256", digest)
        object.__setattr__(self, "confidence", Confidence(self.confidence))
        object.__setattr__(
            self,
            "verification_reasons",
            tuple(
                str(reason).strip()
                for reason in self.verification_reasons
                if str(reason).strip()
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return a JSON-safe, lossless proposal representation.

        Beginner note:
            ``Decimal`` and ``date`` are encoded as strings so database JSON
            storage never introduces binary floating-point rounding or
            locale-dependent date formatting.
        """
        return {
            "field_name": self.field_name,
            "value": str(self.value),
            "unit": self.unit,
            "unit_multiplier": str(self.unit_multiplier),
            "period_end": self.period_end.isoformat() if self.period_end else None,
            "document_sha256": self.document_sha256,
            "page_number": self.page_number,
            "location": self.location,
            "source_token": self.source_token,
            "confidence": self.confidence.value,
            "verification_reasons": list(self.verification_reasons),
        }


@dataclass(frozen=True)
class CitedTextEvidence:
    """One exact source span bound to a narrative proposal field.

    Beginner note:
        A page number alone does not prove that model-written prose came from
        the prospectus. This host-created object preserves the original line or
        table cell so approval can reject invented narrative evidence.
    """

    field_name: str
    document_sha256: str
    page_number: int
    location: str
    source_text: str
    confidence: Confidence
    verification_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate and normalize the immutable narrative source identity.

        Beginner note:
            Narrative evidence has no numeric equality check, so its document
            digest, page, location, and original source text must all survive as
            one inseparable record.
        """
        if not self.field_name.strip():
            raise IpoValidationError("Cited text evidence field_name is required.")
        digest = self.document_sha256.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise IpoValidationError("Cited text evidence document SHA-256 is invalid.")
        if self.page_number < 1:
            raise IpoValidationError("Cited text evidence page number must be positive.")
        if not self.location.strip() or not self.source_text.strip():
            raise IpoValidationError("Cited text evidence source identity is required.")
        object.__setattr__(self, "document_sha256", digest)
        object.__setattr__(self, "confidence", Confidence(self.confidence))
        object.__setattr__(
            self,
            "verification_reasons",
            tuple(
                str(reason).strip()
                for reason in self.verification_reasons
                if str(reason).strip()
            ),
        )

    def to_payload(self) -> dict[str, Any]:
        """Return the JSON-safe, provenance-preserving representation.

        Beginner note:
            The original source span is serialized with its citation rather
            than replaced by model-written prose, enabling approval-time
            revalidation against cached PDF bytes.
        """
        return {
            "field_name": self.field_name,
            "document_sha256": self.document_sha256,
            "page_number": self.page_number,
            "location": self.location,
            "source_text": self.source_text,
            "confidence": self.confidence.value,
            "verification_reasons": list(self.verification_reasons),
        }


class DebtReductionPurposeStatus(enum.StrEnum):
    """Type the conclusion about whether issue proceeds reduce borrowings.

    Beginner note:
        Four states prevent missing or unclear text from being interpreted as
        affirmative debt repayment. Only ``AFFIRMATIVE`` can suppress the
        high-debt caution, and it additionally requires a complete citation.
    """

    AFFIRMATIVE = "affirmative"
    NEGATIVE = "negative"
    AMBIGUOUS = "ambiguous"
    MISSING = "missing"


@dataclass(frozen=True)
class DebtReductionPurposeEvidence:
    """Bind a debt-repayment conclusion to the approved prospectus passage.

    Beginner note:
        A caution flag must never clear because a loose word such as "repay"
        appeared somewhere in a document. Only ``AFFIRMATIVE`` evidence with
        an immutable document hash, page, and span token has that authority.
    """

    status: DebtReductionPurposeStatus
    source_content_sha256: str | None = None
    page_number: int | None = None
    text_span_identity: str | None = None
    evidence_text: str | None = None
    verification_reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Normalize the conclusion and enforce citations for affirmative use.

        Beginner note:
            Non-affirmative states may retain partial context for review, but an
            affirmative claim must be fully bound. This asymmetry is
            intentional because only affirmative evidence changes the
            high-debt caution outcome.
        """
        object.__setattr__(
            self,
            "status",
            _parse_enum(
                self.status,
                DebtReductionPurposeStatus,
                "debt reduction purpose status",
            ),
        )
        digest = (
            str(self.source_content_sha256).strip().lower()
            if self.source_content_sha256 is not None
            else None
        )
        if digest is not None and not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise IpoValidationError(
                "Debt-reduction evidence document SHA-256 is invalid."
            )
        if self.page_number is not None and self.page_number < 1:
            raise IpoValidationError(
                "Debt-reduction evidence page number must be positive."
            )
        span = (
            str(self.text_span_identity).strip()
            if self.text_span_identity is not None
            else None
        )
        evidence = (
            str(redact_text(str(self.evidence_text).strip()))
            if self.evidence_text is not None
            else None
        )
        reasons = tuple(
            str(redact_text(str(reason).strip()))
            for reason in self.verification_reasons
            if str(reason).strip()
        )
        if self.status is DebtReductionPurposeStatus.AFFIRMATIVE and (
            digest is None
            or self.page_number is None
            or not span
            or not evidence
        ):
            raise IpoValidationError(
                "Affirmative debt-reduction evidence requires document, page, "
                "span, and evidence text."
            )
        object.__setattr__(self, "source_content_sha256", digest)
        object.__setattr__(self, "text_span_identity", span or None)
        object.__setattr__(self, "evidence_text", evidence or None)
        object.__setattr__(self, "verification_reasons", reasons)


class FinancialPeriodType(enum.StrEnum):
    """Name the financial statement periods supported by manual records.

    Beginner note:
        Automated IPO-010 evidence accepts annual history for scoring; the
        broader domain enum retains quarterly support for existing manually
        entered financial records.
    """

    ANNUAL = "annual"
    QUARTERLY = "quarterly"


class Recommendation(enum.StrEnum):
    """Define the deliberately binary public IPO decision contract.

    Beginner note:
        Nuance belongs in ``recommendation_type``, confidence, cautions, and the
        seven-factor breakdown. Keeping this top-level verdict binary prevents
        ambiguous “maybe” states in filters and automation.
    """

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


class IpoEvidenceAuthority(enum.StrEnum):
    """Name the tiers used by the central enrichment precedence policy.

    Beginner note:
        Authority describes what a source may influence, not how persuasive
        its wording sounds. Search results remain advisory even when confident;
        official or approved-manual evidence has the stronger role required
        for hard cautions.
    """

    ADVISORY = "advisory"
    OFFICIAL = "official"
    APPROVED_MANUAL = "approved_manual"


class IpoEnrichmentBatchUsability(enum.StrEnum):
    """Describe whether a web-result batch remains safe after quarantine.

    Beginner note:
        A mixed batch can be ``PARTIAL`` so clean siblings survive one hostile
        result. ``NOT_EVALUABLE`` means no safe evidence remained and must not
        be silently interpreted as an absence of risk.
    """

    USABLE = "usable"
    PARTIAL = "partial"
    NOT_EVALUABLE = "not_evaluable"


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
    """Hold one hard caution outcome and its deterministic evidence line.

    Beginner note:
        Every rule returns a record, including rules that did not trigger or
        lacked enough evidence. This makes a historical verdict auditable and
        avoids storing only the alarming outcomes.
    """

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
        """Return fired flags while preserving fixed policy-catalog order.

        Beginner note:
            This is a display convenience over the complete report; it does
            not erase ``NOT_EVALUABLE`` outcomes from the stored audit receipt.
        """
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

    Beginner note:
        This distinction flows all the way to recommendation policy. Missing
        critical evidence fails closed, whereas a verified zero participates
        in weighted arithmetic as an intentionally weak factor.
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

    Beginner note:
        This object is a pure scoring DTO: it contains no database identifiers
        or mutable ORM rows. The same value always produces the same arithmetic
        receipt.
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
class ScoreBreakdownItem:
    """Record one factor's score, weight, contribution, and evidence.

    Beginner note:
        All seven rows are stored even when a factor is missing. That makes the
        displayed total reproducible: known contributions sum exactly to the
        score, while a missing factor is explicit rather than disappearing.
    """

    factor: str
    weight: int
    normalized_score: Decimal | None
    missing: bool
    weighted_contribution: Decimal
    evidence_reason: str | None

    def __post_init__(self) -> None:
        """Normalize arithmetic and reject internally contradictory rows.

        Beginner note:
            ``missing`` must agree with the absence of a normalized score, and
            a contribution cannot exceed its weight. Enforcing those relations
            here protects every serializer and UI consumer from malformed
            receipts.
        """
        factor = str(self.factor).strip()
        if not factor:
            raise IpoValidationError("Score breakdown factor is required.")
        if not isinstance(self.weight, int) or isinstance(self.weight, bool):
            raise IpoValidationError("Score breakdown weight must be an integer.")
        if self.weight < 0 or self.weight > 100:
            raise IpoValidationError("Score breakdown weight must be from 0 to 100.")
        score = (
            _score_decimal(self.normalized_score)
            if self.normalized_score is not None
            else None
        )
        missing = bool(self.missing)
        if missing != (score is None):
            raise IpoValidationError(
                "Score breakdown missing must match the absence of normalized_score."
            )
        try:
            contribution = Decimal(str(self.weighted_contribution))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise IpoValidationError(
                "Score breakdown contribution must be numeric."
            ) from exc
        if (
            not contribution.is_finite()
            or contribution < 0
            or contribution > Decimal(self.weight)
        ):
            raise IpoValidationError(
                "Score breakdown contribution must be finite and within its weight."
            )
        reason = (
            str(redact_text(str(self.evidence_reason).strip()))
            if self.evidence_reason is not None
            else None
        )
        object.__setattr__(self, "factor", factor)
        object.__setattr__(self, "normalized_score", score)
        object.__setattr__(self, "missing", missing)
        object.__setattr__(
            self,
            "weighted_contribution",
            contribution.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP),
        )
        object.__setattr__(self, "evidence_reason", reason or None)

    def to_dict(self) -> dict[str, Any]:
        """Return the additive public JSON receipt for this factor.

        Beginner note:
            Whole-number decimals remain JSON integers for backward-friendly
            output, while fractional values are retained when scoring produces
            them.
        """

        def _number(value: Decimal | None) -> int | float | None:
            """Preserve whole numbers while keeping fractional JSON values."""
            if value is None:
                return None
            return (
                int(value)
                if value == value.to_integral_value()
                else float(value)
            )

        return {
            "factor": self.factor,
            "weight": self.weight,
            "normalized_score": _number(self.normalized_score),
            "missing": self.missing,
            "weighted_contribution": _number(self.weighted_contribution),
            "evidence_reason": self.evidence_reason,
        }


@dataclass(frozen=True)
class IpoScoreResult:
    """Preserve the numeric receipt before recommendation policy is applied."""

    company_name: str
    score: Decimal
    contributions: Mapping[str, Decimal]
    reasons: tuple[str, ...]
    missing_data: tuple[str, ...]
    source_documents: tuple[str, ...]
    breakdown: tuple[ScoreBreakdownItem, ...] = ()

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
    breakdown: tuple[ScoreBreakdownItem, ...] = ()

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
            "breakdown": [item.to_dict() for item in self.breakdown],
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
    """Expose one detached, immutable financial-period row.

    Beginner note:
        ``metrics`` may contain provider data. Freezing the top-level mapping
        after the SQLAlchemy session closes prevents callers from accidentally
        rewriting the repository's detached read model.
    """

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
        """Prevent mutation of metrics returned from a closed ORM session.

        The copy severs any reference to the ORM JSON value before it is
        wrapped in a read-only mapping proxy.
        """
        object.__setattr__(self, "metrics", MappingProxyType(dict(self.metrics)))


@dataclass(frozen=True)
class IpoSubscriptionData:
    """Validate a point-in-time subscription-demand snapshot.

    Beginner note:
        Demand multiples are observations that change during the offer window.
        Each capture is append-only, timezone-aware, and exact to two decimal
        places so scoring can select the newest record deterministically.
    """

    captured_at: dt.datetime
    source_confidence: Confidence
    qib_multiple: Decimal | None = None
    nii_multiple: Decimal | None = None
    retail_multiple: Decimal | None = None
    total_multiple: Decimal | None = None
    source_url: str | None = None

    def __post_init__(self) -> None:
        """Normalize UTC capture time and non-negative demand multiples.

        ``None`` remains distinct from numeric zero: the former means the
        provider omitted a category, while the latter is known weak demand.
        """
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
    """Expose one detached, immutable subscription snapshot.

    Beginner note:
        A record preserves its capture time and source confidence so factor
        derivation never needs a live network request or a mutable ORM session.
    """

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
    authority: IpoEvidenceAuthority = IpoEvidenceAuthority.ADVISORY
    corroborated: bool = False
    authority_policy_version: str = "ipo-enrichment-authority-v2"
    batch_usability: IpoEnrichmentBatchUsability = (
        IpoEnrichmentBatchUsability.PARTIAL
    )
    semantic_hash: str | None = None

    def __post_init__(self) -> None:
        """Normalize, bound, and freeze one enrichment insert payload.

        Beginner note:
            Search data crosses an external-input boundary. Normalizing enums,
            bounding text, freezing payload items, and validating semantic
            hashes here ensures every persistence caller receives the same
            authority and size rules.
        """
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
        object.__setattr__(
            self,
            "authority",
            _parse_enum(self.authority, IpoEvidenceAuthority, "authority"),
        )
        object.__setattr__(self, "corroborated", bool(self.corroborated))
        policy_version = str(self.authority_policy_version).strip()
        if not policy_version or len(policy_version) > 48:
            raise IpoValidationError(
                "authority_policy_version must contain 1 to 48 characters."
            )
        object.__setattr__(self, "authority_policy_version", policy_version)
        object.__setattr__(
            self,
            "batch_usability",
            _parse_enum(
                self.batch_usability,
                IpoEnrichmentBatchUsability,
                "batch_usability",
            ),
        )
        if self.semantic_hash is not None:
            digest = str(self.semantic_hash).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise IpoValidationError("semantic_hash must be a SHA-256 digest.")
            object.__setattr__(self, "semantic_hash", digest)


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
    authority: IpoEvidenceAuthority = IpoEvidenceAuthority.ADVISORY
    corroborated: bool = False
    authority_policy_version: str = "ipo-enrichment-authority-v1"
    batch_usability: IpoEnrichmentBatchUsability = (
        IpoEnrichmentBatchUsability.PARTIAL
    )
    semantic_hash: str | None = None
    first_seen_at: dt.datetime | None = None
    last_seen_at: dt.datetime | None = None

    def __post_init__(self) -> None:
        """Freeze payload entries and normalize persisted policy enums.

        Beginner note:
            SQLAlchemy rows are mutable session objects. The repository exposes
            this detached immutable shape instead, so callers cannot rewrite
            stored evidence by mutating a nested result dictionary.
        """
        object.__setattr__(
            self,
            "payload",
            tuple(MappingProxyType(dict(entry)) for entry in self.payload),
        )
        object.__setattr__(
            self,
            "authority",
            _parse_enum(self.authority, IpoEvidenceAuthority, "authority"),
        )
        object.__setattr__(
            self,
            "batch_usability",
            _parse_enum(
                self.batch_usability,
                IpoEnrichmentBatchUsability,
                "batch_usability",
            ),
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
    document_id: int | None
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
    evidence_schema_version: str = "legacy-unbound/v0"
    semantic_fingerprint: str | None = None

    def __post_init__(self) -> None:
        """Freeze the proposed payload so a detached record stays read-only."""
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))


@dataclass(frozen=True)
class IpoEvaluationRecord:
    """Detached immutable score/recommendation pair.

    ``inputs_fingerprint`` (IPO-006) is the SHA-256 of exactly the evidence the
    scoring service consumed; legacy ipo-001-v1 rows carry ``None``.
    ``contributions`` restores the per-factor weighted points from the stored
    receipt so the dashboard can rank strengths and risks without re-scoring.

    Beginner note:
        The semantic input fingerprint links a verdict to the exact evidence
        snapshot it consumed. Concurrent jobs can reuse one winning immutable
        evaluation, while the dashboard independently reports newer evidence
        as stale.
    """

    issue_id: int
    score_id: int
    recommendation_id: int
    model_version: str
    scored_at: dt.datetime
    result: IpoRecommendationResult
    inputs_fingerprint: str | None = None
    contributions: Mapping[str, Decimal] = dataclasses.field(default_factory=dict)

    def __post_init__(self) -> None:
        """Freeze the contribution mapping so the audit receipt stays read-only.

        Beginner note:
            A frozen dataclass does not recursively freeze a normal dictionary.
            Copying it into a mapping proxy prevents later UI or job code from
            altering the arithmetic attached to a historical evaluation.
        """
        object.__setattr__(
            self, "contributions", MappingProxyType(dict(self.contributions))
        )
