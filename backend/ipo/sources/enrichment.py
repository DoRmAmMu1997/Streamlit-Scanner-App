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
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    SessionFactory,
    get_issue,
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
_PERCENT_PATTERN: Final = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*%")
_RUPEE_PATTERN: Final = re.compile(
    r"(?:₹|rs\.?|inr)\s*(-?\d{1,4}(?:\.\d+)?)", re.IGNORECASE
)
_NEGATION_PATTERN: Final = re.compile(
    r"\b(?:no|not|never|without|den(?:y|ies|ied)|dismiss(?:ed|al)?|"
    r"clear(?:ed)?|exonerat(?:ed|ion)|withdrawn)\b",
    re.IGNORECASE,
)

_TWO_PLACES = Decimal("0.01")


class SupportsIpoSearch(Protocol):
    """The two-client-method seam the collector needs from SerpAPI.

    Beginner note:
        Typing the dependency as a protocol (structural typing) lets tests
        inject a small fake with the same method shapes instead of subclassing
        the real network client. Production passes a ``SerpApiClient``, which
        satisfies this protocol automatically.
    """

    def ensure_ready(self) -> None:
        """Raise ``SerpApiSetupError`` when the API key is not configured."""
        ...

    def search(self, query: str, *, max_results: int = 5) -> list[SearchResult]:
        """Return normalized organic results for one query."""
        ...


@dataclass(frozen=True)
class IpoEnrichmentOutcome:
    """What one collection run observed, skipped, or failed to fetch."""

    issue_id: int
    signals: tuple[IpoEnrichmentSignalRecord, ...]
    skipped_no_key: bool = False
    error_type: str | None = None
    batch_usability: IpoEnrichmentBatchUsability = (
        IpoEnrichmentBatchUsability.USABLE
    )
    human_review_required: bool = False


def _semantic_item_hash(entry: dict[str, Any]) -> str:
    """Hash one secret-safe normalized item for stable observation identity."""
    canonical = json.dumps(
        entry, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _red_flag_observations(text: str) -> list[dict[str, str]]:
    """Classify keyword mentions with nearby negation and preserved context."""
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
        title = normalize_external_text(result.title)
        snippet = normalize_external_text(result.snippet)
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
    return tuple(entries), usability


def _match_distance(left: re.Match[str], right: re.Match[str]) -> int:
    """Return the number of characters separating two regex matches."""
    if left.end() < right.start():
        return right.start() - left.end()
    if right.end() < left.start():
        return left.start() - right.end()
    return 0


def _near_gmp_value(
    text: str,
    pattern: re.Pattern[str],
) -> Decimal | None:
    """Return the first numeric match within 40 characters of a GMP term."""
    terms = list(_GMP_TERM_PATTERN.finditer(text))
    for match in pattern.finditer(text):
        if any(_match_distance(match, term) <= 40 for term in terms):
            return Decimal(match.group(1))
    return None


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
        text = normalize_external_text(f"{entry['title']} {entry['snippet']}")
        percent = _near_gmp_value(text, _PERCENT_PATTERN)
        if percent is not None:
            readings.append(percent)
            continue
        if price_band_high is None or price_band_high <= 0:
            continue
        rupees = _near_gmp_value(text, _RUPEE_PATTERN)
        if rupees is not None:
            readings.append(rupees / price_band_high * Decimal(100))
    if not readings:
        return None
    readings.sort()
    middle = len(readings) // 2
    median = (
        readings[middle]
        if len(readings) % 2 == 1
        else (readings[middle - 1] + readings[middle]) / Decimal(2)
    )
    return median.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


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
    for signal_type in IpoEnrichmentSignalType:
        query = _QUERY_TEMPLATES[signal_type].format(
            company=persisted_company_name
        )
        try:
            results = active_client.search(query, max_results=max_results)
        except SerpApiSearchError as exc:
            error_types.append(type(exc).__name__)
            log_event(
                logger,
                EVENT_IPO_ENRICHMENT_FAILED,
                level=logging.WARNING,
                issue_id=issue_id,
                signal_type=signal_type.value,
                error_type=type(exc).__name__,
            )
            continue
        entries, usability = _normalize_entries(results)
        clean_entries = tuple(
            entry
            for entry in entries
            if entry.get("quarantine_status") == "clean"
        )
        parsed_value = (
            _parse_gmp(clean_entries, persisted_price_band_high)
            if signal_type is IpoEnrichmentSignalType.GMP
            else None
        )
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
    )
