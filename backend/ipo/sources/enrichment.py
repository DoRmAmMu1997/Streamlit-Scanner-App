"""IPO-009: low-confidence SerpAPI web enrichment for sentiment and red flags.

This adapter runs fixed discovery queries (GMP, news, promoter reputation,
litigation, anchor commentary, brokerage reviews, peer discovery) through the
shared SerpAPI client and persists what it finds as ``ipo_enrichment_signals``
rows. It lives under ``backend/ipo/sources`` because that package is the only
reviewed network zone in the IPO domain.

Beginner note — the trust rules, stated once:
Web search results can never override official documents, can never supply a
financial-statement number, and only feed the optional GMP/sentiment factor
plus the litigation caution flag. Those rules are structural, not polite
requests: signals are typed records with a stamped low confidence and source
policy, every snippet is prompt-injection scanned *before* storage (a hit is
replaced by the blocked-evidence marker), and nothing in this module can write
into the manual-extraction or ratio pipelines. If no ``SERPAPI_API_KEY`` is
configured the collector reports a graceful skip and the screener continues
exactly as before.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol

from backend.ipo.models import (
    Confidence,
    IpoEnrichmentBatchUsability,
    IpoEnrichmentSignalData,
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
    IpoEvidenceAuthority,
    IpoSubscriptionData,
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    SessionFactory,
    create_subscription,
    get_issue,
    get_latest_subscription,
    record_enrichment_signals,
)
from backend.observability import (
    EVENT_IPO_ENRICHMENT_COMPLETED,
    EVENT_IPO_ENRICHMENT_FAILED,
    EVENT_IPO_ENRICHMENT_SKIPPED,
    log_event,
)
from backend.security import (
    BLOCKED_EVIDENCE_TEXT,
    contains_injection,
    normalize_external_text,
)
from backend.sixty_seven.search_client import (
    SearchResult,
    SerpApiClient,
    SerpApiQuotaError,
    SerpApiSearchError,
    SerpApiSetupError,
)
from backend.storage import session_scope

logger = logging.getLogger(__name__)

ENRICHMENT_SOURCE_POLICY: Final = "serpapi-low-confidence-v2"
ENRICHMENT_AUTHORITY_POLICY_VERSION: Final = "ipo-enrichment-authority-v2"

# One fixed, deterministic query template per signal type. Templates only ever
# interpolate the company name, so a run's queries are reproducible provenance.
_QUERY_TEMPLATES: Final[dict[IpoEnrichmentSignalType, str]] = {
    IpoEnrichmentSignalType.GMP: "{company} IPO GMP grey market premium",
    IpoEnrichmentSignalType.NEWS: "{company} IPO news",
    IpoEnrichmentSignalType.PROMOTER_REPUTATION: "{company} promoters background reputation",
    IpoEnrichmentSignalType.LITIGATION_RED_FLAG: (
        "{company} litigation investigation auditor qualification"
    ),
    IpoEnrichmentSignalType.ANCHOR_COMMENTARY: "{company} IPO anchor investors",
    IpoEnrichmentSignalType.BROKERAGE_REVIEW: "{company} IPO review recommendation brokerage",
    IpoEnrichmentSignalType.PEER_DISCOVERY: "{company} listed peers comparison",
    IpoEnrichmentSignalType.SUBSCRIPTION_DEMAND: (
        "{company} IPO subscription status QIB subscribed times"
    ),
}

# Case-folded fragments that count as litigation/reputation red flags. Only
# these recorded matches — never snippet text — reach the caution-flag layer.
RED_FLAG_KEYWORDS: Final = (
    "auditor qualification",
    "default",
    "fraud",
    "insolvency",
    "investigation",
    "litigation",
    "penalty",
    "probe",
    "sebi order",
)

# Conservative GMP extraction: a text must actually mention GMP before any
# number in it is trusted, percent readings win over rupee readings, and a
# rupee reading is only convertible when the issue price is known.
_GMP_TERM_PATTERN: Final = re.compile(
    r"\b(?:gmp|grey\s+market\s+premium)\b", re.IGNORECASE
)
# IPO-011: the QIB demand anchor. Requiring an explicit QIB/institutional term
# keeps a retail or overall subscription figure from being read as QIB demand.
_QIB_TERM_PATTERN: Final = re.compile(
    r"\b(?:qib|qibs|qualified\s+institutional(?:\s+buyers?)?)\b", re.IGNORECASE
)
# IPO-011 hardening: the other subscription categories a status headline prints
# alongside QIB. A multiple counts as institutional demand only when the QIB
# anchor is strictly closer to it than every one of these, which is what stops
# "Retail 25.6 times, QIB 1.2 times" being recorded as a 25.6x QIB book.
_COMPETING_CATEGORY_PATTERN: Final = re.compile(
    r"\b(?:retail|rii|nii|hni|non[-\s]?institutional|employee|shareholder|"
    r"overall|total|anchor)\b",
    re.IGNORECASE,
)
# "2.5x", "2.5 times", "subscribed 2.5 time(s)".
_SUBSCRIPTION_MULTIPLE_PATTERN: Final = re.compile(
    r"(\d{1,4}(?:\.\d+)?)\s*(?:x\b|times?\b)", re.IGNORECASE
)
_PERCENT_PATTERN: Final = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*%")
_RUPEE_PATTERN: Final = re.compile(
    r"(?:₹|rs\.?|inr)\s*(-?\d{1,4}(?:\.\d+)?)", re.IGNORECASE
)
_RUPEE_ABBREVIATION_PATTERN: Final = re.compile(
    r"\b(rs)\.(?=\s*-?\d)", re.IGNORECASE
)
_CLAUSE_SPLIT_PATTERN: Final = re.compile(r"[;\n!?]+|(?<!\d)\.|\.(?!\d)")
# A subscription-status headline lists its categories comma- or pipe-separated
# ("Retail 25.6 times, QIB 1.2 times"), so those punctuation marks end a clause
# here even though they do not for the GMP prose the original splitter serves.
_CATEGORY_SPLIT_PATTERN: Final = re.compile(r"[;,\n!?|/]+|(?<!\d)\.|\.(?!\d)")
_NEGATION_PATTERN: Final = re.compile(
    r"\b(?:no|not|never|without|den(?:y|ies|ied)|dismiss(?:ed|al)?|"
    r"clear(?:ed)?|exonerat(?:ed|ion)|withdrawn)\b",
    re.IGNORECASE,
)

_TWO_PLACES = Decimal("0.01")


def _normalize_enrichment_text(value: str) -> str:
    """Normalize web text without erasing newline clause boundaries.

    Beginner note:
        The shared normalizer intentionally collapses whitespace, including
        newlines. Converting line breaks to an explicit clause delimiter first
        keeps two provider claims separate during later GMP parsing.
    """
    clause_aware = re.sub(r"[\r\n]+", "; ", value)
    return normalize_external_text(clause_aware)


class SupportsIpoSearch(Protocol):
    """The two-client-method seam the collector needs from SerpAPI.

    Beginner note:
        Typing the dependency as a protocol (structural typing) lets tests
        inject a small fake with the same method shapes instead of subclassing
        the real network client. Production passes a ``SerpApiClient``, which
        satisfies this protocol automatically.
    """

    def ensure_ready(self) -> None:
        """Raise ``SerpApiSetupError`` when search is not configured.

        Beginner note:
            The collector checks readiness before issuing any query so
            no-key operation is one explicit, graceful batch outcome.
        """
        ...

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Return a bounded list of normalized organic search results.

        The concrete client owns response-size and field-length containment;
        this protocol records that expectation for fakes and alternate clients.
        """
        ...


@dataclass(frozen=True)
class IpoEnrichmentOutcome:
    """Summarize what one enrichment run observed, skipped, or failed.

    Beginner note:
        Optional search failure must not abort official-document screening.
        This typed receipt lets orchestration distinguish no-key operation,
        partial provider failure, quarantined evidence, and successful
        advisory observations without inspecting logs.
    """

    issue_id: int
    signals: tuple[IpoEnrichmentSignalRecord, ...]
    skipped_no_key: bool = False
    error_type: str | None = None
    batch_usability: IpoEnrichmentBatchUsability = (
        IpoEnrichmentBatchUsability.USABLE
    )
    human_review_required: bool = False
    # Set when the provider reported the plan is spent. Unlike an ordinary
    # failure this is not per-issue: nothing else in the run can succeed
    # either, so orchestration stops rather than issuing hundreds of calls
    # that are all going to be refused.
    quota_exhausted: bool = False


def _semantic_item_hash(entry: dict[str, Any]) -> str:
    """Hash one secret-safe normalized item for stable observation identity."""
    canonical = json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _red_flag_observations(text: str) -> list[dict[str, str]]:
    """Classify keyword mentions with nearby negation and preserved context.

    Beginner note:
        A bare keyword match loses meaning: “no litigation” and “litigation
        pending” point in opposite directions. Each observation therefore
        retains a bounded context and an explicit affirmative/negated status;
        it remains advisory until stronger evidence corroborates it.
    """
    normalized = normalize_external_text(text)
    folded = normalized.casefold()
    observations: list[dict[str, str]] = []
    for keyword in RED_FLAG_KEYWORDS:
        for match in re.finditer(re.escape(keyword), folded):
            start = max(0, match.start() - 40)
            end = min(len(normalized), match.end() + 40)
            context = normalized[start:end]
            before = folded[start : match.start()]
            after = folded[match.end() : end]
            negated = bool(_NEGATION_PATTERN.search(before)) or bool(
                _NEGATION_PATTERN.search(after)
            )
            observations.append(
                {
                    "keyword": keyword,
                    "status": "negated" if negated else "affirmative",
                    "context": context,
                    "reason": (
                        "nearby_negation"
                        if negated
                        else "affirmative_keyword_context"
                    ),
                }
            )
    return observations


def _normalize_entries(
    results: list[SearchResult],
) -> tuple[tuple[dict[str, Any], ...], IpoEnrichmentBatchUsability]:
    """Convert raw results into storable entries, quarantining hostile text.

    Beginner note:
        ``contains_injection`` scans the whole entry (title, snippet, and their
        concatenation) for model-directed instructions. On a hit the entry's
        text is replaced with the shared blocked-evidence marker before it can
        reach the database; only a payload-free warning is logged, so the
        hostile text never appears anywhere durable.
    """
    entries: list[dict[str, Any]] = []
    blocked_items = 0
    clean_items = 0
    for result in results:
        title = _normalize_enrichment_text(result.title)
        snippet = _normalize_enrichment_text(result.snippet)
        entry: dict[str, Any] = {
            "title": title,
            "link": result.link,
            "source": result.source,
            "snippet": snippet,
            "date": result.date,
            "authority": IpoEvidenceAuthority.ADVISORY.value,
            "corroborated": False,
        }
        if not title and not snippet:
            blocked_items += 1
            blocked = {
                "title": BLOCKED_EVIDENCE_TEXT,
                "link": "",
                "source": "",
                "snippet": BLOCKED_EVIDENCE_TEXT,
                "date": "",
                "matched_keywords": [],
                "red_flag_observations": [],
                "authority": IpoEvidenceAuthority.ADVISORY.value,
                "corroborated": False,
                "quarantine_status": "malformed",
                "quarantine_reason": "empty_result_text",
            }
            blocked["semantic_hash"] = _semantic_item_hash(blocked)
            entries.append(blocked)
            continue
        if contains_injection(entry):
            blocked_items += 1
            logger.warning(
                "Prompt-injection heuristics blocked one enrichment result; "
                "the snippet was withheld from storage."
            )
            blocked = {
                "title": BLOCKED_EVIDENCE_TEXT,
                "link": "",
                "source": "",
                "snippet": BLOCKED_EVIDENCE_TEXT,
                "date": "",
                "matched_keywords": [],
                "red_flag_observations": [],
                "authority": IpoEvidenceAuthority.ADVISORY.value,
                "corroborated": False,
                "quarantine_status": "quarantined",
                "quarantine_reason": "prompt_injection",
            }
            blocked["semantic_hash"] = _semantic_item_hash(blocked)
            entries.append(blocked)
            continue
        clean_items += 1
        combined = normalize_external_text(f"{title} {snippet}")
        observations = _red_flag_observations(combined)
        entry["matched_keywords"] = [
            observation["keyword"]
            for observation in observations
            if observation["status"] == "affirmative"
        ]
        entry["red_flag_observations"] = observations
        entry["quarantine_status"] = "clean"
        entry["quarantine_reason"] = ""
        entry["semantic_hash"] = _semantic_item_hash(entry)
        entries.append(entry)
    if blocked_items and not clean_items:
        usability = IpoEnrichmentBatchUsability.NOT_EVALUABLE
    elif blocked_items:
        usability = IpoEnrichmentBatchUsability.PARTIAL
    else:
        usability = IpoEnrichmentBatchUsability.USABLE
    # Usability describes every provider item inspected, including repeated
    # blocked items. The payload itself is a semantic set: duplicates must not
    # gain extra votes or create a new durable identity through result order.
    unique_by_hash = {str(entry["semantic_hash"]): entry for entry in entries}
    canonical_entries = tuple(
        unique_by_hash[item_hash] for item_hash in sorted(unique_by_hash)
    )
    return canonical_entries, usability


def _match_distance(left: re.Match[str], right: re.Match[str]) -> int:
    """Return the number of characters separating two regex matches."""
    if left.end() < right.start():
        return right.start() - left.end()
    if right.end() < left.start():
        return left.start() - right.end()
    return 0


def _near_gmp_value(text: str, pattern: re.Pattern[str]) -> Decimal | None:
    """Return the first value bound to a GMP term in the same short clause."""
    # Protect only the standalone currency abbreviation when it introduces a
    # numeric amount. Ordinary words ending in "rs." remain sentence endings.
    clause_text = _RUPEE_ABBREVIATION_PATTERN.sub(r"\1 ", text)
    for clause in _CLAUSE_SPLIT_PATTERN.split(clause_text):
        terms = list(_GMP_TERM_PATTERN.finditer(clause))
        for match in pattern.finditer(clause):
            if any(_match_distance(match, term) <= 40 for term in terms):
                return Decimal(match.group(1))
    return None


def _qib_bound_multiple(text: str) -> Decimal | None:
    """Return the subscription multiple that demonstrably belongs to QIB.

    Beginner note:
        A subscription-status headline prints every category at once -- "Retail
        25.6 times, QIB 1.2 times, NII 40 times". Merely sitting *near* the QIB
        anchor therefore proves nothing, because the retail figure sits near it
        too; the first attempt at this rule read that headline as a 25.6x
        institutional book when the real one was 1.2x, which inverts the signal
        in the dangerous direction.

        So two things are required. The clause is split on the punctuation
        those headlines actually use (commas and pipes, not just sentence
        ends), and within a clause the QIB anchor must be *strictly closer* to
        the number than any competing category word. Anything ambiguous returns
        ``None`` -- no reading at all is safer than the wrong category.
    """
    clause_text = _RUPEE_ABBREVIATION_PATTERN.sub(r"\1 ", text)
    for clause in _CATEGORY_SPLIT_PATTERN.split(clause_text):
        qib_terms = list(_QIB_TERM_PATTERN.finditer(clause))
        if not qib_terms:
            continue
        competitors = list(_COMPETING_CATEGORY_PATTERN.finditer(clause))
        for match in _SUBSCRIPTION_MULTIPLE_PATTERN.finditer(clause):
            nearest_qib = min(_match_distance(match, term) for term in qib_terms)
            if nearest_qib > 40:
                continue
            if competitors and min(
                _match_distance(match, other) for other in competitors
            ) <= nearest_qib:
                # Another category owns this number, or the clause cannot say
                # which one does. Either way it is not QIB evidence.
                continue
            return Decimal(match.group(1))
    return None


def _median_reading(readings: list[Decimal]) -> Decimal:
    """Return the two-place median of one batch of provider readings.

    Beginner note:
        The median is what stops a single outlier headline from setting an
        observation on its own. Both parsers share this one implementation so
        their rounding can never drift apart -- they previously carried
        byte-identical copies, which is exactly how such a drift starts.
    """
    readings.sort()
    middle = len(readings) // 2
    median = (
        readings[middle]
        if len(readings) % 2 == 1
        else (readings[middle - 1] + readings[middle]) / Decimal(2)
    )
    return median.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _parse_subscription_demand(
    entries: tuple[dict[str, Any], ...],
) -> Decimal | None:
    """Extract one conservative QIB subscription multiple, else ``None``.

    Beginner note:
        This follows :func:`_parse_gmp`'s shape -- title and snippet stay
        separate provider claims, and the median across entries prevents one
        outlier headline from setting the observation -- but the binding rule
        is stricter. GMP prose names one quantity; a subscription headline
        names every category at once, so :func:`_qib_bound_multiple` requires
        the number to be attributable to QIB rather than merely adjacent to it.
        Anything ambiguous returns ``None`` rather than guessing, because an
        invented demand multiple would feed a scored factor.
    """
    readings: list[Decimal] = []
    for entry in entries:
        source_fields = tuple(
            normalize_external_text(str(entry[field]))
            for field in ("title", "snippet")
        )
        multiple = next(
            (
                value
                for text in source_fields
                if (value := _qib_bound_multiple(text)) is not None
            ),
            None,
        )
        if multiple is not None:
            readings.append(multiple)
    if not readings:
        return None
    return _median_reading(readings)


def _parse_gmp(
    entries: tuple[dict[str, Any], ...], price_band_high: Decimal | None
) -> Decimal | None:
    """Extract one conservative GMP percent from clean entries, else ``None``.

    Beginner note:
        Each entry contributes at most one reading: its first percent match,
        or — only when the issue price is known — its first rupee match
        converted to a percent of that price. The median across entries keeps
        one outlier headline from setting the whole observation.
    """
    readings: list[Decimal] = []
    for entry in entries:
        # Title and snippet are separate provider claims. Keeping that boundary
        # prevents a number in one field from binding to "GMP" in the other.
        source_fields = tuple(
            normalize_external_text(str(entry[field]))
            for field in ("title", "snippet")
        )
        percent = next(
            (
                value
                for text in source_fields
                if (value := _near_gmp_value(text, _PERCENT_PATTERN)) is not None
            ),
            None,
        )
        if percent is not None:
            readings.append(percent)
            continue
        if price_band_high is None or price_band_high <= 0:
            continue
        rupees = next(
            (
                value
                for text in source_fields
                if (value := _near_gmp_value(text, _RUPEE_PATTERN)) is not None
            ),
            None,
        )
        if rupees is not None:
            readings.append(rupees / price_band_high * Decimal(100))
    if not readings:
        return None
    return _median_reading(readings)


def _record_web_subscription_snapshot(
    issue_id: int,
    signals: list[IpoEnrichmentSignalData],
    captured_at: dt.datetime,
    *,
    session_factory: SessionFactory,
) -> None:
    """Persist a parsed QIB multiple as a low-confidence demand snapshot.

    Beginner note:
        Two guards keep this web-sourced number from doing damage it was never
        trusted to do.

        First, it never shadows better evidence. ``get_latest_subscription``
        prefers an official snapshot over any web-sourced one regardless of
        capture time, so when official evidence exists this sees it and writes
        nothing at all -- and, just as importantly, a web row already on file
        cannot outrank an official snapshot recorded later.

        Second, it never breaks idempotency: an unchanged reading is skipped
        instead of appending a new row every run, because the scoring
        fingerprint includes the snapshot identity and a fresh row each run
        would re-score the issue forever.
    """
    parsed = next(
        (
            signal.parsed_value
            for signal in signals
            if signal.signal_type is IpoEnrichmentSignalType.SUBSCRIPTION_DEMAND
            and signal.parsed_value is not None
            and not signal.quarantined
        ),
        None,
    )
    if parsed is None:
        return

    latest = get_latest_subscription(issue_id, session_factory=session_factory)
    if latest is not None and latest.source_confidence is not Confidence.LOW:
        # Official demand evidence wins; advisory data must not overwrite it.
        return
    if latest is not None and latest.qib_multiple == parsed:
        # Same reading as last time: appending would only churn the fingerprint.
        return

    create_subscription(
        issue_id,
        IpoSubscriptionData(
            captured_at=captured_at,
            qib_multiple=parsed,
            source_confidence=Confidence.LOW,
        ),
        session_factory=session_factory,
    )


def collect_enrichment_signals(
    issue_id: int,
    *,
    company_name: str | None = None,
    price_band_high: Decimal | None = None,
    client: SupportsIpoSearch | None = None,
    captured_at: dt.datetime | None = None,
    max_results: int = 5,
    session_factory: SessionFactory = session_scope,
) -> IpoEnrichmentOutcome:
    """Run every discovery query for one issue and persist the observations.

    Args:
        issue_id: The issue the signals belong to; must already exist.
        company_name: Display name interpolated into the fixed query templates.
        price_band_high: Upper issue price, used only to convert rupee GMP
            quotes into a percent; ``None`` simply leaves those unparsed.
        client: Injectable SerpAPI client; production uses the shared default.
        captured_at: Injectable capture instant for reproducible tests.
        max_results: Per-query organic-result cap passed to the client.
        session_factory: Injectable transaction scope, tests pass fakes.

    Returns:
        An outcome carrying the persisted detached records, a graceful
        ``skipped_no_key`` marker when SerpAPI is unconfigured, and the
        exception type name when one or more queries failed.

    Beginner note:
        Failure isolation is per signal type: one failing query records its
        exception type and moves on, so a transient SerpAPI hiccup cannot wipe
        out the whole observation batch. A type whose query returned nothing is
        still persisted with an empty payload — "we looked and found nothing"
        is itself evidence worth keeping.
    """
    issue = get_issue(issue_id, session_factory=session_factory)
    if issue is None:
        raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
    if company_name is not None and str(company_name).strip() != issue.company_name:
        raise IpoValidationError(
            "company_name does not match the persisted IPO issue."
        )
    if (
        price_band_high is not None
        and price_band_high != issue.price_band_high
    ):
        raise IpoValidationError(
            "price_band_high does not match the persisted IPO issue."
        )
    persisted_company_name = issue.company_name
    persisted_price_band_high = issue.price_band_high

    active_client = client if client is not None else SerpApiClient()
    try:
        active_client.ensure_ready()
    except SerpApiSetupError:
        log_event(logger, EVENT_IPO_ENRICHMENT_SKIPPED, issue_id=issue_id)
        return IpoEnrichmentOutcome(issue_id=issue_id, signals=(), skipped_no_key=True)

    when = captured_at if captured_at is not None else dt.datetime.now(dt.UTC)
    signals: list[IpoEnrichmentSignalData] = []
    error_types: list[str] = []
    quota_exhausted = False
    for signal_type in IpoEnrichmentSignalType:
        query = _QUERY_TEMPLATES[signal_type].format(
            company=persisted_company_name
        )
        try:
            results = active_client.search(query, max_results=max_results)
        except SerpApiSearchError as exc:
            error_types.append(type(exc).__name__)
            # The class name and the status are the whole diagnosis. The
            # exception *message* is deliberately not logged: it can echo
            # provider text, which is untrusted upstream input.
            log_event(
                logger,
                EVENT_IPO_ENRICHMENT_FAILED,
                level=logging.WARNING,
                issue_id=issue_id,
                signal_type=signal_type.value,
                error_type=type(exc).__name__,
                status_code=getattr(exc, "status_code", None),
            )
            if isinstance(exc, SerpApiQuotaError):
                # Every remaining query would be refused the same way, so stop
                # here and let the caller stop too.
                quota_exhausted = True
                break
            continue
        entries, usability = _normalize_entries(results)
        clean_entries = tuple(
            entry
            for entry in entries
            if entry.get("quarantine_status") == "clean"
        )
        if signal_type is IpoEnrichmentSignalType.GMP:
            parsed_value = _parse_gmp(clean_entries, persisted_price_band_high)
        elif signal_type is IpoEnrichmentSignalType.SUBSCRIPTION_DEMAND:
            parsed_value = _parse_subscription_demand(clean_entries)
        else:
            parsed_value = None
        signals.append(
            IpoEnrichmentSignalData(
                signal_type=signal_type,
                captured_at=when,
                query_text=query,
                payload=entries,
                parsed_value=parsed_value,
                quarantined=usability
                is not IpoEnrichmentBatchUsability.USABLE,
                confidence=Confidence.LOW,
                source_policy=ENRICHMENT_SOURCE_POLICY,
                authority=IpoEvidenceAuthority.ADVISORY,
                corroborated=False,
                authority_policy_version=ENRICHMENT_AUTHORITY_POLICY_VERSION,
                batch_usability=usability,
            )
        )

    records = record_enrichment_signals(
        issue_id, signals, session_factory=session_factory
    )
    _record_web_subscription_snapshot(
        issue_id, signals, when, session_factory=session_factory
    )
    log_event(
        logger,
        EVENT_IPO_ENRICHMENT_COMPLETED,
        issue_id=issue_id,
        signals=len(records),
        quarantined=sum(1 for record in records if record.quarantined),
        failed_queries=len(error_types),
    )
    usability_values = {record.batch_usability for record in records}
    if usability_values == {IpoEnrichmentBatchUsability.NOT_EVALUABLE}:
        overall_usability = IpoEnrichmentBatchUsability.NOT_EVALUABLE
    elif usability_values - {IpoEnrichmentBatchUsability.USABLE}:
        overall_usability = IpoEnrichmentBatchUsability.PARTIAL
    else:
        overall_usability = IpoEnrichmentBatchUsability.USABLE
    return IpoEnrichmentOutcome(
        issue_id=issue_id,
        signals=tuple(records),
        error_type=", ".join(sorted(set(error_types))) or None,
        batch_usability=overall_usability,
        human_review_required=(
            overall_usability is not IpoEnrichmentBatchUsability.USABLE
            or bool(error_types)
        ),
        quota_exhausted=quota_exhausted,
    )
