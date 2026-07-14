"""Tests for the IPO-004 administrator manual-extraction page.

Beginner note:
Streamlit reruns a page after every interaction, so the renderer is kept thin
and delegates conversion to pure helpers. These tests can therefore prove the
security guard and exact form-to-domain conversion without launching a browser
or writing to the real application database.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pandas as pd
import pytest

from backend.auth.roles import Role
from backend.auth.session import AuthenticatedUser
from backend.ipo.manual_extraction import IpoAmountUnit, IpoPeerMetric, IpoShareUnit
from backend.ipo.models import (
    Confidence,
    IpoExtractionProposalRecord,
    IpoExtractionProposalStatus,
    IpoValidationError,
)
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
        self.captions: list[str] = []

    def subheader(self, *_args, **_kwargs) -> None:
        """Accept the page heading without rendering a real browser widget."""

    def markdown(self, *_args, **_kwargs) -> None:
        """Accept section headings without rendering a real browser widget."""

    def caption(self, text, **_kwargs) -> None:
        """Record explanatory copy so review-queue states are assertable."""
        self.captions.append(str(text))

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
    monkeypatch.setattr(
        ipo_manual_page, "list_extraction_proposals", lambda **_kwargs: []
    )

    ipo_manual_page._render_ipo_manual_page(ADMIN)

    assert fake_st.errors == []
    assert any("ingestion" in message.lower() for message in fake_st.infos)
    assert any("no pending" in caption.lower() for caption in fake_st.captions)


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
    """The UI adapter should create the same exact three-period domain contract.

    Beginner note:
        This test checks values and citations together because preserving a number
        while shifting its page would still produce a valid-looking but unauditable
        extraction record.
    """
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
                "profit_before_tax": "13.25",
                "profit_before_tax_page": 13,
                "finance_cost": "1.5",
                "finance_cost_page": 14,
            }
            for year in (2023, 2024, 2025)
        ]
    )

    assert periods[0].period_end == dt.date(2023, 3, 31)
    assert periods[0].revenue == Decimal("2023.0000")
    assert periods[0].profit_before_tax == Decimal("13.2500")
    assert periods[0].finance_cost == Decimal("1.5000")


def test_form_mapping_builds_complete_domain_payload_without_actor_fields() -> None:
    """The UI adapter must not accept an entered-by identity from browser data.

    Beginner note:
        The browser controls every mapping value supplied here. Proving that the
        resulting DTO has no actor field protects the rule that identity comes only
        from the authenticated server session in the repository call.
    """
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
        "total_assets": "150",
        "total_assets_page": 142,
        "current_liabilities": "45",
        "current_liabilities_page": 143,
        "post_issue_equity_shares": "60",
        "post_issue_equity_shares_page": 144,
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
            "profit_before_tax": "12",
            "profit_before_tax_page": 103,
            "finance_cost": "2",
            "finance_cost_page": 104,
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
    assert payload.periods[2].profit_before_tax == Decimal("12.0000")
    assert payload.total_assets == Decimal("150.0000")
    assert payload.post_issue_equity_shares == Decimal("60.0000")
    assert not hasattr(payload, "entered_by_email")


class _PeerEditorFakeStreamlit:
    """Capture the DataFrame the peer grid hands to ``st.data_editor``.

    Beginner note:
    ``st.data_editor`` only renders columns that already exist in the DataFrame it
    receives; ``column_config`` for a missing column is silently ignored. This fake
    records that frame so a test can prove the metric columns are present even before
    any peer data exists, without launching a browser.
    """

    class _ColumnConfig:
        """Stand in for the ``st.column_config`` factory the grid calls."""

        def TextColumn(self, *_args, **_kwargs) -> None:
            """Accept a text-column spec without building a real widget config."""

        def NumberColumn(self, *_args, **_kwargs) -> None:
            """Accept a number-column spec without building a real widget config."""

    def __init__(self) -> None:
        """Prepare the capture slot and the column-config factory."""
        self.captured_frame: pd.DataFrame | None = None
        self.column_config = self._ColumnConfig()

    def markdown(self, *_args, **_kwargs) -> None:
        """Accept section headings without rendering a real browser widget."""

    def data_editor(self, dataframe, **_kwargs):
        """Record the incoming frame and echo it back like the real editor."""
        self.captured_frame = dataframe
        return dataframe


def test_peer_editor_seeds_every_metric_column_on_a_fresh_form(monkeypatch) -> None:
    """A first-time IPO entry must still expose all six peer-metric columns.

    Beginner note:
    Regression guard for a silent-drop defect: Streamlit ignores ``column_config``
    for columns absent from the DataFrame, so the grid must seed them itself. Without
    this the admin could never enter a peer metric for a brand-new IPO, and the
    domain (which requires at least one metric per peer) would reject every save.
    """
    fake_st = _PeerEditorFakeStreamlit()
    monkeypatch.setattr(ipo_manual_page, "st", fake_st)

    ipo_manual_page._render_peer_controls(5, None)

    assert fake_st.captured_frame is not None
    assert list(fake_st.captured_frame.columns) == [
        "company_name",
        "source_page",
        *[metric.value for metric in IpoPeerMetric],
    ]


def test_peer_rows_skip_blank_spare_row_with_nan_source_page() -> None:
    """A pandas ``NaN`` in the untouched spare row must not fail the whole form.

    Beginner note:
    ``st.data_editor`` returns ``float('nan')`` for an empty numeric cell, so the
    trailing spare row an admin never touched must be skipped rather than rejected as
    a nameless partial peer.
    """
    peers = ipo_manual_page._peer_rows_to_domain(
        [
            {"company_name": "Peer One Ltd", "source_page": 210, "pe": "20"},
            {"company_name": "", "source_page": float("nan"), "eps": ""},
        ]
    )

    assert len(peers) == 1
    assert peers[0].company_name == "Peer One Ltd"


def test_peer_rows_reject_named_row_even_with_nan_source_page() -> None:
    """A named peer whose page is a pandas ``NaN`` still surfaces a validation error."""
    with pytest.raises(IpoValidationError):
        ipo_manual_page._peer_rows_to_domain(
            [{"company_name": "Peer One Ltd", "source_page": float("nan"), "pe": "20"}]
        )


def _proposal_record(**overrides: Any) -> IpoExtractionProposalRecord:
    """Build one detached pending proposal for review-section smoke tests."""
    values: dict[str, Any] = {
        "id": 9,
        "issue_id": 1,
        "document_id": 3,
        "company_name": "Example Ltd",
        "document_url": "https://www.sebi.gov.in/filings/example-rhp.html",
        "status": IpoExtractionProposalStatus.PENDING,
        "payload": {"net_worth": "90", "net_worth_page": 2},
        "confidence": Confidence.MEDIUM,
        "needs_review_reasons": ("Could not independently verify total_debt (page 2).",),
        "model_version": "ipo-010-extractor-v1",
        "agent_model": "claude-sonnet-4-6",
        "source_content_sha256": "a" * 64,
        "page_count": 3,
        "created_at": dt.datetime(2026, 7, 13, 9, 0, tzinfo=dt.UTC),
        "reviewed_by_email": None,
        "reviewed_at": None,
        "review_note": None,
        "manual_extraction_id": None,
    }
    values.update(overrides)
    return IpoExtractionProposalRecord(**values)


class _ReviewFakeStreamlit(_FakeStreamlit):
    """Extend the message-capturing fake with the review-queue widget surface."""

    def __init__(self, *, button_clicks: dict[str, bool] | None = None) -> None:
        """Record which keyed buttons the scenario pretends were clicked."""
        super().__init__()
        self.button_clicks = button_clicks or {}
        self.warnings: list[str] = []
        self.successes: list[str] = []
        self.json_payloads: list[Any] = []
        self.text_inputs: dict[str, str] = {}

    def selectbox(self, _label, options, **_kwargs):
        """Return the first option like a freshly rendered selectbox."""
        return next(iter(options))

    def warning(self, text, **_kwargs) -> None:
        """Record verifier notes shown to the reviewer."""
        self.warnings.append(str(text))

    def success(self, text, **_kwargs) -> None:
        """Record one success confirmation."""
        self.successes.append(str(text))

    def json(self, payload, **_kwargs) -> None:
        """Record the payload the reviewer inspected."""
        self.json_payloads.append(payload)

    def expander(self, *_args, **_kwargs):
        """Provide the context manager shape of a real expander."""
        return contextlib.nullcontext()

    def columns(self, count: int):
        """Provide context-manager columns like the real layout helper."""
        return [contextlib.nullcontext() for _ in range(count)]

    def button(self, _label, *, key: str, **_kwargs) -> bool:
        """Report a click only for keys the scenario armed."""
        return self.button_clicks.get(key, False)

    def text_input(self, _label, *, key: str, **_kwargs) -> str:
        """Return the canned rejection reason for this widget key."""
        return self.text_inputs.get(key, "")


def test_review_section_approves_with_the_reviewer_identity(monkeypatch) -> None:
    """Approve must call the repository with the signed-in admin as attestor."""
    proposal = _proposal_record()
    fake_st = _ReviewFakeStreamlit(
        button_clicks={f"ipo_proposal_approve_{proposal.id}": True}
    )
    approvals: list[dict[str, Any]] = []

    def _approve(proposal_id: int, **kwargs: Any) -> SimpleNamespace:
        """Record the approval call and hand back a revision-like object."""
        approvals.append({"proposal_id": proposal_id, **kwargs})
        return SimpleNamespace(id=42)

    monkeypatch.setattr(ipo_manual_page, "st", fake_st)
    monkeypatch.setattr(
        ipo_manual_page, "list_extraction_proposals", lambda **_kwargs: [proposal]
    )
    monkeypatch.setattr(ipo_manual_page, "approve_extraction_proposal", _approve)

    ipo_manual_page._render_proposal_review(ADMIN)

    assert approvals[0]["proposal_id"] == proposal.id
    assert approvals[0]["reviewed_by_email"] == "admin@example.com"
    assert any("revision #42" in message for message in fake_st.successes)
    assert any("total_debt" in warning for warning in fake_st.warnings)
    assert fake_st.json_payloads == [dict(proposal.payload)]


def test_review_section_rejects_with_the_typed_reason(monkeypatch) -> None:
    """Reject must pass the typed reason through to the repository."""
    proposal = _proposal_record()
    fake_st = _ReviewFakeStreamlit(
        button_clicks={f"ipo_proposal_reject_{proposal.id}": True}
    )
    fake_st.text_inputs[f"ipo_proposal_reject_reason_{proposal.id}"] = (
        "Totals do not match the cited pages."
    )
    rejections: list[dict[str, Any]] = []

    def _reject(proposal_id: int, **kwargs: Any) -> SimpleNamespace:
        """Record the rejection call like the real repository function."""
        rejections.append({"proposal_id": proposal_id, **kwargs})
        return SimpleNamespace(id=proposal_id)

    monkeypatch.setattr(ipo_manual_page, "st", fake_st)
    monkeypatch.setattr(
        ipo_manual_page, "list_extraction_proposals", lambda **_kwargs: [proposal]
    )
    monkeypatch.setattr(ipo_manual_page, "reject_extraction_proposal", _reject)

    ipo_manual_page._render_proposal_review(ADMIN)

    assert rejections[0]["proposal_id"] == proposal.id
    assert rejections[0]["reason"] == "Totals do not match the cited pages."
    assert any("Rejected proposal" in message for message in fake_st.successes)


def test_review_section_surfaces_validation_errors_safely(monkeypatch) -> None:
    """A backend rejection (e.g. empty reason) renders as a redacted error."""
    proposal = _proposal_record()
    fake_st = _ReviewFakeStreamlit(
        button_clicks={f"ipo_proposal_reject_{proposal.id}": True}
    )

    def _reject(*_args: Any, **_kwargs: Any) -> SimpleNamespace:
        """Refuse like the real repository does for an empty reason."""
        raise IpoValidationError("A rejection requires a non-empty reason.")

    monkeypatch.setattr(ipo_manual_page, "st", fake_st)
    monkeypatch.setattr(
        ipo_manual_page, "list_extraction_proposals", lambda **_kwargs: [proposal]
    )
    monkeypatch.setattr(ipo_manual_page, "reject_extraction_proposal", _reject)

    ipo_manual_page._render_proposal_review(ADMIN)

    assert fake_st.successes == []
    assert any("non-empty reason" in message for message in fake_st.errors)
