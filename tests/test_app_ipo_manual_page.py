"""Tests for the IPO-004 administrator manual-extraction page.

Beginner note:
Streamlit reruns a page after every interaction, so the renderer is kept thin
and delegates conversion to pure helpers. These tests can therefore prove the
security guard and exact form-to-domain conversion without launching a browser
or writing to the real application database.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from backend.auth.roles import Role
from backend.auth.session import AuthenticatedUser
from backend.ipo.manual_extraction import IpoAmountUnit, IpoPeerMetric, IpoShareUnit
from backend.ipo.models import IpoValidationError
from ui import ipo_manual_page

ADMIN = AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)


def test_widget_keys_are_scoped_to_the_selected_issue() -> None:
    """Switching IPOs must give Streamlit a fresh, issue-specific widget state.

    Beginner note:
    Streamlit remembers widget values by key. If two IPOs shared a key, values
    typed for the first company could appear in the second company's form and
    make accidental cross-company entry much easier.
    """
    assert ipo_manual_page._widget_key(7, "net_worth") == "ipo_7_net_worth"
    assert ipo_manual_page._widget_key(8, "net_worth") != ipo_manual_page._widget_key(
        7, "net_worth"
    )


class _FakeStreamlit:
    """Capture the small Streamlit surface used before the entry form renders."""

    def __init__(self) -> None:
        """Prepare message lists that assertions can inspect."""
        self.errors: list[str] = []
        self.infos: list[str] = []

    def subheader(self, *_args, **_kwargs) -> None:
        """Accept the page heading without rendering a real browser widget."""

    def caption(self, *_args, **_kwargs) -> None:
        """Accept explanatory copy without rendering a real browser widget."""

    def error(self, text, **_kwargs) -> None:
        """Record one user-facing error."""
        self.errors.append(str(text))

    def info(self, text, **_kwargs) -> None:
        """Record one user-facing informational message."""
        self.infos.append(str(text))


def test_manual_page_rejects_non_admin_before_reading_data(monkeypatch) -> None:
    """Defense in depth must stop analysts even if routing is bypassed."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(ipo_manual_page, "st", fake_st)
    monkeypatch.setattr(
        ipo_manual_page,
        "list_issues",
        lambda: (_ for _ in ()).throw(AssertionError("must not query")),
    )

    ipo_manual_page._render_ipo_manual_page(
        AuthenticatedUser("analyst@example.com", role=Role.ANALYST)
    )

    assert fake_st.errors == ["Admin access is required to enter IPO evidence."]


def test_manual_page_explains_when_no_ipo_issue_exists(monkeypatch) -> None:
    """An empty database should produce an actionable state instead of a crash."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(ipo_manual_page, "st", fake_st)
    monkeypatch.setattr(ipo_manual_page, "list_issues", list)

    ipo_manual_page._render_ipo_manual_page(ADMIN)

    assert fake_st.errors == []
    assert any("ingestion" in message.lower() for message in fake_st.infos)


def test_peer_rows_convert_only_complete_dynamic_editor_rows() -> None:
    """Blank editor rows are ignored while entered decimals retain precision."""
    peers = ipo_manual_page._peer_rows_to_domain(
        [
            {
                "company_name": "Peer One Ltd",
                "source_page": 210,
                "eps": "8.2500",
                "pe": "21.40",
                "nav_book_value": "",
                "ronw": None,
                "ev_ebitda": "",
                "price_sales": "",
            },
            {"company_name": "", "source_page": None},
        ]
    )

    assert len(peers) == 1
    assert peers[0].metrics == {
        IpoPeerMetric.EPS: Decimal("8.2500"),
        IpoPeerMetric.PE: Decimal("21.4000"),
    }


def test_peer_rows_reject_partial_rows_instead_of_silently_dropping_them() -> None:
    """A named peer without a page or metric must surface a validation error."""
    with pytest.raises(IpoValidationError):
        ipo_manual_page._peer_rows_to_domain(
            [{"company_name": "Peer One Ltd", "source_page": None, "pe": ""}]
        )


def test_period_builder_preserves_explicit_dates_values_and_pages() -> None:
    """The UI adapter should create the same exact three-period domain contract."""
    periods = ipo_manual_page._period_rows_to_domain(
        [
            {
                "period_end": dt.date(year, 3, 31),
                "revenue": str(year),
                "revenue_page": 10,
                "ebitda": "20.5",
                "ebitda_page": 11,
                "pat": "10.25",
                "pat_page": 12,
            }
            for year in (2023, 2024, 2025)
        ]
    )

    assert periods[0].period_end == dt.date(2023, 3, 31)
    assert periods[0].revenue == Decimal("2023.0000")


def test_form_mapping_builds_complete_domain_payload_without_actor_fields() -> None:
    """The UI adapter must not accept an entered-by identity from browser data."""
    values = {
        "net_worth": "80",
        "net_worth_page": 130,
        "total_debt": "12",
        "total_debt_page": 131,
        "cash": "5",
        "cash_page": 132,
        "cash_flow_from_operations": "14",
        "cash_flow_from_operations_page": 133,
        "equity_shares": "50",
        "equity_shares_page": 134,
        "eps": "2.5",
        "eps_page": 135,
        "nav_book_value": "18.75",
        "nav_book_value_page": 136,
        "objects_of_issue": "Build a plant and repay debt.",
        "objects_of_issue_page": 137,
        "fresh_issue_amount": "300",
        "fresh_issue_amount_page": 138,
        "ofs_amount": "0",
        "ofs_amount_page": 139,
        "promoter_holding_pre_issue": "75.25",
        "promoter_holding_pre_issue_page": 140,
        "promoter_holding_post_issue": "56.44",
        "promoter_holding_post_issue_page": 141,
    }
    period_rows = [
        {
            "period_end": dt.date(year, 3, 31),
            "revenue": "100",
            "revenue_page": 100,
            "ebitda": "20",
            "ebitda_page": 101,
            "pat": "10",
            "pat_page": 102,
        }
        for year in (2023, 2024, 2025)
    ]

    payload = ipo_manual_page._build_payload(
        source_document_id=7,
        financial_amount_unit=IpoAmountUnit.CRORE_INR,
        issue_amount_unit=IpoAmountUnit.CRORE_INR,
        equity_share_unit=IpoShareUnit.LAKH_SHARES,
        period_rows=period_rows,
        scalar_values=values,
        peer_rows=[{"company_name": "Peer Ltd", "source_page": 210, "pe": "20"}],
    )

    assert payload.source_document_id == 7
    assert payload.net_worth == Decimal("80.0000")
    assert payload.periods[2].period_end == dt.date(2025, 3, 31)
    assert not hasattr(payload, "entered_by_email")
