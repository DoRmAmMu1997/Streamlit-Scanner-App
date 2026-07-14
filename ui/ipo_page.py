"""Read-only IPO screener dashboard page (IPO-007).

Beginner note:
This page renders whatever ``backend.ipo.dashboard`` assembled and nothing
else. It performs no network call and no scoring during render — the compute
pass is the ``run_ipo_screener`` job, and the only mutation this page can
trigger is the explicit, capability-gated re-score button, which runs the
same repository-only scoring service the job uses.
"""

from __future__ import annotations

import pandas as pd
import streamlit as st

from backend.audit import record_audit_event
from backend.ipo.dashboard import (
    IpoDashboardRow,
    IpoDashboardSnapshot,
    build_dashboard_snapshot,
    section_available_filings,
    section_drhp_watchlist,
    section_missing_data_queue,
    section_not_recommended,
    section_open,
    section_recommended,
    section_upcoming,
)
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    INSUFFICIENT_VERIFIED_DATA,
    SKIP,
)
from backend.ipo.scoring.service import rescore_issue
from backend.observability import EVENT_IPO_RESCORE_TRIGGERED
from ui.common import _csv_safe

# Pure display mapping (IPO-006 decision): the database keeps its four stable
# recommendation_type strings; the dashboard shows the sprint's friendlier
# wording. Every stored type MUST have an entry here — a policy test pins it.
_RECOMMENDATION_TYPE_LABELS: dict[str, str] = {
    APPLY_AND_HOLD: "Recommended - high conviction",
    APPLY_FOR_LISTING_GAINS: "Recommended - selective / listing-gain oriented",
    SKIP: "Not Recommended",
    INSUFFICIENT_VERIFIED_DATA: "Not Recommended - insufficient verified data",
}

# The spec's section order, top to bottom.
_SECTIONS = (
    ("Available filings", section_available_filings),
    ("Open IPOs", section_open),
    ("Upcoming IPOs", section_upcoming),
    ("DRHP watchlist", section_drhp_watchlist),
    ("Recommended IPOs", section_recommended),
    ("Not Recommended IPOs", section_not_recommended),
    ("Missing data queue", section_missing_data_queue),
)

_VERDICT_FILTERS = ("All", "Recommended", "Not Recommended")


@st.cache_data(ttl=300, show_spinner=False)
def _load_snapshot() -> IpoDashboardSnapshot:
    """Read (and briefly cache) the dashboard snapshot for this session.

    Beginner note:
        ``st.cache_data`` keeps Streamlit's rerun-per-interaction model cheap:
        clicking a filter does not re-read every issue. The re-score handler
        calls ``.clear()`` so its fresh evaluations appear immediately.
    """
    return build_dashboard_snapshot()


def _verdict_label(row: IpoDashboardRow) -> str:
    """Map one stored recommendation_type onto its display wording."""
    if row.recommendation_type is None:
        return "Not scored yet"
    return _RECOMMENDATION_TYPE_LABELS.get(row.recommendation_type, row.recommendation_type)


def _apply_verdict_filter(
    rows: tuple[IpoDashboardRow, ...], choice: str
) -> tuple[IpoDashboardRow, ...]:
    """Narrow rows to one binary verdict; unscored rows only pass 'All'."""
    if choice == "All":
        return rows
    return tuple(row for row in rows if row.recommendation == choice)


def _rows_frame(rows: tuple[IpoDashboardRow, ...]) -> pd.DataFrame:
    """Shape dashboard rows into the display table (pure, no Streamlit).

    Every cell is plain text so ``_csv_safe`` protects any future export the
    same way the scan-history tables are protected.
    """
    return pd.DataFrame(
        [
            {
                "Company": row.company_name,
                "Issue status": row.issue_status.value,
                "Score": str(row.score) if row.score is not None else "",
                "Recommendation": _verdict_label(row),
                "Confidence": row.confidence or "",
                "Top positives": "; ".join(row.top_positives),
                "Top risks": "; ".join((*row.triggered_flags, *row.top_risks)),
                "Missing data": "; ".join(row.missing_data)
                or ("" if row.has_manual_profile else "manual extraction"),
                "Pending proposals": row.pending_proposals,
                "Documents": f"{row.documents_downloaded}/{row.documents_total}",
                "Source documents": "; ".join(row.source_documents),
                "Last updated": row.last_updated.isoformat() if row.last_updated else "",
            }
            for row in rows
        ]
    )


def _render_section(title: str, rows: tuple[IpoDashboardRow, ...]) -> None:
    """Render one titled section as a table, or an honest empty note."""
    st.markdown(f"**{title} ({len(rows)})**")
    if not rows:
        st.caption("No issues in this section.")
        return
    st.dataframe(_csv_safe(_rows_frame(rows)), hide_index=True)


def _render_breakdowns(rows: tuple[IpoDashboardRow, ...]) -> None:
    """Render one expander per scored issue with the full verdict receipt."""
    scored = [row for row in rows if row.score is not None]
    if not scored:
        return
    st.markdown("**Score breakdowns**")
    for row in scored:
        with st.expander(f"{row.company_name} - {row.score}/100 ({_verdict_label(row)})"):
            if row.triggered_flags:
                st.warning(
                    "Hard caution flags: " + ", ".join(row.triggered_flags)
                )
            for reason in row.reasons:
                st.markdown(f"- {reason}")
            if row.missing_data:
                st.caption("Missing data: " + ", ".join(row.missing_data))
            if row.source_documents:
                st.caption("Source documents: " + "; ".join(row.source_documents))


def _run_rescore_all(
    snapshot: IpoDashboardSnapshot, user_email: str | None
) -> dict[str, int]:
    """Re-score every issue through the shared scoring service.

    Beginner note:
        This is repository work only (no network), so it is safe inside a
        Streamlit action. Failures are counted, never raised: one bad issue
        must not abort the button for the rest.
    """
    counts = {"evaluated": 0, "skipped_unchanged": 0, "insufficient_inputs": 0, "failed": 0}
    for row in snapshot.rows:
        try:
            outcome = rescore_issue(row.issue_id)
        except Exception:  # noqa: BLE001 - one issue must not block the rest
            counts["failed"] += 1
        else:
            counts[outcome.status] += 1
    record_audit_event(
        event=EVENT_IPO_RESCORE_TRIGGERED,
        user_email=user_email,
        metadata=dict(counts),
    )
    return counts


def _render_ipo_page(*, can_rescore: bool, user_email: str | None = None) -> None:
    """Render the read-only IPO screener dashboard.

    Args:
        can_rescore: Whether the signed-in role may trigger a re-score
            (MANAGE_IPO_DATA). The button is hidden otherwise; hiding is UX,
            the capability check in ``app.main`` is the boundary.
        user_email: Signed-in identity for the re-score audit trail.
    """
    st.subheader("IPO screener")
    st.caption(
        "Read-only view of scanned SEBI filings and their deterministic "
        "verdicts. Evidence and scores are produced by the screener job; "
        "no network call runs inside this page."
    )

    if can_rescore and st.button("Re-score all issues", key="ipo_rescore_all"):
        counts = _run_rescore_all(_load_snapshot(), user_email)
        _load_snapshot.clear()
        st.success(
            "Re-score complete: "
            f"{counts['evaluated']} evaluated, "
            f"{counts['skipped_unchanged']} unchanged, "
            f"{counts['insufficient_inputs']} missing evidence, "
            f"{counts['failed']} failed."
        )

    snapshot = _load_snapshot()
    st.caption(f"Snapshot generated at {snapshot.generated_at.isoformat()}.")
    choice = st.radio(
        "Verdict filter",
        _VERDICT_FILTERS,
        horizontal=True,
        key="ipo_verdict_filter",
    )

    for title, selector in _SECTIONS:
        _render_section(title, _apply_verdict_filter(selector(snapshot), choice))

    _render_breakdowns(_apply_verdict_filter(snapshot.rows, choice))
