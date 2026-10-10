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
from collections.abc import Mapping, MutableMapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, cast

import pandas as pd
import streamlit as st

from backend.auth.session import AuthenticatedUser
from backend.config import get_settings
from backend.ipo.approval import IpoManualEditContext, current_document_id, same_source
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualExtractionRecord,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    IpoDocumentParseStatus,
    IpoExtractionProposalRecord,
    IpoExtractionProposalStatus,
    IpoValidationError,
)
from backend.ipo.repository import (
    IpoNotFoundError,
    approve_extraction_proposal,
    get_latest_manual_profile,
    list_documents,
    list_extraction_proposals,
    list_issues,
    list_manual_extractions,
    load_manual_edit_context,
    reject_extraction_proposal,
    submit_manual_extraction,
)
from ui.common import _neutralize_markdown, _redact_secrets

# ``st.data_editor`` only renders columns that already exist in the DataFrame it is
# handed; ``column_config`` keys with no matching column are silently ignored. Seeding
# the peer grid with this full, ordered column set guarantees every metric column
# appears even on the very first (no prior revision) entry for an IPO.
_PEER_METRIC_COLUMNS: tuple[str, ...] = tuple(metric.value for metric in IpoPeerMetric)
_PEER_EDITOR_COLUMNS: tuple[str, ...] = ("company_name", "source_page", *_PEER_METRIC_COLUMNS)


@dataclass(frozen=True)
class _ManualDraft:
    """Retain one source-bound editing snapshot across Streamlit reruns.

    Attributes:
        context: Authority captured when editing began, retained until reload.
        seed: Approved values allowed to initialize fresh widgets, if any.
        form_key: Namespace binding issue, document, hash, revision and generation.
        copied: Whether pages must be cleared and every copied fact reverified.

    Beginner note:
        Latest database state and the user's loaded draft are different things.
        Replacing the context on a routine rerun could authorize stale typed
        values against a newer revision. Keep that baseline immutable instead.
    """

    context: IpoManualEditContext
    seed: IpoManualExtractionRecord | None
    form_key: str
    copied: bool = False


def _manual_draft(
    current: IpoManualEditContext,
    *,
    state: MutableMapping[str, Any],
    reload: bool = False,
    copy_previous: bool = False,
) -> _ManualDraft:
    """Reuse a loaded draft or explicitly create a fresh widget namespace.

    Args:
        current: Coherent, newly read source/manual snapshot from the backend.
        state: This browser session's mutable state, supplied by the page.
        reload: Explicitly discard old inputs and capture current authority.
        copy_previous: Explicitly seed values from the draft's captured prior
            evidence. It never refreshes the retained authority baseline.

    Returns:
        Draft whose seed and copy flag tell renderers which values to show and
        whether citation pages must start blank before source verification.

    Raises:
        IpoValidationError: If reload and copy are requested together or there
            is no captured previous evidence to copy.

    Beginner note:
        Widget values persist by key. Each source switch or explicit reset gets
        a new generation, so A-B-A cannot revive A's abandoned values. Only the
        old draft's field namespace is cleared; source selection and other pages
        survive. Routine reruns keep both input widgets and the original baseline.
    """
    if reload and copy_previous:
        raise IpoValidationError("Reload evidence before starting a separate copy action.")
    stored = state.get("ipo_manual_edit_draft")
    previous = stored if isinstance(stored, _ManualDraft) else None
    same = previous is not None and same_source(previous.context.document, current.document)
    if same and not reload and not copy_previous:
        return cast(_ManualDraft, previous)

    # Copying uses the captured previous evidence, even if an external save has
    # since changed the database. Only explicit reload may advance the baseline.
    context = previous.context if same and previous is not None and not reload else current
    profile = context.latest_profile
    if copy_previous and profile is None:
        raise IpoValidationError("There is no saved evidence to copy.")
    same_seed_source = (
        profile is not None
        and profile.source_document_id == context.document.id
        and profile.source_content_sha256 == context.document.content_sha256
        and profile.source_document_url == context.document.document_url
    )
    seed = profile if copy_previous or same_seed_source else None
    prior_generation = state.get("ipo_manual_draft_generation", 0)
    generation = prior_generation + 1 if type(prior_generation) is int and prior_generation >= 0 else 1
    form_key = (
        f"draft_{context.issue.id}_{context.document.id}_{context.document.content_sha256}"
        f"_{context.manual_revision}_{generation}"
    )
    if previous is not None:
        prefix = _widget_key(previous.form_key, "")
        # Iterate a snapshot because deleting keys mutates the supplied mapping.
        # Action buttons use a different prefix and may already be instantiated;
        # these field widgets have not been rendered in the new form yet.
        for key in list(state):
            if key.startswith(prefix):
                del state[key]
    draft = _ManualDraft(context=context, seed=seed, form_key=form_key, copied=copy_previous)
    state["ipo_manual_draft_generation"] = generation
    state["ipo_manual_edit_draft"] = draft
    return draft


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
    _render_proposal_review(authenticated_user)
    issues = list_issues()
    if not issues:
        st.info(
            "No IPO issues are available. Run the SEBI filing ingestion job before "
            "entering manual evidence."
        )
        return

    _render_entry_workflow(authenticated_user, issues)


def _proposal_label(proposal: IpoExtractionProposalRecord) -> str:
    """Build one stable, Markdown-neutral review-queue entry label.

    Beginner note:
        Issuer names originate outside the UI. Escaping Markdown controls
        prevents a crafted name from turning a select-box label into an image,
        link, or misleading formatted instruction.
    """
    return _neutralize_markdown(
        f"{proposal.company_name} - proposal #{proposal.id} "
        f"({proposal.confidence.value} confidence)"
    )


def _render_proposal_review(authenticated_user: AuthenticatedUser) -> None:
    """Render the IPO-010 review queue for pending AI extraction proposals.

    Beginner note:
    This section is the human half of the fail-closed trust model: the agent
    only ever queues *proposals*, and the buttons below are the sole path that
    turns one into scoring evidence. Approval re-runs the full manual
    validation (including re-hashing the cached PDF), so a reviewer's click is
    an attestation, not a rubber stamp.
    """
    pending = list_extraction_proposals(status=IpoExtractionProposalStatus.PENDING)
    st.markdown("**Review AI extraction proposals**")
    if not pending:
        st.caption("No pending AI extraction proposals.")
        return

    labels = {_proposal_label(proposal): proposal for proposal in pending}
    selected_label = st.selectbox(
        "Pending proposal", tuple(labels), key="ipo_proposal_review_select"
    )
    proposal = labels[selected_label]
    st.caption(
        _neutralize_markdown(
            f"Document: {proposal.document_url} | pages seen: {proposal.page_count} | "
            f"agent model: {proposal.agent_model} | extractor: {proposal.model_version} | "
            f"source SHA-256: {proposal.source_content_sha256}"
        )
    )
    if proposal.needs_review_reasons:
        st.warning(
            "Verifier notes:\n"
            + "\n".join(
                f"- {_neutralize_markdown(reason)}"
                for reason in proposal.needs_review_reasons
            )
        )
    with st.expander("Proposed values (with page citations)", expanded=False):
        st.json(dict(proposal.payload))

    review_ready = False
    accept_authority_change = False
    if proposal.baseline_manual_revision is None or proposal.document_id is None:
        st.warning("This proposal has no trusted manual baseline. "
                   "Use fresh manual review below; rejection remains available.")
    else:
        try:
            context = load_manual_edit_context(proposal.issue_id, proposal.document_id)
            review_ready = context.manual_revision == proposal.baseline_manual_revision
            if not review_ready:
                st.warning("Newer manual evidence exists. Reload and re-enter reviewed evidence below; "
                           "this draft cannot be approved.")
            else:
                inventory = list_documents(proposal.issue_id)
                cap = proposal.payload.get("price_band_high")
                changed_cap = cap is not None and context.issue.price_band_high is not None and (
                    Decimal(str(cap)) != context.issue.price_band_high
                )
                previous = context.latest_profile
                source = next((item for item in inventory if previous is not None
                               and item.id == previous.source_document_id), None)
                unknown_previous = previous is not None and (
                    source is None or source.filing_date is None
                    or source.document_url != previous.source_document_url
                    or source.content_sha256 != previous.source_content_sha256
                )
                if current_document_id(inventory) != proposal.document_id or changed_cap or unknown_previous:
                    st.warning("Review this source's chronology and the recorded issue terms "
                               "before replacing evidence.")
                    st.caption(_neutralize_markdown(_redact_secrets(
                        f"Recorded cap: {context.issue.price_band_high}; proposed cap: {cap or 'unchanged'}."
                    )))
                    accept_authority_change = st.checkbox(
                        "I explicitly accept this source and any changed issue terms after reviewing them",
                        key=f"ipo_proposal_authority_{proposal.id}",
                    )
                st.caption("The manual baseline is current. "
                           "Source bytes and citations will be checked again on approval.")
        except Exception:  # noqa: BLE001 - a failed authority read must disable approval without leaking storage details.
            review_ready = False
            st.warning("Current evidence could not be checked. Reload before approving this proposal.")

    approve_column, reject_column = st.columns(2)
    with approve_column:
        approve_clicked = st.button(
            "Approve as immutable revision",
            key=f"ipo_proposal_approve_{proposal.id}",
            type="primary",
            disabled=not review_ready,
        )
    with reject_column:
        reject_reason = st.text_input(
            "Rejection reason", key=f"ipo_proposal_reject_reason_{proposal.id}"
        )
        reject_clicked = st.button(
            "Reject proposal", key=f"ipo_proposal_reject_{proposal.id}"
        )

    if approve_clicked and review_ready:
        try:
            revision = approve_extraction_proposal(
                proposal.id,
                reviewed_by_email=authenticated_user.email,
                accept_authority_change=accept_authority_change,
                data_dir=get_settings().data_dir,
            )
        except (IpoValidationError, IpoNotFoundError) as exc:
            st.error(_neutralize_markdown(_redact_secrets(str(exc))))
        except Exception:  # noqa: BLE001 - UI must fail safely without raw exception text.
            st.error(
                "The proposal could not be approved. Check logs for the safe error code."
            )
        else:
            st.success(
                f"Approved proposal #{proposal.id} as immutable revision #{revision.id}."
            )
    if reject_clicked:
        try:
            reject_extraction_proposal(
                proposal.id,
                reviewed_by_email=authenticated_user.email,
                reason=reject_reason,
            )
        except (IpoValidationError, IpoNotFoundError) as exc:
            st.error(_neutralize_markdown(_redact_secrets(str(exc))))
        except Exception:  # noqa: BLE001 - UI must fail safely without raw exception text.
            st.error(
                "The proposal could not be rejected. Check logs for the safe error code."
            )
        else:
            st.success(f"Rejected proposal #{proposal.id}.")


def _render_entry_workflow(
    authenticated_user: AuthenticatedUser,
    issues: Sequence[Any],
) -> None:
    """Render source-bound evidence editing while retaining the loaded baseline.

    Args:
        authenticated_user: Trusted administrator identity used for attribution.
        issues: Available issues already loaded by the authorized page.

    Returns:
        None. Renders fields, safe validation messages and immutable history.

    Beginner note:
        Only verified cached DRHP/RHP records are offered. Selecting a source
        does not trust its metadata forever: the repository re-hashes its bytes
        during submission before it creates an immutable revision. Each draft
        retains its original context across reruns; newer evidence prompts an
        explicit reload rather than silently rebinding old typed values.
    """
    issue_labels = {
        _neutralize_markdown(f"{issue.company_name} (#{issue.id})"): issue
        for issue in issues
    }
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
        _neutralize_markdown(
            f"{document.document_type.upper()} - "
            f"{document.filing_date or 'date unknown'} (#{document.id})"
        ): document
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
        _neutralize_markdown(
            f"Source SHA-256: {selected_document.content_sha256} | "
            f"URL: {selected_document.document_url}"
        )
    )
    try:
        current_context = load_manual_edit_context(selected_issue.id, selected_document.id)
    except (IpoValidationError, IpoNotFoundError) as exc:
        st.error(_neutralize_markdown(_redact_secrets(str(exc))))
        return
    except Exception:  # noqa: BLE001 - storage errors must not expose connection details.
        st.error("The selected evidence could not be loaded. Reload the page and try again.")
        return
    reload_after_save = bool(st.session_state.pop("ipo_manual_reload_after_save", False))
    # Streamlit accepts integer or string keys and normalizes them to strings;
    # this helper owns only its explicit string namespace within that mapping.
    draft_state = cast(MutableMapping[str, Any], st.session_state)
    draft = _manual_draft(current_context, state=draft_state, reload=reload_after_save)
    notice = st.session_state.pop("ipo_manual_save_notice", None)
    if isinstance(notice, str):
        st.success(notice)
    stale = draft.context.manual_revision != current_context.manual_revision
    if stale:
        st.warning("Newer evidence has been saved. Reload latest evidence before saving this draft.")
    reload_column, copy_column = st.columns(2)
    if reload_column.button("Reload latest evidence", key=f"ipo_manual_reload_{draft.form_key}"):
        draft = _manual_draft(current_context, state=draft_state, reload=True)
        stale = False
    if copy_column.button(
        "Copy previous values", key=f"ipo_manual_copy_{draft.form_key}",
        disabled=stale or draft.context.latest_profile is None,
        help="Copy values and units, then enter and verify page citations for this prospectus.",
    ):
        draft = _manual_draft(current_context, state=draft_state, copy_previous=True)
    if draft.copied and draft.seed is not None:
        st.caption(_neutralize_markdown(_redact_secrets(
            f"Values copied from revision #{draft.seed.id} ({draft.seed.source_document_url}). "
            "Enter citations for this prospectus."
        )))

    # Buttons above use a separate namespace; resets can clear the old source's
    # field keys before these widgets are instantiated. Source selection survives.
    with st.form(f"ipo_manual_extraction_{draft.form_key}"):
        financial_unit, issue_unit, share_unit = _render_unit_controls(
            draft.form_key, draft.seed
        )
        period_rows = _render_period_controls(draft.form_key, draft.seed, clear_citations=draft.copied)
        scalar_values = _render_scalar_controls(draft.form_key, draft.seed, clear_citations=draft.copied)
        peer_rows = _render_peer_controls(draft.form_key, draft.seed, clear_citations=draft.copied)
        submitted = st.form_submit_button("Save immutable revision", type="primary", disabled=stale)

    if submitted:
        try:
            if financial_unit is None or issue_unit is None or share_unit is None:
                raise IpoValidationError("Select the reported financial, issue and share units before saving.")
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
                expected_context=draft.context,
                verify_copied_citations=draft.copied,
            )
        except (IpoValidationError, IpoNotFoundError) as exc:
            st.error(_neutralize_markdown(_redact_secrets(str(exc))))
        except Exception:  # noqa: BLE001 - UI must fail safely without raw exception text.
            st.error("The IPO revision could not be saved. Check logs for the safe error code.")
        else:
            # The next run reads the committed revision and creates new field
            # keys. Do not mutate already-instantiated widget values in this run.
            st.session_state["ipo_manual_save_notice"] = f"Saved immutable IPO revision #{latest.id}."
            st.session_state["ipo_manual_reload_after_save"] = True
            st.rerun()

    _render_latest_and_history(selected_issue.id, current_context.latest_profile)


def _format_decimal(value: Decimal | None, default: str = "") -> str:
    """Format reported Decimal text while keeping missing facts visibly empty.

    Args:
        value: Exact stored value, or None for an unentered fact.
        default: Text to show for a missing value; blank by default.

    Returns:
        Decimal text without a float conversion, or the supplied empty default.

    Beginner note:
        Zero is a financial assertion. A fresh source must not silently acquire
        zero-valued facts merely because its form has not been filled in yet.
    """
    return format(value, "f") if value is not None else default


def _widget_key(issue_id: int | str, field_name: str) -> str:
    """Build a field key from a legacy issue ID or a complete draft namespace.

    Args:
        issue_id: Integer for legacy callers or the page's source-bound form key.
        field_name: Stable field name; empty text returns its namespace prefix.

    Returns:
        A key preserving existing integer-ID compatibility while allowing the
        form to bind fields to document, hash, loaded revision and generation.

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
    issue_id: int | str,
    latest: IpoManualExtractionRecord | None,
) -> tuple[IpoAmountUnit | None, IpoAmountUnit | None, IpoShareUnit | None]:
    """Render reported units from the approved seed or require an explicit choice.

    Args:
        issue_id: Legacy integer ID or the current source-bound draft namespace.
        latest: Approved same-source values or explicitly copied values to seed.

    Returns:
        Financial, issue and share units; any unselected unit remains None.

    Beginner note:
        A missing source unit must not silently become crores or lakhs. Explicit
        copy may preserve the previous units, but the backend verifies them anew
        against every cited fact in the selected PDF before copied data is saved.
    """
    amount_units = tuple(IpoAmountUnit)
    share_units = tuple(IpoShareUnit)
    columns = st.columns(3)
    financial_unit = columns[0].selectbox(
        "Financial statement unit",
        amount_units,
        index=_enum_index(amount_units, latest.financial_amount_unit) if latest else None,
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "financial_amount_unit"),
    )
    issue_unit = columns[1].selectbox(
        "Issue amount unit",
        amount_units,
        index=_enum_index(amount_units, latest.issue_amount_unit) if latest else None,
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "issue_amount_unit"),
    )
    share_unit = columns[2].selectbox(
        "Equity share unit",
        share_units,
        index=_enum_index(share_units, latest.equity_share_unit) if latest else None,
        format_func=lambda unit: unit.value.replace("_", " ").upper(),
        key=_widget_key(issue_id, "equity_share_unit"),
    )
    return financial_unit, issue_unit, share_unit


def _render_period_controls(
    issue_id: int | str,
    latest: IpoManualExtractionRecord | None,
    *,
    clear_citations: bool = False,
) -> list[dict[str, object]]:
    """Render three annual income-statement rows with value-level citations.

    Args:
        issue_id: Legacy integer ID or source-bound draft namespace.
        latest: Approved values used only to initialize this namespace.
        clear_citations: Whether copied values require newly entered pages.

    Returns:
        Browser values for three periods; dates and pages stay empty when unknown.

    Beginner note:
    PBT and finance cost are raw source facts, not ratios. Capturing them beside
    revenue/EBITDA/PAT lets the backend derive EBIT and coverage transparently.
    A legacy correction starts these new widgets blank so zero is never invented.
    """
    st.markdown("#### Annual financials")
    defaults = list(latest.periods) if latest else []
    rows: list[dict[str, object]] = []
    for index in range(3):
        period = defaults[index] if index < len(defaults) else None
        default_date = period.period_end if period else None
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
                    value=period.revenue_page if period and not clear_citations else None,
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
                    value=period.ebitda_page if period and not clear_citations else None,
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
                    value=period.pat_page if period and not clear_citations else None,
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
                        if period and not clear_citations
                        else None
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
                        if period and not clear_citations
                        else None
                    ),
                    key=_widget_key(issue_id, f"finance_cost_page_{index}"),
                ),
            }
        )
    return rows


def _sourced_text_control(
    issue_id: int | str,
    label: str,
    field_name: str,
    latest: IpoManualExtractionRecord | None,
    *,
    clear_citations: bool = False,
) -> tuple[str, int | None]:
    """Render one required decimal input beside its required source page.

    Args:
        issue_id: Legacy integer ID or source-bound draft namespace.
        label: Human-readable field name.
        field_name: Attribute and widget suffix for the financial value.
        latest: Approved seed, if the user has chosen to reuse its values.
        clear_citations: Whether a copied value needs a new source page.

    Returns:
        Decimal text and a page number, with missing values/pages left blank.

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
        value=_format_decimal(stored_value),
        key=_widget_key(issue_id, field_name),
    )
    page = page_column.number_input(
        f"{label} page",
        min_value=1,
        step=1,
        value=stored_page if not clear_citations else None,
        key=_widget_key(issue_id, f"{field_name}_page"),
    )
    return value, page


def _render_scalar_controls(
    issue_id: int | str,
    latest: IpoManualExtractionRecord | None,
    *,
    clear_citations: bool = False,
) -> dict[str, object]:
    """Render every required singleton fact and its page-level provenance.

    Args:
        issue_id: Legacy integer ID or source-bound draft namespace.
        latest: Approved seed for values and, when permitted, page references.
        clear_citations: Whether copied values must start without source pages.

    Returns:
        Named browser values and page references for strict payload conversion.

    Beginner note:
    Historical/current shares support EPS and book value, while post-issue shares
    support market capitalization. Keeping separate fields avoids a convenient but
    misleading EV calculation based on the wrong share count.
    """
    st.markdown("#### Balance sheet, issue, and ownership facts")
    labels = {
        "net_worth": "Net worth",
        "total_debt": "Total debt",
        "cash": "Cash",
        "cash_flow_from_operations": "Cash flow from operations",
        "equity_shares": "Equity shares used for EPS / book value",
        "eps": "Reported EPS (INR per share)",
        "nav_book_value": "Reported NAV / book value (INR per share)",
        "fresh_issue_amount": "Fresh issue amount",
        "ofs_amount": "OFS amount",
        "promoter_holding_pre_issue": "Promoter holding before issue (%)",
        "promoter_holding_post_issue": "Promoter holding after issue (%)",
        "total_assets": "Total assets",
        "current_liabilities": "Current liabilities",
        "post_issue_equity_shares": "Post-issue equity shares",
    }
    values: dict[str, object] = {}
    for field_name, label in labels.items():
        value, page = _sourced_text_control(
            issue_id,
            label,
            field_name,
            latest,
            clear_citations=clear_citations,
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
        value=latest.objects_of_issue_page if latest and not clear_citations else None,
        key=_widget_key(issue_id, "objects_of_issue_page"),
    )
    return values


def _render_peer_controls(
    issue_id: int | str,
    latest: IpoManualExtractionRecord | None,
    *,
    clear_citations: bool = False,
) -> list[dict[str, object]]:
    """Render peer values in this draft's namespace with source pages kept separate.

    Args:
        issue_id: Legacy integer ID or source-bound draft namespace.
        latest: Approved peer rows to seed, if any.
        clear_citations: Whether copied rows need new source-page references.

    Returns:
        Edited peer dictionaries containing only the configured metric columns.

    Beginner note:
        Copying a peer's numbers does not copy its page authority. Clear that
        column while retaining useful values, and keep the data editor under the
        same source-bound namespace as every other financial field.
    """
    st.markdown("#### Peer valuation")
    rows = []
    if latest is not None:
        rows = [
            {
                "company_name": peer.company_name,
                "source_page": None if clear_citations else peer.source_page,
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
