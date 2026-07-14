"""IPO-010 deterministic section classifier tests.

Beginner note:
The classifier decides which prospectus pages the extraction agent may read
for each topic. It is pure keyword matching — no AI — so these tests can pin
exact assignments: which anchors map to which section, how ties break, and
that unrecognized pages land in the explicit OTHER bucket instead of being
guessed into a financial section.
"""

from __future__ import annotations

from backend.ipo.documents.section_classifier import (
    IpoSectionType,
    classify_pages,
)
from backend.ipo.documents.table_extractor import ExtractedPage


def _page(page_number: int, text: str) -> ExtractedPage:
    """Build one extracted page fixture with no tables."""
    return ExtractedPage(page_number=page_number, text=text, tables=())


def _section(sections, section_type):
    """Return one classified section by type, or ``None`` when absent."""
    return next(
        (section for section in sections if section.section is section_type), None
    )


def test_anchor_keywords_assign_pages_to_their_sections() -> None:
    """Each anchor family lands its page in the right section with receipts."""
    sections = classify_pages(
        [
            _page(1, "RISK FACTORS\nAn investment in equity shares involves risk."),
            _page(2, "OBJECTS OF THE OFFER\nRepayment of borrowings."),
            _page(
                3,
                "RESTATED CONSOLIDATED FINANCIAL INFORMATION\n"
                "Statement of profit and loss for the year.",
            ),
            _page(4, "OUR PROMOTERS\nProfiles of the promoter group."),
            _page(5, "OUTSTANDING LITIGATION AND MATERIAL DEVELOPMENTS"),
            _page(6, "CAPITAL STRUCTURE\nShare capital history of our company."),
            _page(7, "BASIS FOR OFFER PRICE\nComparison with listed industry peers."),
        ]
    )

    assert _section(sections, IpoSectionType.RISK_FACTORS).page_numbers == (1,)
    assert _section(sections, IpoSectionType.OBJECTS_OF_ISSUE).page_numbers == (2,)
    financial = _section(sections, IpoSectionType.FINANCIAL_STATEMENTS)
    assert financial.page_numbers == (3,)
    assert "restated consolidated financial information" in financial.keyword_hits
    assert _section(sections, IpoSectionType.PROMOTER).page_numbers == (4,)
    assert _section(sections, IpoSectionType.LITIGATION).page_numbers == (5,)
    assert _section(sections, IpoSectionType.CAPITAL_STRUCTURE).page_numbers == (6,)
    assert _section(sections, IpoSectionType.PEER_COMPARISON).page_numbers == (7,)


def test_unmatched_pages_land_in_other_never_a_guessed_section() -> None:
    """Pages without any anchor stay honestly unclassified."""
    sections = classify_pages(
        [
            _page(1, "GENERAL INFORMATION\nRegistered office and board details."),
            _page(2, "RISK FACTORS"),
        ]
    )

    other = _section(sections, IpoSectionType.OTHER)
    assert other is not None
    assert other.page_numbers == (1,)
    assert other.keyword_hits == ()


def test_page_with_multiple_sections_goes_to_the_highest_hit_count() -> None:
    """The most-anchored section wins one contested page deterministically."""
    text = (
        "Summary of RESTATED FINANCIAL STATEMENTS\n"
        "Statement of profit and loss\nBalance sheet\n"
        "with a passing mention of risk factors"
    )
    sections = classify_pages([_page(1, text)])

    financial = _section(sections, IpoSectionType.FINANCIAL_STATEMENTS)
    assert financial is not None and financial.page_numbers == (1,)
    assert _section(sections, IpoSectionType.RISK_FACTORS) is None


def test_tie_breaks_follow_the_fixed_section_order() -> None:
    """Equal hit counts resolve by catalog order, never dict ordering."""
    text = "risk factors\nour promoters"
    sections = classify_pages([_page(1, text)])

    # RISK_FACTORS precedes PROMOTER in the fixed catalog order.
    assert _section(sections, IpoSectionType.RISK_FACTORS).page_numbers == (1,)
    assert _section(sections, IpoSectionType.PROMOTER) is None


def test_sections_collect_all_their_pages_sorted() -> None:
    """Multi-page sections aggregate page numbers in ascending order."""
    sections = classify_pages(
        [
            _page(3, "risk factors continued"),
            _page(1, "RISK FACTORS"),
            _page(2, "internal risk factors"),
        ]
    )

    assert _section(sections, IpoSectionType.RISK_FACTORS).page_numbers == (1, 2, 3)


def test_classification_is_deterministic() -> None:
    """Two runs over the same pages produce identical receipts."""
    pages = [_page(1, "RISK FACTORS"), _page(2, "capital structure")]

    assert classify_pages(pages) == classify_pages(pages)
