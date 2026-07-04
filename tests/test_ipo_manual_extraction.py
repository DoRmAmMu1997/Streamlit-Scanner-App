"""Contract tests for IPO-004/005 immutable manual-extraction input models.

Beginner note:
These tests describe the form payload before any database or Streamlit code is
involved. Keeping validation here makes the browser form only one caller of the
same rules that future jobs or extractors will also have to obey.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import IpoValidationError


def _period(year: int) -> IpoManualPeriodData:
    """Build one complete fiscal period with deliberately different pages.

    Beginner note:
        Distinct page numbers make accidental field-to-page swaps visible in test
        failures; using one page for every value would hide that class of bug.
    """
    return IpoManualPeriodData(
        period_end=dt.date(year, 3, 31),
        revenue=Decimal("100.25"),
        revenue_page=101,
        ebitda=Decimal("20.50"),
        ebitda_page=102,
        pat=Decimal("10.75"),
        pat_page=103,
        profit_before_tax=Decimal("14.25"),
        profit_before_tax_page=104,
        finance_cost=Decimal("1.25"),
        finance_cost_page=105,
    )


def _peer(name: str = "Example Peer Ltd") -> IpoPeerValuationData:
    """Build one peer row using two supported valuation metrics."""
    return IpoPeerValuationData(
        company_name=name,
        source_page=210,
        metrics={
            IpoPeerMetric.EPS: Decimal("8.25"),
            IpoPeerMetric.PE: Decimal("21.40"),
        },
    )


def _payload(**overrides: object) -> IpoManualExtractionData:
    """Return a complete valid payload, replacing only named test fields.

    Beginner note:
        Most validation tests need one intentionally bad field. Starting from a
        known-good payload keeps each failure focused on that single rule instead
        of repeating dozens of unrelated required values in every test.
    """
    values: dict[str, object] = {
        "source_document_id": 7,
        "financial_amount_unit": IpoAmountUnit.CRORE_INR,
        "issue_amount_unit": IpoAmountUnit.CRORE_INR,
        "equity_share_unit": IpoShareUnit.LAKH_SHARES,
        "periods": (_period(2023), _period(2024), _period(2025)),
        "net_worth": Decimal("80"),
        "net_worth_page": 110,
        "total_debt": Decimal("12"),
        "total_debt_page": 111,
        "cash": Decimal("5"),
        "cash_page": 112,
        "cash_flow_from_operations": Decimal("14"),
        "cash_flow_from_operations_page": 113,
        "equity_shares": Decimal("50"),
        "equity_shares_page": 114,
        "eps": Decimal("2.50"),
        "eps_page": 115,
        "nav_book_value": Decimal("18.75"),
        "nav_book_value_page": 116,
        "objects_of_issue": "Build a new plant and repay secured borrowings.",
        "objects_of_issue_page": 117,
        "fresh_issue_amount": Decimal("300"),
        "fresh_issue_amount_page": 118,
        "ofs_amount": Decimal("0"),
        "ofs_amount_page": 119,
        "promoter_holding_pre_issue": Decimal("75.25"),
        "promoter_holding_pre_issue_page": 120,
        "promoter_holding_post_issue": Decimal("56.44"),
        "promoter_holding_post_issue_page": 121,
        "total_assets": Decimal("150"),
        "total_assets_page": 122,
        "current_liabilities": Decimal("45"),
        "current_liabilities_page": 123,
        "post_issue_equity_shares": Decimal("60"),
        "post_issue_equity_shares_page": 124,
        "peers": (_peer(),),
    }
    values.update(overrides)
    return IpoManualExtractionData(**values)  # type: ignore[arg-type]


def test_amount_and_share_units_convert_to_canonical_values_exactly() -> None:
    """Unit conversion must use Decimal arithmetic without float rounding."""
    assert IpoAmountUnit.CRORE_INR.to_inr(Decimal("1.25")) == Decimal("12500000.00")
    assert IpoAmountUnit.LAKH_INR.to_inr(Decimal("2.5")) == Decimal("250000.0")
    assert IpoShareUnit.LAKH_SHARES.to_shares(Decimal("1.25")) == Decimal("125000.00")


def test_complete_payload_normalizes_order_and_peer_metric_keys() -> None:
    """A valid form submission becomes a frozen, oldest-to-newest contract."""
    payload = _payload(periods=(_period(2025), _period(2023), _period(2024)))

    assert [period.period_end.year for period in payload.periods] == [2023, 2024, 2025]
    assert payload.peers[0].company_key == "example peer"
    assert payload.peers[0].metrics == {
        IpoPeerMetric.EPS: Decimal("8.2500"),
        IpoPeerMetric.PE: Decimal("21.4000"),
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"periods": (_period(2023), _period(2024))}, "exactly three"),
        (
            {"periods": (_period(2023), _period(2023), _period(2025))},
            "distinct period_end",
        ),
        (
            {"periods": (_period(2022), _period(2024), _period(2025))},
            "consecutive annual years",
        ),
        ({"objects_of_issue": "   "}, "objects_of_issue is required"),
        ({"peers": ()}, "at least one peer"),
        ({"equity_shares": Decimal("0")}, "equity_shares must be positive"),
        (
            {"promoter_holding_post_issue": Decimal("100.01")},
            "promoter_holding_post_issue must be from 0 to 100",
        ),
        ({"cash": Decimal("NaN")}, "cash must be finite"),
        ({"total_debt": Decimal("-0.01")}, "total_debt must be non-negative"),
        ({"total_assets": Decimal("-0.01")}, "total_assets must be non-negative"),
        (
            {"current_liabilities": Decimal("-0.01")},
            "current_liabilities must be non-negative",
        ),
        (
            {"post_issue_equity_shares": Decimal("0")},
            "post_issue_equity_shares must be positive",
        ),
        ({"total_assets": None}, "total_assets is required"),
        ({"net_worth_page": 0}, "net_worth_page must be positive"),
        ({"total_assets_page": 0}, "total_assets_page must be positive"),
    ],
)
def test_complete_payload_rejects_missing_or_unsafe_values(
    overrides: dict[str, object], message: str
) -> None:
    """Every required value and its page must pass the domain's strict rules."""
    with pytest.raises(IpoValidationError, match=message):
        _payload(**overrides)


def test_peer_rows_reject_duplicate_companies_after_normalization() -> None:
    """Punctuation or suffix variations must not create duplicate peer rows."""
    with pytest.raises(IpoValidationError, match="peer companies must be unique"):
        _payload(peers=(_peer("Example Peer Ltd"), _peer("example-peer limited")))


def test_peer_requires_a_supported_metric_and_positive_page() -> None:
    """A peer name alone is not valuation evidence and cannot be submitted."""
    with pytest.raises(IpoValidationError, match="at least one supported metric"):
        IpoPeerValuationData(company_name="Peer Ltd", source_page=5, metrics={})
    with pytest.raises(IpoValidationError, match="source_page must be positive"):
        IpoPeerValuationData(
            company_name="Peer Ltd",
            source_page=0,
            metrics={IpoPeerMetric.PE: Decimal("10")},
        )


def test_period_rejects_partial_ipo005_value_and_page_groups() -> None:
    """PBT and finance cost must never be detached from their source pages.

    Beginner note:
        A value without its citation cannot be independently checked in the offer
        document. The all-or-none contract therefore rejects even a numerically
        valid PBT when the rest of the sourced IPO-005 group is absent.
    """
    with pytest.raises(IpoValidationError, match="require values and source pages together"):
        IpoManualPeriodData(
            period_end=dt.date(2025, 3, 31),
            revenue=Decimal("100"),
            revenue_page=1,
            ebitda=Decimal("20"),
            ebitda_page=2,
            pat=Decimal("10"),
            pat_page=3,
            profit_before_tax=Decimal("12"),
        )
