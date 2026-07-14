"""IPO-010: deterministic keyword classification of extracted prospectus pages.

The classifier decides which pages the extraction agent may read for each
topic (financial statements, objects of issue, risk factors, and so on). It
is intentionally plain substring matching against a reviewed anchor catalog —
no AI, no scoring model — so its assignments are reproducible receipts that a
human can verify against the page text.

Beginner note:
The output is a set of ``ClassifiedSection`` receipts: each names its section,
the pages assigned to it, and exactly which anchor phrases matched. A page
that matches nothing lands in the explicit ``OTHER`` bucket rather than being
guessed into a section, because a wrong "financial statements" page would let
the extraction agent cite numbers from the wrong part of the document.
"""

from __future__ import annotations

import enum
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from backend.ipo.documents.table_extractor import ExtractedPage


class IpoSectionType(enum.StrEnum):
    """The prospectus section families the extraction agent understands."""

    FINANCIAL_STATEMENTS = "financial_statements"
    OBJECTS_OF_ISSUE = "objects_of_issue"
    RISK_FACTORS = "risk_factors"
    PROMOTER = "promoter"
    LITIGATION = "litigation"
    CAPITAL_STRUCTURE = "capital_structure"
    PEER_COMPARISON = "peer_comparison"
    OTHER = "other"


# Case-folded anchor phrases per section, drawn from standard DRHP/RHP
# chapter headings. A page is assigned to the section with the most anchor
# hits; ties break by this catalog's order, so classification never depends
# on dict iteration details.
_SECTION_ANCHORS: Final[tuple[tuple[IpoSectionType, tuple[str, ...]], ...]] = (
    (
        IpoSectionType.FINANCIAL_STATEMENTS,
        (
            "restated consolidated financial information",
            "restated financial statements",
            "restated financial information",
            "summary of financial information",
            "statement of profit and loss",
            "balance sheet",
            "cash flow statement",
        ),
    ),
    (
        IpoSectionType.OBJECTS_OF_ISSUE,
        (
            "objects of the offer",
            "objects of the issue",
            "use of proceeds",
            "utilisation of net proceeds",
        ),
    ),
    (
        IpoSectionType.RISK_FACTORS,
        ("risk factors", "internal risk factors", "external risk factors"),
    ),
    (
        IpoSectionType.PROMOTER,
        ("our promoters", "promoter group", "promoters and promoter group"),
    ),
    (
        IpoSectionType.LITIGATION,
        (
            "outstanding litigation",
            "material developments",
            "legal proceedings",
        ),
    ),
    (
        IpoSectionType.CAPITAL_STRUCTURE,
        ("capital structure", "share capital history"),
    ),
    (
        IpoSectionType.PEER_COMPARISON,
        (
            "basis for offer price",
            "basis for the offer price",
            "comparison with listed industry peers",
            "accounting ratios",
        ),
    ),
)


@dataclass(frozen=True)
class ClassifiedSection:
    """One section's assigned pages and the anchor phrases that earned them."""

    section: IpoSectionType
    page_numbers: tuple[int, ...]
    keyword_hits: tuple[str, ...]


def _classify_page(text: str) -> tuple[IpoSectionType, tuple[str, ...]]:
    """Assign one page to its best section and report the matched anchors."""
    folded = text.casefold()
    best_section = IpoSectionType.OTHER
    best_hits: tuple[str, ...] = ()
    for section, anchors in _SECTION_ANCHORS:
        hits = tuple(anchor for anchor in anchors if anchor in folded)
        # Strictly-greater keeps the first (catalog-order) section on ties.
        if len(hits) > len(best_hits):
            best_section = section
            best_hits = hits
    return best_section, best_hits


def classify_pages(pages: Sequence[ExtractedPage]) -> tuple[ClassifiedSection, ...]:
    """Classify extracted pages into section receipts in catalog order.

    Args:
        pages: Extracted pages in any order; assignments use each page's own
            recorded number, and section page lists come back sorted.

    Returns:
        One ``ClassifiedSection`` per section that received at least one page
        (including ``OTHER``), ordered by the fixed catalog order with
        ``OTHER`` last. Keyword hits are the sorted union of every matched
        anchor across the section's pages.
    """
    assigned: dict[IpoSectionType, list[int]] = {}
    hits_by_section: dict[IpoSectionType, set[str]] = {}
    for page in pages:
        section, hits = _classify_page(page.text)
        assigned.setdefault(section, []).append(page.page_number)
        hits_by_section.setdefault(section, set()).update(hits)

    ordered_sections = [section for section, _anchors in _SECTION_ANCHORS]
    ordered_sections.append(IpoSectionType.OTHER)
    return tuple(
        ClassifiedSection(
            section=section,
            page_numbers=tuple(sorted(assigned[section])),
            keyword_hits=tuple(sorted(hits_by_section[section])),
        )
        for section in ordered_sections
        if section in assigned
    )
