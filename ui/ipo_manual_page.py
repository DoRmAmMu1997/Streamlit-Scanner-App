"""Administrator-only Streamlit page for IPO-004 manual prospectus entry.

Beginner note:
This module renders widgets and converts their values into strict IPO domain
objects; it never opens a database session or constructs SQL. The repository
owns persistence and revalidates document ownership/cache integrity, so a UI
bug cannot bypass the backend's provenance rules.
"""

from __future__ import annotations

import datetime as dt
import math
from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any, cast

import pandas as pd
import streamlit as st

from backend.auth.session import AuthenticatedUser
from backend.config import get_settings
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualExtractionRecord,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import IpoDocumentParseStatus, IpoValidationError
from backend.ipo.repository import (
    IpoNotFoundError,
    get_latest_manual_profile,
    list_documents,
    list_issues,
    list_manual_extractions,
    submit_manual_extraction,
)
from ui.common import _redact_secrets

# ``st.data_editor`` only renders columns that already exist in the DataFrame it is
# handed; ``column_config`` keys with no matching column are silently ignored. Seeding
# the peer grid with this full, ordered column set guarantees every metric column
# appears even on the very first (no prior revision) entry for an IPO.
_PEER_METRIC_COLUMNS: tuple[str, ...] = tuple(metric.value for metric in IpoPeerMetric)
_PEER_EDITOR_COLUMNS: tuple[str, ...] = ("company_name", "source_page", *_PEER_METRIC_COLUMNS)


def _is_blank(value: object) -> bool:
    """Treat ``None``, pandas ``NaN``, and empty/whitespace strings as an empty cell.

    Beginner note:
    ``st.data_editor`` returns a pandas DataFrame, and an empty numeric cell arrives
    as ``float('nan')`` rather than ``None`` (``nan in (None, "")`` is ``False``).
    Centralising the blank check here keeps the peer converter from mistaking a blank
    spare row for partially entered input.
    """
    if value is None:
        return True
    if isinstance(value, float) and math.isnan(value):
        return True
    return not str(value).strip()


def _decimal_text(value: object, field_name: str) -> Decimal:
    """Parse one required text cell as Decimal and raise a friendly field error."""
    text = str(value).strip() if value is not None else ""
    if not text:
        raise IpoValidationError(f"{field_name} is required.")
    try:
        return Decimal(text)
    except Exception as exc:  # noqa: BLE001 - Decimal exposes several parse failures.
        raise IpoValidationError(f"{field_name} must be numeric.") from exc


def _period_rows_to_domain(
    rows: Sequence[Mapping[str, object]],
) -> tuple[IpoManualPeriodData, ...]:
    """Convert three UI row mappings into strict sourced annual periods.

    Beginner note:
    Text widgets remain untrusted strings until this adapter parses them as exact
    Decimal values. Actor identity is not part of these mappings and cannot be
    smuggled into the backend DTO.
    """
    return tuple(
        IpoManualPeriodData(
            period_end=cast(dt.date, row["period_end"]),
            revenue=_decimal_text(row.get("revenue"), "revenue"),
            revenue_page=cast(int, row.get("revenue_page")),
            ebitda=_decimal_text(row.get("ebitda"), "ebitda"),
            ebitda_page=cast(int, row.get("ebitda_page")),
            pat=_decimal_text(row.get("pat"), "pat"),
            pat_page=cast(int, row.get("pat_page")),
            profit_before_tax=_decimal_text(
                row.get("profit_before_tax"), "profit_before_tax"
            ),
            profit_before_tax_page=cast(int, row.get("profit_before_tax_page")),
            finance_cost=_decimal_text(row.get("finance_cost"), "finance_cost"),
            finance_cost_page=cast(int, row.get("finance_cost_page")),
        )
        for row in rows
    )


def _peer_rows_to_domain(
    rows: Sequence[Mapping[str, object]],
) -> tuple[IpoPeerValuationData, ...]:
    """Convert dynamic editor rows while ignoring only completely blank rows.

    Beginner note:
    Streamlit keeps one empty row available for the next peer. Ignoring that
    wholly blank placeholder is safe, but a partly entered row is passed to the
    domain validator so missing pages/metrics are shown instead of silently lost.
    """
    peers: list[IpoPeerValuationData] = []
    for row in rows:
        raw_name = row.get("company_name")
        company_name = "" if _is_blank(raw_name) else str(raw_name).strip()
        page = row.get("source_page")
        raw_metrics = {
            metric: row.get(metric.value)
            for metric in IpoPeerMetric
            if not _is_blank(row.get(metric.value))
        }
        # Skip only the wholly blank spare row Streamlit's dynamic editor always
        # leaves at the bottom. ``_is_blank`` recognises the ``float('nan')`` pandas
        # puts in an empty numeric ``source_page`` cell, so an untouched spare row is
        # ignored instead of being rejected as a nameless partial peer. A row with a
        # name (or any metric) still reaches the domain validator, which reports the
        # missing page/metric.
        if not company_name and _is_blank(page) and not raw_metrics:
            continue
        peers.append(
            IpoPeerValuationData(
                company_name=company_name,
                source_page=cast(int, page),
                metrics={
                    metric: _decimal_text(value, f"peer {metric.value}")
                    for metric, value in raw_metrics.items()
                },
            )
        )
    return tuple(peers)


def _build_payload(
    *,
    source_document_id: int,
    financial_amount_unit: IpoAmountUnit,
    issue_amount_unit: IpoAmountUnit,
    equity_share_unit: IpoShareUnit,
    period_rows: Sequence[Mapping[str, object]],
    scalar_values: Mapping[str, object],
    peer_rows: Sequence[Mapping[str, object]],
) -> IpoManualExtractionData:
    """Build the backend DTO from browser values without accepting actor data.

    Beginner note:
    Listing every numeric field here is intentional mass-assignment protection.
    Unexpected browser keys are ignored, while missing or malformed reviewed keys
    fail in the same strict domain validator used by non-UI callers.
    """
    numeric_names = (
        "net_worth",
        "total_debt",
        "cash",
        "cash_flow_from_operations",
        "equity_shares",
        "eps",
        "nav_book_value",
        "fresh_issue_amount",
        "ofs_amount",
        "promoter_holding_pre_issue",
        "promoter_holding_post_issue",
        "total_assets",
        "current_liabilities",
        "post_issue_equity_shares",
    )
    values = dict(scalar_values)
    for name in numeric_names:
        values[name] = _decimal_text(values.get(name), name)
    return IpoManualExtractionData(
        source_document_id=source_document_id,
        financial_amount_unit=financial_amount_unit,
        issue_amount_unit=issue_amount_unit,
        equity_share_unit=equity_share_unit,
        periods=_period_rows_to_domain(period_rows),
        peers=_peer_rows_to_domain(peer_rows),
        net_worth=cast(Decimal, values["net_worth"]),
        net_worth_page=cast(int, values["net_worth_page"]),
        total_debt=cast(Decimal, values["total_debt"]),
        total_debt_page=cast(int, values["total_debt_page"]),
        cash=cast(Decimal, values["cash"]),
        cash_page=cast(int, values["cash_page"]),
        cash_flow_from_operations=cast(Decimal, values["cash_flow_from_operations"]),
        cash_flow_from_operations_page=cast(
            int, values["cash_flow_from_operations_page"]
        ),
        equity_shares=cast(Decimal, values["equity_shares"]),
        equity_shares_page=cast(int, values["equity_shares_page"]),
        eps=cast(Decimal, values["eps"]),
        eps_page=cast(int, values["eps_page"]),
        nav_book_value=cast(Decimal, values["nav_book_value"]),
        nav_book_value_page=cast(int, values["nav_book_value_page"]),
        objects_of_issue=cast(str, values["objects_of_issue"]),
        objects_of_issue_page=cast(int, values["objects_of_issue_page"]),
        fresh_issue_amount=cast(Decimal, values["fresh_issue_amount"]),
        fresh_issue_amount_page=cast(int, values["fresh_issue_amount_page"]),
        ofs_amount=cast(Decimal, values["ofs_amount"]),
        ofs_amount_page=cast(int, values["ofs_amount_page"]),
        promoter_holding_pre_issue=cast(
            Decimal, values["promoter_holding_pre_issue"]
        ),
        promoter_holding_pre_issue_page=cast(
            int, values["promoter_holding_pre_issue_page"]
        ),
        promoter_holding_post_issue=cast(
            Decimal, values["promoter_holding_post_issue"]
        ),
        promoter_holding_post_issue_page=cast(
            int, values["promoter_holding_post_issue_page"]
        ),
        total_assets=cast(Decimal, values["total_assets"]),
        total_assets_page=cast(int, values["total_assets_page"]),
        current_liabilities=cast(Decimal, values["current_liabilities"]),
        current_liabilities_page=cast(int, values["current_liabilities_page"]),
        post_issue_equity_shares=cast(
            Decimal, values["post_issue_equity_shares"]
        ),
        post_issue_equity_shares_page=cast(
            int, values["post_issue_equity_shares_page"]
        ),
    )


def _render_ipo_manual_page(authenticated_user: AuthenticatedUser | None) -> None:
    """Render the admin-only manual extraction workflow.

    Args:
        authenticated_user: Server-derived signed-in identity. ``None`` means no
            trusted actor is available and therefore submission is forbidden.

    Beginner note:
    The main app hides this view from non-admins, but this renderer repeats the
    check. That defense in depth protects direct/stale Streamlit reruns too.
    """
    if authenticated_user is None or not authenticated_user.is_admin:
        st.error("Admin access is required to enter IPO evidence.")
        return

    st.subheader("Admin IPO extraction")
    st.caption(
        "Transcribe a complete DRHP/RHP profile with page-level provenance. "
        "Each save creates a new immutable revision."
    )
    issues = list_issues()
    if not issues:
        st.info(
            "No IPO issues are available. Run the SEBI filing ingestion job before "
            "entering manual evidence."
        )
        return

    _render_entry_workflow(authenticated_user, issues)


def _render_entry_workflow(
    authenticated_user: AuthenticatedUser,
    issues: Sequence[Any],
) -> None:
    """Render issue selection, complete entry form, latest profile, and history."""
    issue_labels = {f"{issue.company_name} (#{issue.id})": issue for issue in issues}
    selected_label = st.selectbox("IPO issue", tuple(issue_labels))
    selected_issue = issue_labels[selected_label]
    documents = [
        document
        for document in list_documents(selected_issue.id)
        if document.document_type in {"drhp", "rhp"}
        and document.parse_status is IpoDocumentParseStatus.PENDING
        and document.content_sha256
        and document.file_path
    ]
    if not documents:
        st.info(
            "This IPO has no verified cached DRHP/RHP. Use the IPO-003 download "
            "service first; this form never downloads a source automatically."
        )
        return

    latest = get_latest_manual_profile(selected_issue.id)
    document_labels = {
        f"{document.document_type.upper()} - {document.filing_date or 'date unknown'} "
        f"(#{document.id})": document
        for document in documents
    }
    default_document_index = 0
    if latest is not None and latest.source_document_id is not None:
        document_ids = [document.id for document in document_labels.values()]
        if latest.source_document_id in document_ids:
            default_document_index = document_ids.index(latest.source_document_id)
    selected_document_label = st.selectbox(
        "Cached source document",
        tuple(document_labels),
        index=default_document_index,
        help="Only intact DRHP/RHP cache metadata is offered; bytes are re-hashed on save.",
        key=_widget_key(selected_issue.id, "source_document"),
    )
    selected_document = document_labels[selected_document_label]
    st.caption(
        f"Source SHA-256: {selected_document.content_sha256} | "
        f"URL: {selected_document.document_url}"
    )

    with st.form(f"ipo_manual_extraction_{selected_issue.id}"):
        financial_unit, issue_unit, share_unit = _render_unit_controls(
            selected_issue.id, latest
        )
        period_rows = _render_period_controls(selected_issue.id, latest)
        scalar_values = _render_scalar_controls(selected_issue.id, latest)
        peer_rows = _render_peer_controls(selected_issue.id, latest)
        submitted = st.form_submit_button("Save immutable revision", type="primary")

    if submitted:
        try:
            payload = _build_payload(
                source_document_id=selected_document.id,
                financial_amount_unit=financial_unit,
                issue_amount_unit=issue_unit,
                equity_share_unit=share_unit,
                period_rows=period_rows,
                scalar_values=scalar_values,
                peer_rows=peer_rows,
            )
            latest = submit_manual_extraction(
                selected_issue.id,
                payload,
                entered_by_email=authenticated_user.email,
                data_dir=get_settings().data_dir,
            )
        except (IpoValidationError, IpoNotFoundError) as exc:
            st.error(_redact_secrets(str(exc)))
        except Exception:  # noqa: BLE001 - UI must fail safely without raw exception text.
            st.error("The IPO revision could not be saved. Check logs for the safe error code.")
        else:
            st.success(f"Saved immutable IPO revision #{latest.id}.")

    _render_latest_and_history(selected_issue.id, latest)


def _format_decimal(value: Decimal | None, default: str = "0") -> str:
    """Format a stored Decimal for an editable text widget without float loss."""
    return format(value, "f") if value is not None else default


def _widget_key(issue_id: int, field_name: str) -> str:
    """Build the stable Streamlit key for one issue-specific form field.

    Beginner note:
    Streamlit carries widget state across page reruns. Including the issue ID
    prevents values entered for one company from leaking into another company's
    form when an administrator changes the issue selector.
    """
    return f"ipo_{issue_id}_{field_name}"


def _enum_index(options: Sequence[Any], selected: Any | None) -> int:
    """Return a safe selectbox index when an older record lacks a new option."""
    return options.index(selected) if selected in options else 0


def _render_unit_controls(
    issue_id: int,
    latest: IpoManualExtractionRecord | None,
) -> tuple[IpoAmountUnit, IpoAmountUnit, IpoShareUnit]:
    """Render source-reported unit selectors while preserving a revision's units."""
    amount_units = tuple(IpoAmountUnit)
    share_units = tuple(IpoShareUnit)
    columns = st.columns(3)
    financial_unit = columns[0].selectbox(
        "Financial statement unit",
        amount_units,
        index=_enum_index(
            amount_units, latest.financial_amount_unit if latest else IpoAmountUnit.CRORE_INR
        ),
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "financial_amount_unit"),
    )
    issue_unit = columns[1].selectbox(
        "Issue amount unit",
        amount_units,
        index=_enum_index(
            amount_units, latest.issue_amount_unit if latest else IpoAmountUnit.CRORE_INR
        ),
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "issue_amount_unit"),
    )
    share_unit = columns[2].selectbox(
        "Equity share unit",
        share_units,
        index=_enum_index(
            share_units, latest.equity_share_unit if latest else IpoShareUnit.LAKH_SHARES
        ),
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "equity_share_unit"),
    )
    return financial_unit, issue_unit, share_unit


def _render_period_controls(
    issue_id: int,
    latest: IpoManualExtractionRecord | None,
) -> list[dict[str, object]]:
    """Render three annual income-statement rows with value-level citations.

    Beginner note:
    PBT and finance cost are raw source facts, not ratios. Capturing them beside
    revenue/EBITDA/PAT lets the backend derive EBIT and coverage transparently.
    A legacy correction starts these new widgets blank so zero is never invented.
    """
    st.markdown("#### Annual financials")
    defaults = list(latest.periods) if latest else []
    current_year = dt.date.today().year
    rows: list[dict[str, object]] = []
    for index in range(3):
        period = defaults[index] if index < len(defaults) else None
        default_date = period.period_end if period else dt.date(current_year - 2 + index, 3, 31)
        st.markdown(f"**FY{index + 1}**")
        date_column, revenue_column, ebitda_column, pat_column = st.columns(4)
        pbt_column, finance_cost_column = st.columns(2)
        rows.append(
            {
                "period_end": date_column.date_input(
                    "Period end",
                    value=default_date,
                    key=_widget_key(issue_id, f"period_end_{index}"),
                ),
                "revenue": revenue_column.text_input(
                    "Revenue",
                    value=_format_decimal(period.revenue if period else None),
                    key=_widget_key(issue_id, f"revenue_{index}"),
                ),
                "revenue_page": revenue_column.number_input(
                    "Revenue page",
                    min_value=1,
                    step=1,
                    value=period.revenue_page if period else 1,
                    key=_widget_key(issue_id, f"revenue_page_{index}"),
                ),
                "ebitda": ebitda_column.text_input(
                    "EBITDA",
                    value=_format_decimal(period.ebitda if period else None),
                    key=_widget_key(issue_id, f"ebitda_{index}"),
                ),
                "ebitda_page": ebitda_column.number_input(
                    "EBITDA page",
                    min_value=1,
                    step=1,
                    value=period.ebitda_page if period else 1,
                    key=_widget_key(issue_id, f"ebitda_page_{index}"),
                ),
                "pat": pat_column.text_input(
                    "PAT",
                    value=_format_decimal(period.pat if period else None),
                    key=_widget_key(issue_id, f"pat_{index}"),
                ),
                "pat_page": pat_column.number_input(
                    "PAT page",
                    min_value=1,
                    step=1,
                    value=period.pat_page if period else 1,
                    key=_widget_key(issue_id, f"pat_page_{index}"),
                ),
                "profit_before_tax": pbt_column.text_input(
                    "Profit before tax (PBT)",
                    value=_format_decimal(
                        period.profit_before_tax if period else None, ""
                    ),
                    key=_widget_key(issue_id, f"profit_before_tax_{index}"),
                ),
                "profit_before_tax_page": pbt_column.number_input(
                    "PBT page",
                    min_value=1,
                    step=1,
                    value=(
                        period.profit_before_tax_page
                        if period and period.profit_before_tax_page is not None
                        else 1
                    ),
                    key=_widget_key(issue_id, f"profit_before_tax_page_{index}"),
                ),
                "finance_cost": finance_cost_column.text_input(
                    "Finance cost",
                    value=_format_decimal(
                        period.finance_cost if period else None, ""
                    ),
                    key=_widget_key(issue_id, f"finance_cost_{index}"),
                ),
                "finance_cost_page": finance_cost_column.number_input(
                    "Finance cost page",
                    min_value=1,
                    step=1,
                    value=(
                        period.finance_cost_page
                        if period and period.finance_cost_page is not None
                        else 1
                    ),
                    key=_widget_key(issue_id, f"finance_cost_page_{index}"),
                ),
            }
        )
    return rows


def _sourced_text_control(
    issue_id: int,
    label: str,
    field_name: str,
    latest: IpoManualExtractionRecord | None,
    *,
    blank_when_missing: bool = False,
) -> tuple[str, int]:
    """Render one required decimal input beside its required source page.

    ``blank_when_missing`` is reserved for IPO-005 additions on a legacy revision.
    Showing an empty widget forces an administrator to transcribe the real value;
    defaulting those newly introduced facts to zero would fabricate evidence.

    Beginner note:
        This helper only collects browser values. The domain object validates them
        again, and the repository later verifies the cached document, so a widget's
        minimum or default is never treated as the security or provenance boundary.
    """
    value_column, page_column = st.columns((3, 1))
    stored_value = getattr(latest, field_name) if latest else None
    stored_page = getattr(latest, f"{field_name}_page") if latest else None
    value = value_column.text_input(
        label,
        value=_format_decimal(stored_value, "" if blank_when_missing else "0"),
        key=_widget_key(issue_id, field_name),
    )
    page = page_column.number_input(
        f"{label} page",
        min_value=1,
        step=1,
        value=stored_page if stored_page is not None else 1,
        key=_widget_key(issue_id, f"{field_name}_page"),
    )
    return value, page


def _render_scalar_controls(
    issue_id: int,
    latest: IpoManualExtractionRecord | None,
) -> dict[str, object]:
    """Render every required singleton fact and its page-level provenance.

    Beginner note:
    Historical/current shares support EPS and book value, while post-issue shares
    support market capitalization. Keeping separate fields avoids a convenient but
    misleading EV calculation based on the wrong share count.
    """
    st.markdown("#### Balance sheet, issue, and ownership facts")
    labels = {
        "net_worth": ("Net worth", False),
        "total_debt": ("Total debt", False),
        "cash": ("Cash", False),
        "cash_flow_from_operations": ("Cash flow from operations", False),
        "equity_shares": ("Equity shares used for EPS / book value", False),
        "eps": ("Reported EPS (INR per share)", False),
        "nav_book_value": ("Reported NAV / book value (INR per share)", False),
        "fresh_issue_amount": ("Fresh issue amount", False),
        "ofs_amount": ("OFS amount", False),
        "promoter_holding_pre_issue": ("Promoter holding before issue (%)", False),
        "promoter_holding_post_issue": ("Promoter holding after issue (%)", False),
        "total_assets": ("Total assets", True),
        "current_liabilities": ("Current liabilities", True),
        "post_issue_equity_shares": ("Post-issue equity shares", True),
    }
    values: dict[str, object] = {}
    for field_name, (label, blank_when_missing) in labels.items():
        value, page = _sourced_text_control(
            issue_id,
            label,
            field_name,
            latest,
            blank_when_missing=blank_when_missing,
        )
        values[field_name] = value
        values[f"{field_name}_page"] = page

    text_column, page_column = st.columns((3, 1))
    values["objects_of_issue"] = text_column.text_area(
        "Objects of issue",
        value=latest.objects_of_issue if latest else "",
        height=140,
        key=_widget_key(issue_id, "objects_of_issue"),
    )
    values["objects_of_issue_page"] = page_column.number_input(
        "Objects page",
        min_value=1,
        step=1,
        value=latest.objects_of_issue_page if latest else 1,
        key=_widget_key(issue_id, "objects_of_issue_page"),
    )
    return values


def _render_peer_controls(
    issue_id: int,
    latest: IpoManualExtractionRecord | None,
) -> list[dict[str, object]]:
    """Render dynamic peer rows with allowlisted metric columns only."""
    st.markdown("#### Peer valuation")
    rows = []
    if latest is not None:
        rows = [
            {
                "company_name": peer.company_name,
                "source_page": peer.source_page,
                **{
                    metric.value: _format_decimal(peer.metrics.get(metric), "")
                    for metric in IpoPeerMetric
                },
            }
            for peer in latest.peers
        ]
    if not rows:
        # First entry for this IPO: seed one empty row that already carries every
        # metric column. Without these keys the metric columns are absent from the
        # DataFrame, ``st.data_editor`` drops them, and the admin cannot enter any
        # peer metric — which the domain requires, so the save would always fail.
        rows = [
            {"company_name": "", "source_page": None, **dict.fromkeys(_PEER_METRIC_COLUMNS, "")}
        ]
    edited = st.data_editor(
        # Pin the column set/order so every configured column renders regardless of
        # whether a prior revision happened to supply it.
        pd.DataFrame(rows, columns=list(_PEER_EDITOR_COLUMNS)),
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_config={
            "company_name": st.column_config.TextColumn("Peer company", required=True),
            "source_page": st.column_config.NumberColumn(
                "Source page", min_value=1, step=1, required=True
            ),
            **{
                metric.value: st.column_config.TextColumn(
                    metric.value.replace("_", " ").upper()
                )
                for metric in IpoPeerMetric
            },
        },
        key=_widget_key(issue_id, "peer_editor"),
    )
    # to_dict("records") types its keys as Hashable; the editor's columns are
    # the string names configured above.
    return cast(list[dict[str, object]], edited.to_dict(orient="records"))


def _render_latest_and_history(
    issue_id: int,
    latest: IpoManualExtractionRecord | None,
) -> None:
    """Show the canonical latest profile identity and immutable revision ledger."""
    if latest is None:
        st.info("No manual extraction revision has been submitted for this IPO yet.")
        return
    st.markdown("#### Latest manual profile")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "revision_id": latest.id,
                    "submitted_at": latest.submitted_at,
                    "entered_by": latest.entered_by_email,
                    "source_sha256": latest.source_content_sha256,
                    "net_worth_inr": latest.net_worth_inr,
                    "canonical_shares": latest.equity_shares_canonical,
                }
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
    history = list_manual_extractions(issue_id)
    st.markdown("#### Revision history")
    st.dataframe(
        pd.DataFrame(
            [
                {
                    "revision_id": record.id,
                    "submitted_at": record.submitted_at,
                    "entered_by": record.entered_by_email,
                    "document_id": record.source_document_id,
                    "peer_count": len(record.peers),
                }
                for record in history
            ]
        ),
        hide_index=True,
        use_container_width=True,
    )
