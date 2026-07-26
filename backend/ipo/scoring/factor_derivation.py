"""Derive the seven 0-100 factor scores from typed, verified IPO evidence.

This is the bridge the IPO-001 design deferred as "a later ticket": it maps
IPO-005 ratio receipts, the human-verified manual extraction, the latest
official subscription snapshot, and (optionally) low-confidence IPO-009 web
signals into the seven ``FactorAssessment`` values the deterministic scorecard
consumes. It performs no I/O and never talks to a database, network, or model.

Beginner note:
The single most important rule here is the None-versus-zero distinction. A
factor score of ``None`` means "the evidence needed to judge this is absent"
and later forces the fail-closed verdict path. A score of ``0`` means "the
evidence exists and it is bad" — a negative CAGR, an undersubscribed book, a
grey-market discount. Collapsing those two states would let missing data
masquerade as a judged company, which is exactly what this pipeline exists to
prevent.
"""

from __future__ import annotations

import datetime as dt
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Final

from backend.ipo.financials.ratio_engine import (
    IpoRatioAnalysis,
    IpoRatioName,
    IpoRatioReceipt,
    IpoRatioStatus,
)
from backend.ipo.manual_extraction import IpoManualExtractionRecord, IpoPeerMetric
from backend.ipo.models import (
    DebtReductionPurposeEvidence,
    DebtReductionPurposeStatus,
    FactorAssessment,
    IpoEnrichmentBatchUsability,
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
    IpoIssueRecord,
    IpoScoreInput,
    IpoSubscriptionRecord,
)

FACTOR_MODEL_VERSION: Final = "ipo-006-factors-v2"

# GMP chatter goes stale fast around an issue window; older observations are
# ignored entirely rather than down-weighted so staleness cannot fabricate a
# sentiment score.
GMP_SIGNAL_MAX_AGE_DAYS: Final = 5

_TWO_PLACES = Decimal("0.01")

# One band row: (inclusive lower bound, exclusive upper bound, sub-score).
# ``None`` marks an unbounded side, and every table below covers the whole
# real line so a lookup can never fall through.
_Band = tuple[Decimal | None, Decimal | None, Decimal]


def _bands(*rows: tuple[str | None, str | None, str]) -> tuple[_Band, ...]:
    """Declare one half-open band table from exact decimal strings.

    Beginner note:
        Writing thresholds as strings (``"0.8"``) instead of floats keeps every
        boundary exact. ``Decimal(0.8)`` would inherit binary floating-point
        noise and silently shift the band edge.
    """
    return tuple(
        (
            Decimal(lower) if lower is not None else None,
            Decimal(upper) if upper is not None else None,
            Decimal(score),
        )
        for lower, upper, score in rows
    )


# Threshold tables are versioned constants: any edit must bump
# FACTOR_MODEL_VERSION so stored evaluations remain attributable to the exact
# rules that produced them.
GROWTH_BANDS: Final = _bands(
    ("25", None, "100"), ("15", "25", "75"), ("8", "15", "50"), ("0", "8", "25"), (None, "0", "0")
)
RETURN_BANDS: Final = _bands(
    ("20", None, "100"), ("15", "20", "75"), ("10", "15", "50"), ("5", "10", "25"), (None, "5", "0")
)
VALUATION_PREMIUM_BANDS: Final = _bands(
    (None, "0.8", "100"),
    ("0.8", "1.0", "80"),
    ("1.0", "1.2", "60"),
    ("1.2", "1.5", "35"),
    ("1.5", None, "10"),
)
EBITDA_MARGIN_BANDS: Final = _bands(
    ("25", None, "100"), ("18", "25", "75"), ("12", "18", "50"), ("6", "12", "25"), (None, "6", "0")
)
PAT_MARGIN_BANDS: Final = _bands(
    ("15", None, "100"), ("10", "15", "75"), ("6", "10", "50"), ("2", "6", "25"), (None, "2", "0")
)
CFO_TO_PAT_BANDS: Final = _bands(
    ("1", None, "100"),
    ("0.8", "1", "75"),
    ("0.5", "0.8", "50"),
    ("0.2", "0.5", "25"),
    (None, "0.2", "0"),
)
INTEREST_COVERAGE_BANDS: Final = _bands(
    ("8", None, "100"), ("5", "8", "75"), ("3", "5", "50"), ("1.5", "3", "25"), (None, "1.5", "0")
)
PROMOTER_HOLDING_BANDS: Final = _bands(
    ("60", None, "100"), ("50", "60", "80"), ("40", "50", "60"), ("30", "40", "40"), (None, "30", "20")
)
# A zero OFS share (all-fresh issue) and a 100% OFS share are handled as exact
# endpoints in ``_promoter_quality``; this table bands everything in between.
OFS_FRACTION_BANDS: Final = _bands(
    (None, "0.25", "80"), ("0.25", "0.5", "60"), ("0.5", "0.75", "30"), ("0.75", None, "10")
)
QIB_BANDS: Final = _bands(
    ("50", None, "100"),
    ("20", "50", "85"),
    ("10", "20", "70"),
    ("3", "10", "55"),
    ("1", "3", "35"),
    (None, "1", "0"),
)
GMP_BANDS: Final = _bands(
    ("40", None, "100"), ("20", "40", "75"), ("10", "20", "60"), ("0", "10", "40"), (None, "0", "0")
)


@dataclass(frozen=True)
class IpoFactorInputs:
    """Everything factor derivation may look at — nothing else exists for it.

    Beginner note:
    Collecting the evidence into one frozen bundle makes the derivation a pure
    function: the caller (job or dashboard) loads records, and this module
    only reads them. ``as_of`` is an injected clock so recency rules such as
    the GMP staleness window are testable and reproducible.
    """

    issue: IpoIssueRecord
    profile: IpoManualExtractionRecord | None
    ratios: IpoRatioAnalysis | None
    subscription: IpoSubscriptionRecord | None
    as_of: dt.datetime
    enrichment: tuple[IpoEnrichmentSignalRecord, ...] = ()
    debt_reduction_purpose: DebtReductionPurposeEvidence | None = None
    source_documents: tuple[str, ...] = ()


_DEBT_PURPOSE_PATTERN: Final = re.compile(
    r"\b(?:"
    r"repay(?:ment|ing)?|prepay(?:ment|ing)?|redemption|"
    r"deleverag(?:e|ing)|"
    r"(?:reduction|reduce)\s+(?:of\s+)?(?:debt|borrowings?|loans?)"
    r")\b",
    re.IGNORECASE,
)
_DEBT_CONTEXT_PATTERN: Final = re.compile(
    r"\b(?:debt|borrowings?|loans?|credit facilities)\b",
    re.IGNORECASE,
)
_DEBT_PURPOSE_PROPOSITION_BOUNDARY_PATTERN: Final = re.compile(
    r"(?<=[.;!?])\s+|,\s+\b(?:and|but|or|nor|yet|so|however|whereas)\b\s+(?=(?:"
    r"the\s+(?:net\s+)?proceeds?|the\s+(?:company|issue|funds?)|they|it|we|"
    r"no\s+(?:portion|part)|not|will|shall|may|can|could|should|would|do|does|did|"
    r"is|are|was|were|has|have)\b)|"
    r"\s+\b(?:and|but|or|nor|yet|so|however|whereas)\b\s+(?=(?:"
    r"the\s+(?:net\s+)?proceeds?|the\s+(?:company|issue|funds?)|they|it|we|"
    r"no\s+(?:portion|part)|not|will|shall|may|can|could|should|would|do|does|did|"
    r"is|are|was|were|has|have)\b)",
    re.IGNORECASE,
)
_NOT_ONLY_PATTERN: Final = re.compile(r"\bnot\s+only\b", re.IGNORECASE)
_NEGATIVE_DEBT_PURPOSE_PATTERN: Final = re.compile(
    r"\b(?:"
    r"no|not|never|without|neither|nor|"
    r"exclud(?:e|ed|es|ing)?|"
    r"prohibit(?:ed|s|ing)?|forbid(?:den|s|ding)?|bar(?:red|s|ring)?|"
    r"cannot|can't|won't|shall\s+not|will\s+not|may\s+not|"
    r"(?:do|does|did|has|have|was|were|should|would|could|must|might|need|is|are)n't|"
    r"is(?:n't|\s+not)|are(?:n't|\s+not)"
    r")\b",
    re.IGNORECASE,
)
_APOSTROPHE_VARIANT_TRANSLATION: Final = str.maketrans({"\u2018": "'", "\u2019": "'", "\u02BC": "'"})


def _classify_debt_purpose_proposition(proposition: str) -> DebtReductionPurposeStatus:
    """Classify one complete repayment proposition as affirmative or negative.

    Beginner note:
        A negator can govern a repayment word from either side of a sentence,
        so a fixed character window is not a safe proxy for meaning.  This
        deliberately conservative classifier examines the governing proposition;
        any recognized denial or prohibition makes the proposition negative.
    """
    proposition_without_not_only = _NOT_ONLY_PATTERN.sub("", proposition)
    if _NEGATIVE_DEBT_PURPOSE_PATTERN.search(proposition_without_not_only):
        return DebtReductionPurposeStatus.NEGATIVE
    return DebtReductionPurposeStatus.AFFIRMATIVE


def derive_debt_reduction_purpose_evidence(
    profile: IpoManualExtractionRecord | None,
) -> DebtReductionPurposeEvidence | None:
    """Classify the approved objects-of-issue span into a cited typed fact.

    Beginner note:
        This parser is intentionally narrow. It recognizes explicit repayment
        language in complete sentences or coordinated propositions, while
        ambiguous debt references fail closed. The caution rule consumes only
        this typed conclusion and never performs its own substring search.
    """
    if profile is None:
        return None
    text = " ".join(profile.objects_of_issue.split())
    classification_text = text.translate(_APOSTROPHE_VARIANT_TRANSLATION)
    source_sha256 = profile.source_content_sha256
    page_number = profile.objects_of_issue_page
    span_identity = f"objects_of_issue:p{page_number}"
    matches = list(_DEBT_PURPOSE_PATTERN.finditer(classification_text))
    if not matches:
        status = (
            DebtReductionPurposeStatus.AMBIGUOUS
            if _DEBT_CONTEXT_PATTERN.search(classification_text)
            else DebtReductionPurposeStatus.MISSING
        )
        reason = (
            "Debt is mentioned without an explicit repayment purpose."
            if status is DebtReductionPurposeStatus.AMBIGUOUS
            else "No explicit debt-reduction purpose was found."
        )
        return DebtReductionPurposeEvidence(
            status=status,
            source_content_sha256=source_sha256,
            page_number=page_number,
            text_span_identity=span_identity,
            evidence_text=text,
            verification_reasons=(reason,),
        )

    proposition_statuses = [
        _classify_debt_purpose_proposition(proposition)
        for proposition in _DEBT_PURPOSE_PROPOSITION_BOUNDARY_PATTERN.split(classification_text)
        if _DEBT_PURPOSE_PATTERN.search(proposition)
    ]
    if len(set(proposition_statuses)) > 1:
        status = DebtReductionPurposeStatus.AMBIGUOUS
        reason = "The cited passage contains conflicting repayment statements."
    elif proposition_statuses[0] is DebtReductionPurposeStatus.AFFIRMATIVE:
        status = DebtReductionPurposeStatus.AFFIRMATIVE
        reason = "Explicit non-negated debt-reduction purpose verified."
    else:
        status = DebtReductionPurposeStatus.NEGATIVE
        reason = "Repayment language is explicitly negated."
    return DebtReductionPurposeEvidence(
        status=status,
        source_content_sha256=source_sha256,
        page_number=page_number,
        text_span_identity=span_identity,
        evidence_text=text,
        verification_reasons=(reason,),
    )


@dataclass(frozen=True)
class _SubScore:
    """One factor sub-input's outcome: a banded score or an explained absence."""

    label: str
    score: Decimal | None
    note: str


def _fmt(value: Decimal) -> str:
    """Render one decimal at two places so reason strings stay deterministic."""
    return str(value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP))


def _band(value: Decimal, bands: tuple[_Band, ...]) -> Decimal:
    """Look one value up in a half-open band table (``lower <= x < upper``)."""
    for lower, upper, score in bands:
        if (lower is None or value >= lower) and (upper is None or value < upper):
            return score
    raise LookupError("Band tables must cover the whole real line.")


def _receipt(
    ratios: IpoRatioAnalysis | None, name: IpoRatioName
) -> IpoRatioReceipt | None:
    """Fetch one ratio receipt, treating an absent snapshot as absent evidence."""
    if ratios is None:
        return None
    return ratios.ratios.get(name)


def _ratio_subscore(
    ratios: IpoRatioAnalysis | None,
    name: IpoRatioName,
    bands: tuple[_Band, ...],
    *,
    label: str,
    weak_when_undefined: bool = True,
) -> _SubScore:
    """Turn one ratio receipt into a banded sub-score or an explained absence.

    Beginner note:
        The receipt status drives the None-versus-zero rule. ``computed`` gets
        banded; ``undefined`` usually means the denominator told a bad story (a
        loss-base CAGR, negative net worth) and scores a known-weak zero with
        the engine's own explanation quoted; every other status means the
        evidence is absent and the sub-score stays ``None``.
    """
    receipt = _receipt(ratios, name)
    if receipt is None or receipt.status is IpoRatioStatus.MISSING_INPUTS:
        return _SubScore(label=label, score=None, note=f"{label} unavailable")
    if receipt.status is IpoRatioStatus.COMPUTED and receipt.value is not None:
        banded = _band(receipt.value, bands)
        return _SubScore(
            label=label,
            score=banded,
            note=f"{label} {_fmt(receipt.value)} -> {banded}",
        )
    if receipt.status is IpoRatioStatus.UNDEFINED and weak_when_undefined:
        explanation = receipt.explanation or "undefined by its inputs"
        return _SubScore(
            label=label, score=Decimal(0), note=f"{label} treated as weak: {explanation}"
        )
    explanation = receipt.explanation or receipt.status.value
    return _SubScore(label=label, score=None, note=f"{label} unavailable: {explanation}")


def _factor(
    label: str,
    core: list[_SubScore],
    optional: list[_SubScore],
    provenance: str,
) -> FactorAssessment:
    """Average available sub-scores into one factor, or explain the gap.

    Every core sub-input must carry a score for the factor to be judged at
    all; optional sub-inputs join the average only when they scored. The
    reason string always names each contribution so the persisted receipt can
    be audited without re-running the derivation.

    Beginner note:
        This function never rescales around a missing core input. Returning
        ``None`` preserves the evidence gap so recommendation policy can fail
        closed instead of awarding a deceptively complete-looking score.
    """
    missing_core = [sub for sub in core if sub.score is None]
    if missing_core or not core:
        gaps = "; ".join(sub.note for sub in missing_core) or "no evidence on file"
        return FactorAssessment(score=None, reason=f"{label}: not scored; {gaps}.")

    used = list(core) + [sub for sub in optional if sub.score is not None]
    skipped = [sub for sub in optional if sub.score is None]
    total = sum((sub.score for sub in used if sub.score is not None), Decimal(0))
    mean = (total / Decimal(len(used))).quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)

    notes = ", ".join(sub.note for sub in used)
    reason = f"{label}: {notes}; factor {mean}/100."
    if skipped:
        reason += " Skipped: " + "; ".join(sub.note for sub in skipped) + "."
    if provenance:
        reason += f" {provenance}"
    return FactorAssessment(score=mean, reason=reason)


def _ratio_provenance(ratios: IpoRatioAnalysis | None) -> str:
    """Describe exactly which ratio snapshot and source bytes were consumed."""
    if ratios is None:
        return ""
    return (
        f"Source: ratio engine {ratios.formula_version}, "
        f"sha256 {ratios.source_content_sha256[:12]}."
    )


def _peer_median(
    profile: IpoManualExtractionRecord | None, metric: IpoPeerMetric
) -> Decimal | None:
    """Compute the median of one positive peer multiple, or ``None`` if unusable.

    Beginner note:
        Non-positive multiples (a peer with negative earnings has no meaningful
        P/E) are excluded before the median so one broken row cannot poison the
        denominator every premium is judged against.
    """
    if profile is None:
        return None
    values = sorted(
        peer.metrics[metric]
        for peer in profile.peers
        if metric in peer.metrics and peer.metrics[metric] > 0
    )
    if not values:
        return None
    middle = len(values) // 2
    if len(values) % 2 == 1:
        return values[middle]
    return (values[middle - 1] + values[middle]) / Decimal(2)


def _premium_subscore(
    ratios: IpoRatioAnalysis | None,
    profile: IpoManualExtractionRecord | None,
    name: IpoRatioName,
    metric: IpoPeerMetric,
    *,
    label: str,
) -> _SubScore:
    """Score one valuation multiple as a premium over its peer median.

    Beginner note:
        Peer comparisons use only positive, allowlisted metrics from the
        approved manual profile. A missing median remains missing evidence;
        the issuer is never rewarded merely because no peer data was supplied.
    """
    receipt = _receipt(ratios, name)
    if receipt is None or receipt.status is IpoRatioStatus.MISSING_INPUTS:
        return _SubScore(
            label=label, score=None, note=f"{label} unavailable (issue price or inputs missing)"
        )
    if receipt.status is IpoRatioStatus.UNDEFINED:
        explanation = receipt.explanation or "undefined by its inputs"
        return _SubScore(
            label=label, score=Decimal(0), note=f"{label} treated as weak: {explanation}"
        )
    if receipt.status is not IpoRatioStatus.COMPUTED or receipt.value is None:
        explanation = receipt.explanation or receipt.status.value
        return _SubScore(label=label, score=None, note=f"{label} unavailable: {explanation}")

    median = _peer_median(profile, metric)
    if median is None:
        return _SubScore(
            label=label,
            score=None,
            note=f"{label} unusable: no positive peer {metric.value} metrics on file",
        )
    premium = receipt.value / median
    banded = _band(premium, VALUATION_PREMIUM_BANDS)
    return _SubScore(
        label=label,
        score=banded,
        note=(
            f"{label} {_fmt(receipt.value)} vs peer {metric.value} median "
            f"{_fmt(median)} = {_fmt(premium)}x premium -> {banded}"
        ),
    )


def _promoter_quality(profile: IpoManualExtractionRecord | None) -> FactorAssessment:
    """Judge promoter alignment from post-issue holding and the OFS share.

    Beginner note:
        The factor uses approved pre/post holding and fresh-versus-OFS values,
        not promoter-reputation search snippets. External commentary remains
        advisory and cannot overwrite prospectus facts.
    """
    if profile is None:
        return FactorAssessment(
            score=None,
            reason="Promoter quality: not scored; no verified manual extraction on file.",
        )

    holding = profile.promoter_holding_post_issue
    holding_sub = _SubScore(
        label="post-issue promoter holding",
        score=_band(holding, PROMOTER_HOLDING_BANDS),
        note=(
            f"post-issue promoter holding {_fmt(holding)}% -> "
            f"{_band(holding, PROMOTER_HOLDING_BANDS)}"
        ),
    )

    canonical = profile.canonical_values
    fresh = canonical["fresh_issue_amount_inr"]
    ofs = canonical["ofs_amount_inr"]
    total = fresh + ofs
    optional: list[_SubScore] = []
    if total == 0:
        optional.append(
            _SubScore(
                label="offer-for-sale share",
                score=None,
                note="offer-for-sale share unavailable (zero issue amounts)",
            )
        )
    else:
        fraction = ofs / total
        if fraction == 0:
            ofs_score = Decimal(100)
            ofs_note = "all-fresh issue with zero OFS proceeds -> 100"
        elif fraction == 1:
            ofs_score = Decimal(0)
            ofs_note = (
                "entirely OFS with zero fresh-issue proceeds -> 0"
            )
        else:
            ofs_score = _band(fraction, OFS_FRACTION_BANDS)
            ofs_note = (
                f"offer-for-sale share {_fmt(fraction)} of issue -> {ofs_score}"
            )
        optional.append(
            _SubScore(
                label="offer-for-sale share",
                score=ofs_score,
                note=ofs_note,
            )
        )

    provenance = (
        f"Source: sha256 {profile.source_content_sha256[:12]}."
    )
    return _factor("Promoter quality", [holding_sub], optional, provenance)


def _qib_subscription(subscription: IpoSubscriptionRecord | None) -> FactorAssessment:
    """Judge institutional demand from the latest official snapshot.

    Beginner note:
        No snapshot means ``None``, while a real zero multiple means weak demand
        and scores zero. Keeping those states separate is essential near issue
        close, where missing demand can also trigger a hard caution.
    """
    if subscription is None:
        return FactorAssessment(
            score=None,
            reason="QIB subscription: not scored; no demand snapshot captured for this issue yet.",
        )
    if subscription.qib_multiple is None:
        return FactorAssessment(
            score=None,
            reason=(
                "QIB subscription: not scored; the latest demand snapshot "
                "lacks the QIB breakdown."
            ),
        )
    banded = _band(subscription.qib_multiple, QIB_BANDS)
    return FactorAssessment(
        score=banded,
        reason=(
            f"QIB subscription: QIB book {_fmt(subscription.qib_multiple)}x -> {banded}; "
            f"factor {banded}/100. Source: subscription snapshot captured "
            f"{subscription.captured_at.isoformat()}."
        ),
    )


def _gmp_sentiment(
    enrichment: tuple[IpoEnrichmentSignalRecord, ...], as_of: dt.datetime
) -> FactorAssessment:
    """Judge grey-market sentiment from recent, clean, parseable web signals.

    Beginner note:
        This is the only factor fed by IPO-009 web enrichment, and it stays on
        a short leash: quarantined rows, unparseable snippets, and stale
        captures are excluded outright. When nothing usable remains the factor
        is honestly missing instead of guessed.
    """
    cutoff = as_of - dt.timedelta(days=GMP_SIGNAL_MAX_AGE_DAYS)
    usable = sorted(
        signal.parsed_value
        for signal in enrichment
        if signal.signal_type is IpoEnrichmentSignalType.GMP
        and signal.batch_usability
        is not IpoEnrichmentBatchUsability.NOT_EVALUABLE
        and (
            not signal.quarantined
            or any(
                entry.get("quarantine_status") == "clean"
                for entry in signal.payload
            )
        )
        and signal.parsed_value is not None
        and (signal.last_seen_at or signal.captured_at) >= cutoff
    )
    if not usable:
        return FactorAssessment(
            score=None,
            reason=(
                "GMP sentiment: not scored; no recent parseable grey-market "
                "observations (low-confidence web source; never overrides "
                "document evidence)."
            ),
        )
    middle = len(usable) // 2
    median = (
        usable[middle]
        if len(usable) % 2 == 1
        else (usable[middle - 1] + usable[middle]) / Decimal(2)
    )
    banded = _band(median, GMP_BANDS)
    return FactorAssessment(
        score=banded,
        reason=(
            f"GMP sentiment: median grey-market premium {_fmt(median)}% of issue "
            f"price across {len(usable)} recent observations -> {banded}; factor "
            f"{banded}/100 (low-confidence web source; never overrides document "
            "evidence)."
        ),
    )


def derive_score_input(inputs: IpoFactorInputs) -> IpoScoreInput:
    """Derive the complete seven-factor scorecard input from typed evidence.

    Args:
        inputs: The frozen evidence bundle for one issue. Absent members simply
            leave their dependent factors missing; they never raise.

    Returns:
        An ``IpoScoreInput`` whose seven assessments each carry either a banded
        0-100 score or ``None`` plus a reason string explaining the gap, ready
        for ``score_ipo`` and ``build_recommendation``.

    Beginner note:
        The function is deliberately a straight-line assembly of per-factor
        helpers. There is no fallback logic and no cross-factor compensation:
        each factor sees only its own evidence, which keeps every number in the
        persisted receipt attributable to one rule in this module.
    """
    ratios = inputs.ratios
    provenance = _ratio_provenance(ratios)

    financial_growth = _factor(
        "Financial growth",
        [
            _ratio_subscore(
                ratios, IpoRatioName.REVENUE_CAGR, GROWTH_BANDS, label="revenue CAGR"
            ),
            _ratio_subscore(ratios, IpoRatioName.PAT_CAGR, GROWTH_BANDS, label="PAT CAGR"),
        ],
        [],
        provenance,
    )
    return_ratios = _factor(
        "Return ratios",
        [_ratio_subscore(ratios, IpoRatioName.ROE, RETURN_BANDS, label="ROE")],
        [_ratio_subscore(ratios, IpoRatioName.ROCE, RETURN_BANDS, label="ROCE")],
        provenance,
    )
    valuation = _factor(
        "Valuation",
        [
            _premium_subscore(
                ratios,
                inputs.profile,
                IpoRatioName.PRICE_TO_EARNINGS,
                IpoPeerMetric.PE,
                label="P/E",
            )
        ],
        [
            _premium_subscore(
                ratios,
                inputs.profile,
                IpoRatioName.EV_TO_EBITDA,
                IpoPeerMetric.EV_EBITDA,
                label="EV/EBITDA",
            )
        ],
        provenance,
    )
    business_quality = _factor(
        "Business quality",
        [
            _ratio_subscore(
                ratios, IpoRatioName.EBITDA_MARGIN, EBITDA_MARGIN_BANDS, label="EBITDA margin"
            ),
            _ratio_subscore(
                ratios, IpoRatioName.PAT_MARGIN, PAT_MARGIN_BANDS, label="PAT margin"
            ),
            _ratio_subscore(
                ratios, IpoRatioName.CFO_TO_PAT, CFO_TO_PAT_BANDS, label="CFO/PAT"
            ),
        ],
        [
            _ratio_subscore(
                ratios,
                IpoRatioName.INTEREST_COVERAGE,
                INTEREST_COVERAGE_BANDS,
                label="interest coverage",
            )
        ],
        provenance,
    )

    source_documents = inputs.source_documents
    if not source_documents and inputs.profile is not None:
        source_documents = (inputs.profile.source_document_url,)

    return IpoScoreInput(
        company_name=inputs.issue.company_name,
        business_quality=business_quality,
        financial_growth=financial_growth,
        return_ratios=return_ratios,
        valuation=valuation,
        qib_subscription=_qib_subscription(inputs.subscription),
        promoter_quality=_promoter_quality(inputs.profile),
        gmp_sentiment=_gmp_sentiment(inputs.enrichment, inputs.as_of),
        source_documents=source_documents,
    )
