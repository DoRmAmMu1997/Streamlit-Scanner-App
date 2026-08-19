"""IPO-011 cap-price (price band) extraction tests.

Beginner note:
The cap price is the one bound every valuation ratio consumes, and valuation
is a *critical* factor -- without it an issue can never reach a positive
verdict. It therefore travels the same route as every other number: the model
proposes it with a page citation, the host re-resolves that citation against
the real PDF bytes, and only an approval writes it to the issue.

Two properties get the most attention here. A price-band line names both a
floor and a cap, so the host additionally requires the claimed cap to be the
largest number on the span it cites -- claiming the floor would make the issue
look cheaper and inflate the valuation score. And the field stays optional,
because a DRHP is filed before pricing.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from backend.ipo.agents import financial_extractor
from backend.ipo.documents.table_extractor import ExtractedPage
from backend.ipo.models import Confidence


def _model(**overrides: object) -> dict[str, object]:
    """Build the minimum proposal payload the model validator accepts."""
    period = {
        "period_end": "2026-03-31",
        "revenue": "150",
        "revenue_page": 1,
        "ebitda": "30",
        "ebitda_page": 1,
        "pat": "15",
        "pat_page": 1,
        "profit_before_tax": "18",
        "profit_before_tax_page": 1,
        "finance_cost": "2",
        "finance_cost_page": 1,
    }
    payload: dict[str, object] = {
        "financial_amount_unit": "crore_inr",
        "financial_amount_unit_page": 1,
        "issue_amount_unit": "crore_inr",
        "issue_amount_unit_page": 3,
        "equity_share_unit": "lakh_shares",
        "equity_share_unit_page": 2,
        "periods": [
            {**period, "period_end": "2024-03-31"},
            {**period, "period_end": "2025-03-31"},
            period,
        ],
        "net_worth": "90",
        "net_worth_page": 2,
        "total_debt": "12",
        "total_debt_page": 2,
        "cash": "5",
        "cash_page": 2,
        "cash_flow_from_operations": "14",
        "cash_flow_from_operations_page": 2,
        "equity_shares": "50",
        "equity_shares_page": 2,
        "eps": "3.00",
        "eps_page": 2,
        "nav_book_value": "18.75",
        "nav_book_value_page": 2,
        "objects_of_issue": "Fresh issue and offer for sale as described.",
        "objects_of_issue_page": 3,
        "fresh_issue_amount": "300",
        "fresh_issue_amount_page": 3,
        "ofs_amount": "0",
        "ofs_amount_page": 3,
        "promoter_holding_pre_issue": "75.25",
        "promoter_holding_pre_issue_page": 3,
        "promoter_holding_post_issue": "56.44",
        "promoter_holding_post_issue_page": 3,
        "total_assets": "150",
        "total_assets_page": 2,
        "current_liabilities": "45",
        "current_liabilities_page": 2,
        "post_issue_equity_shares": "60",
        "post_issue_equity_shares_page": 2,
        "peers": [
            {
                "company_name": "Peer One Ltd",
                "source_page": 3,
                "metrics": {"pe": "21.40", "eps": "8.25"},
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_cap_price_is_optional_so_drhp_proposals_stay_valid() -> None:
    """A DRHP is filed before pricing; omitting the band must be fine."""
    proposal = financial_extractor._ProposalModel.model_validate(_model())

    assert proposal.price_band_high is None
    assert proposal.price_band_high_page is None
    # The optional field must not appear as a citation when it is absent.
    labels = [label for label, _value, _page in financial_extractor._citations(proposal)]
    assert "price_band_high" not in labels


def test_cap_price_requires_its_own_page_citation() -> None:
    """A value without a page (or a page without a value) is rejected."""
    with pytest.raises(ValidationError):
        financial_extractor._ProposalModel.model_validate(
            _model(price_band_high="100")
        )
    with pytest.raises(ValidationError):
        financial_extractor._ProposalModel.model_validate(
            _model(price_band_high_page=3)
        )


def test_cap_price_becomes_a_citation_when_supplied() -> None:
    """A priced RHP contributes the cap exactly like any other numeric fact."""
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="100", price_band_high_page=3)
    )

    citations = {
        label: (value, page)
        for label, value, page in financial_extractor._citations(proposal)
    }
    assert citations["price_band_high"] == ("100", 3)


def _band_page(line: str) -> ExtractedPage:
    """Build a one-line page carrying a price-band statement."""
    return ExtractedPage(page_number=3, text=line, tables=())


def test_cap_price_binds_to_the_band_line_when_it_is_the_upper_bound() -> None:
    """The host resolves the cap against the real printed span."""
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="100", price_band_high_page=3)
    )
    page = _band_page("Price Band: Rs 95 to Rs 100 per equity share")

    source = financial_extractor._matching_numeric_source_for_fact(
        "price_band_high", "100", page, proposal
    )

    assert source is not None
    # The receipt records the exact printed token, currency prefix included.
    assert source.source_token == "Rs 100"
    assert financial_extractor._parse_printed_number(source.source_token) == Decimal("100")
    assert source.location.startswith("text-line:")


def test_claiming_the_floor_price_as_the_cap_is_refused() -> None:
    """The unsafe direction is blocked: a floor must not pass as the cap.

    Beginner note:
        Both bounds sit on the same line, so plain token matching would accept
        95 just as readily as 100. A lower price makes the issue look cheaper
        and inflates the valuation factor, so the host requires the claimed cap
        to be the largest number on the span it cites.
    """
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="95", price_band_high_page=3)
    )
    page = _band_page("Price Band: Rs 95 to Rs 100 per equity share")

    assert (
        financial_extractor._matching_numeric_source_for_fact(
            "price_band_high", "95", page, proposal
        )
        is None
    )


def test_cap_price_is_not_bound_from_an_unrelated_line() -> None:
    """A number without the price-band label proves nothing."""
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="100", price_band_high_page=3)
    )
    page = _band_page("Total assets 100 (in crore INR)")

    assert (
        financial_extractor._matching_numeric_source_for_fact(
            "price_band_high", "100", page, proposal
        )
        is None
    )


def test_cap_price_fact_carries_no_unit_scaling() -> None:
    """The band is printed in plain rupees per share, so no multiplier applies."""
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="100", price_band_high_page=3)
    )
    page = _band_page("Price Band: Rs 95 to Rs 100 per equity share")

    # Every cited page must exist; the other two carry no matching spans, so
    # only the cap price binds -- which is exactly what this test inspects.
    facts = financial_extractor._cited_financial_facts(
        proposal,
        (
            ExtractedPage(page_number=1, text="", tables=()),
            ExtractedPage(page_number=2, text="", tables=()),
            page,
        ),
        source_content_sha256="a" * 64,
        confidence=Confidence.HIGH,
    )

    band = next(fact for fact in facts if fact.field_name == "price_band_high")
    assert band.value == Decimal("100")
    assert band.unit is None
    assert band.unit_multiplier == Decimal("1")
    assert band.period_end is None
    assert band.page_number == 3


def test_a_cover_page_row_with_a_bid_lot_still_binds_the_cap() -> None:
    """A larger unrelated number on the row must not void the citation.

    Beginner note:
        The first version of this guard demanded the cap be the largest number
        anywhere on the span. Real cover-page rows print a bid lot ("150 Equity
        Shares") and a fiscal year beside a two-digit share price, so that rule
        rejected perfectly good evidence -- and a rejected cap price means no
        valuation, which means no verdict at all. Binding to the stated price
        construct is both stricter and free of that false rejection.
    """
    proposal = financial_extractor._ProposalModel.model_validate(
        _model(price_band_high="100", price_band_high_page=3)
    )
    page = _band_page(
        "Price Band: Rs 95 to Rs 100 per Equity Share | Bid Lot: 150 Equity Shares"
    )

    source = financial_extractor._matching_numeric_source_for_fact(
        "price_band_high", "100", page, proposal
    )

    assert source is not None
    # The floor is still refused on the very same span.
    assert not financial_extractor._cap_price_binds_to_a_stated_band(
        "price_band_high", "95", page.text
    )
    # ...and so is the bid lot, which is not a price at all.
    assert not financial_extractor._cap_price_binds_to_a_stated_band(
        "price_band_high", "150", page.text
    )


def test_naming_a_price_band_does_not_make_a_sibling_fact_unverifiable() -> None:
    """A "Basis for the Offer Price" row must still verify its own number.

    Beginner note:
        The RHP states EPS and NAV "in relation to the Price Band". Registering
        the cap price in the shared field-label table made those rows carry two
        semantic fields, and the "exactly one field per span" rule then rejected
        them -- failing proposals that verified cleanly before the cap price
        existed. EPS is a core label, so that took the whole proposal down with
        it.
    """
    span = (
        "Earnings Per Share (EPS) 3.00 in relation to the Price Band of Rs 95 to Rs 100"
    )

    assert financial_extractor._semantic_field_labels(span) == {"eps"}
    assert financial_extractor._span_matches_field_label("eps", span)
    # The cap price is still recognised on that same span, by construct.
    assert financial_extractor._span_matches_field_label("price_band_high", span)
