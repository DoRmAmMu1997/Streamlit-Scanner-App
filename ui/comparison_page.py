"""JOB-003 scan comparison page.

This read-only page compares the latest finalized shortlist against the
immediately previous finalized shortlist for one screener/universe pair.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

import pandas as pd
import streamlit as st
from sqlalchemy.exc import OperationalError

from backend.audit import record_audit_event
from backend.observability import EVENT_EXPORT_DOWNLOADED
from backend.scanning.comparison import ComparisonRow, ScanComparison, build_scan_comparison
from backend.storage import list_finalized_scan_groups, session_scope
from ui.common import _csv_safe, _decimal_column_config

_SAFE_FILE_TOKEN_RE = re.compile(r"[^A-Za-z0-9_-]+")


def _comparison_screener_options(groups: Sequence[tuple[str, str]]) -> list[str]:
    """Return sorted screener keys that have finalized comparison history."""
    return sorted({screener for screener, _universe in groups})


def _comparison_universe_options(
    groups: Sequence[tuple[str, str]],
    screener_key: str,
) -> list[str]:
    """Return sorted universe keys available for the selected screener."""
    return sorted(
        universe for screener, universe in groups if screener == screener_key
    )


def _comparison_export_csv(frame: pd.DataFrame) -> bytes:
    """Return formula-safe UTF-8 CSV bytes for the displayed comparison."""
    return _csv_safe(frame).to_csv(index=False).encode("utf-8")


def _safe_file_token(value: str) -> str:
    """Return a conservative token safe to place in a browser download name."""
    token = _SAFE_FILE_TOKEN_RE.sub("_", value.strip()).strip("_-")
    return token or "unknown"


def _render_comparison_page() -> None:
    """Render latest-vs-previous comparison for finalized scan history."""
    st.subheader("Scan comparison")
    st.caption(
        "Compare the latest finalized shortlist with the immediately previous "
        "finalized run for the same screener and universe."
    )

    try:
        with session_scope() as session:
            groups = list_finalized_scan_groups(session)
    except OperationalError:
        st.error(
            "Scan comparison tables are missing or outdated. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return

    if not groups:
        st.info(
            "No finalized scan runs yet. Once a scanner or daily job records a "
            "success or partial run, comparisons will appear here."
        )
        return

    screener_options = _comparison_screener_options(groups)
    filter_col1, filter_col2 = st.columns(2)
    with filter_col1:
        screener_choice = st.selectbox(
            "Screener",
            screener_options,
            key="comparison_screener_filter",
            help="Only screener/universe pairs with finalized history are listed.",
        )
    universe_options = _comparison_universe_options(groups, screener_choice)
    with filter_col2:
        universe_choice = st.selectbox(
            "Universe",
            universe_options,
            key="comparison_universe_filter",
            help="The comparison uses the latest two finalized runs for this pair.",
        )

    try:
        with session_scope() as session:
            comparison = build_scan_comparison(
                session,
                screener_key=screener_choice,
                universe_key=universe_choice,
            )
    except OperationalError:
        st.error(
            "Scan comparison tables are missing or outdated. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return
    except ValueError:
        # The pair came from list_finalized_scan_groups, so it had history when the
        # page loaded; its runs vanishing before this read is a rare TOCTOU edge.
        st.info(
            "No finalized runs are available for this screener/universe pair "
            "anymore. Reload the page to refresh the list."
        )
        return

    _render_run_summary(comparison)
    if comparison.previous_run is None:
        st.info(
            "Need at least two finalized runs for this screener/universe pair "
            "before a latest-vs-previous comparison can be shown."
        )
        return

    sections = [
        ("New today", comparison.new_today, "comparison_new_today"),
        (
            "Repeated from yesterday",
            comparison.repeated_from_yesterday,
            "comparison_repeated_from_yesterday",
        ),
        ("Dropped today", comparison.dropped_today, "comparison_dropped_today"),
        ("Improved score", comparison.improved_score, "comparison_improved_score"),
        ("Degraded score", comparison.degraded_score, "comparison_degraded_score"),
    ]
    for title, rows, key in sections:
        _render_section(title, rows, key=key)

    export_frame = comparison.to_export_frame()
    if export_frame.empty:
        return

    file_name = (
        f"scan_comparison_{_safe_file_token(screener_choice)}_"
        f"{_safe_file_token(universe_choice)}.csv"
    )
    if st.download_button(
        label="Download comparison CSV",
        data=_comparison_export_csv(export_frame),
        file_name=file_name,
        mime="text/csv",
        key="comparison_csv",
    ):
        session_state = getattr(st, "session_state", {})
        record_audit_event(
            event=EVENT_EXPORT_DOWNLOADED,
            user_email=session_state.get("_audit_user_email"),
            metadata={
                "file_name": file_name,
                "row_count": len(export_frame),
                "kind": "scan_comparison",
                "screener_key": screener_choice,
                "universe_key": universe_choice,
                "latest_run_id": comparison.latest_run.run_id,
                "previous_run_id": comparison.previous_run.run_id,
            },
        )


def _render_run_summary(comparison: ScanComparison) -> None:
    latest = comparison.latest_run
    previous = comparison.previous_run
    metric_cols = st.columns(5)
    metric_cols[0].metric("Latest run", latest.run_id)
    metric_cols[1].metric("Previous run", previous.run_id if previous else "-")
    metric_cols[2].metric("New", len(comparison.new_today))
    metric_cols[3].metric("Repeated", len(comparison.repeated_from_yesterday))
    metric_cols[4].metric("Dropped", len(comparison.dropped_today))
    st.caption(
        f"Latest started: {latest.started} | Previous started: "
        f"{previous.started if previous else '-'}"
    )


def _render_section(
    title: str,
    rows: Sequence[ComparisonRow],
    *,
    key: str,
) -> None:
    st.subheader(title)
    frame = _section_frame(rows)
    if frame.empty:
        st.caption("No rows in this comparison section.")
        return
    st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        column_config=_decimal_column_config(frame),
        key=key,
    )


def _section_frame(rows: Sequence[ComparisonRow]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "Symbol": row.symbol,
                "Latest rating": row.latest_rating,
                "Previous rating": row.previous_rating,
                "Latest score": _decimal_value(row.latest_score),
                "Previous score": _decimal_value(row.previous_score),
                "Score source": row.score_source or "",
                "Score delta": _decimal_value(row.score_delta),
                "Latest reason": row.latest_reason,
                "Previous reason": row.previous_reason,
            }
            for row in rows
        ]
    )


def _decimal_value(value: Decimal | None) -> float | None:
    return float(value) if value is not None else None
