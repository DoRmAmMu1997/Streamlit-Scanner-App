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
import logging
import re
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Final, Protocol

from backend.ipo.models import (
    Confidence,
    IpoEnrichmentSignalData,
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
)
from backend.ipo.repository import SessionFactory, record_enrichment_signals
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

ENRICHMENT_SOURCE_POLICY: Final = "serpapi-low-confidence-v1"

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
_PERCENT_PATTERN: Final = re.compile(r"(-?\d{1,3}(?:\.\d+)?)\s*%")
_RUPEE_PATTERN: Final = re.compile(r"(?:₹|rs\.?|inr)\s*(-?\d{1,4}(?:\.\d+)?)", re.IGNORECASE)

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


def _normalize_entries(
    results: list[SearchResult],
) -> tuple[tuple[dict[str, Any], ...], bool]:
    """Convert raw results into storable entries, quarantining hostile text.

    Beginner note:
        ``contains_injection`` scans the whole entry (title, snippet, and their
        concatenation) for model-directed instructions. On a hit the entry's
        text is replaced with the shared blocked-evidence marker before it can
        reach the database; only a payload-free warning is logged, so the
        hostile text never appears anywhere durable.
    """
    entries: list[dict[str, Any]] = []
    any_quarantined = False
    for result in results:
        entry: dict[str, Any] = {
            "title": result.title,
            "link": result.link,
            "source": result.source,
            "snippet": result.snippet,
            "date": result.date,
        }
        if contains_injection(entry):
            any_quarantined = True
            logger.warning(
                "Prompt-injection heuristics blocked one enrichment result; "
                "the snippet was withheld from storage."
            )
            entries.append(
                {
                    "title": BLOCKED_EVIDENCE_TEXT,
                    "link": "",
                    "source": "",
                    "snippet": BLOCKED_EVIDENCE_TEXT,
                    "date": "",
                    "matched_keywords": [],
                }
            )
            continue
        combined = normalize_external_text(f"{result.title} {result.snippet}").casefold()
        entry["matched_keywords"] = [
            keyword for keyword in RED_FLAG_KEYWORDS if keyword in combined
        ]
        entries.append(entry)
    return tuple(entries), any_quarantined


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
        if "gmp" not in text.casefold():
            continue
        percent_match = _PERCENT_PATTERN.search(text)
        if percent_match is not None:
            readings.append(Decimal(percent_match.group(1)))
            continue
        if price_band_high is None or price_band_high <= 0:
            continue
        rupee_match = _RUPEE_PATTERN.search(text)
        if rupee_match is not None:
            rupees = Decimal(rupee_match.group(1))
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
    company_name: str,
    price_band_high: Decimal | None,
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
        query = _QUERY_TEMPLATES[signal_type].format(company=company_name)
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
        entries, any_quarantined = _normalize_entries(results)
        clean_entries = tuple(
            entry for entry in entries if entry.get("title") != BLOCKED_EVIDENCE_TEXT
        )
        parsed_value = (
            _parse_gmp(clean_entries, price_band_high)
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
                quarantined=any_quarantined,
                confidence=Confidence.LOW,
                source_policy=ENRICHMENT_SOURCE_POLICY,
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
    return IpoEnrichmentOutcome(
        issue_id=issue_id,
        signals=tuple(records),
        error_type=", ".join(sorted(set(error_types))) or None,
    )
