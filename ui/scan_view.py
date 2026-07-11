"""Scan-results view — shortlist table, chart sync, CSV export (REF-002).

Extracted verbatim from app.py, continuing REF-001's direction. The scan
output pipeline is: summary + run diagnostics, the row-selectable results
table (RANK-002 ordering), the two-widget table/dropdown chart sync, the
Check Fundamentals panel for the charted symbol, and the capability-gated
CSV export (AUTH-003).

Beginner note: like every ui/ module, this file reads Streamlit through its
own module global, so tests monkeypatch ``ui.scan_view.st``. app.py
re-exports these helpers and calls them through its module globals, so tests
that monkeypatch ``app._render_scan_output`` keep working (REF-001
convention).
"""

from __future__ import annotations

from typing import Any

import pandas as pd
import streamlit as st

from backend.audit import record_audit_event
from backend.observability import EVENT_EXPORT_DOWNLOADED
from backend.screener_registry import ScreenerDefinition
from ui.chart_cache import _render_cached_symbol_chart
from ui.common import (
    _csv_safe,
    _decimal_column_config,
    _drop_provenance,
    _emoji_rating,
    _redact_secrets,
    _score_components_frame,
    _sort_results_by_final_score,
)
from ui.fundamentals_panel import _render_fundamentals_panel


def _has_rating_column(results: pd.DataFrame) -> bool:
    """Return True when the results table carries a BUY/SELL-style column."""
    return any(column in results.columns for column in ("rating", "signal"))



def _render_scan_output(
    selected: ScreenerDefinition, cache: dict[str, Any], *, can_export: bool
) -> None:
    """Render the cached scan: stats + ranked selectable table + chart.

    The cache payload survives ordinary Streamlit reruns, so this function must
    be deterministic: sort by ``final_score`` the same way every time, preserve
    the selected-row contract for charts, and keep exports aligned with what the
    user sees on screen.

    ``can_export`` is the AUTH-003 ``EXPORT_RESULTS`` capability. A CSV download has
    no separate server handler — ``st.download_button`` builds its payload at render
    time — so a viewer (no export capability) must never reach the button or the
    bytes build; the whole export block is skipped for them.
    """
    results: pd.DataFrame = _sort_results_by_final_score(cache["results"])
    stats = cache["stats"]
    failures: list[dict[str, Any]] = cache["failures"]
    compute_failures: list[dict[str, Any]] = cache.get("compute_failures", [])

    # A short summary line, with the per-run diagnostics tucked into a
    # collapsed expander so they are available but never clutter the results.
    st.markdown(f"### {len(results)} stock(s) shortlisted")
    with st.expander("Run details", expanded=False):
        detail_col1, detail_col2 = st.columns(2)
        detail_col1.metric("Cache hits", stats["cache_hits"])
        detail_col1.metric("API cache misses", stats["cache_misses"])
        detail_col2.metric("API attempts (incl. retries)", stats["api_attempts"])
        detail_col2.metric("Rate-limit retries", stats["rate_limit_retries"])
        st.caption(f"Fetch failures: {len(failures)}")
        st.caption(f"Compute failures: {len(compute_failures)}")

    if results.empty:
        st.warning("The screener returned no rows.")
    else:
        chart_symbol = _render_results_with_chart(selected, results, cache)
        # Show the Check Fundamentals panel after the chart. The helper chooses
        # criteria mode for curated symbols and universal mode for everything
        # else, so every shortlisted stock can still get a fundamentals view.
        _render_fundamentals_panel(chart_symbol)
        # AUTH-003: only analysts and admins may export. Build the CSV bytes and
        # render the download button only when the role allows it (the button has
        # no post-click handler to re-check, so this conditional IS the boundary).
        if can_export:
            # CSV-safe wrapper neutralizes formula injection before download. The
            # raw DataFrame still has full precision; only the on-screen Styler
            # rounds to 2 decimals, so the CSV mirrors the source data.
            results_file_name = f"{selected.key}_results.csv"
            # st.download_button returns True on the rerun where the user clicks it,
            # so it doubles as the OBS-003 export trigger (edge-triggered, no dedup).
            if st.download_button(
                "Download results CSV",
                data=_csv_safe(_drop_provenance(results))
                .to_csv(index=False)
                .encode("utf-8"),
                file_name=results_file_name,
                mime="text/csv",
            ):
                record_audit_event(
                    event=EVENT_EXPORT_DOWNLOADED,
                    user_email=st.session_state.get("_audit_user_email"),
                    metadata={
                        "file_name": results_file_name,
                        "row_count": len(results),
                        "kind": "scan_results",
                    },
                )

    if failures:
        with st.expander("Fetch failures", expanded=True):
            failures_df = pd.DataFrame(failures)
            if "message" in failures_df.columns:
                failures_df["message"] = failures_df["message"].map(_redact_secrets)
            st.dataframe(failures_df, width="stretch", hide_index=True)

    if compute_failures:
        with st.expander("Compute failures", expanded=True):
            compute_df = pd.DataFrame(compute_failures)
            if "message" in compute_df.columns:
                compute_df["message"] = compute_df["message"].map(_redact_secrets)
            st.dataframe(compute_df, width="stretch", hide_index=True)



def _render_results_with_chart(
    selected: ScreenerDefinition,
    results: pd.DataFrame,
    cache: dict[str, Any],
) -> str | None:
    """Render the combined results table (row-selectable) and the chart.

    Returns the symbol currently shown on the chart, or None when no chart
    can be rendered (no symbol column, no `build_chart`, etc.).
    """
    table_key = f"results_table_{selected.key}"
    # RANK-002 sorting happens here too, even though run_scan already returns a
    # ranked frame. Keeping the UI helper as a second guard makes old cached test
    # payloads and future history imports display consistently.
    ranked_results = _sort_results_by_final_score(results)

    # The reserved PROV-002 provenance column is machine-readable evidence for
    # persistence, not a table column; drop it for display. Row order/indices are
    # unchanged, so the row-selection below still maps back to `results`.
    display = _drop_provenance(ranked_results)

    # ONE plain DataFrame does both jobs: emoji BUY/SELL badges for the eye,
    # and `selection_mode` row-selection to drive the chart. We deliberately
    # do NOT pass a pandas Styler here — Streamlit only reliably supports row
    # selection on plain DataFrames. 2-decimal price display is handled by
    # `column_config`, which (unlike a Styler) composes with selection.
    table_state = st.dataframe(
        _emoji_rating(display),
        width="stretch",
        hide_index=True,
        column_config=_decimal_column_config(display),
        selection_mode="single-row",
        on_select="rerun",
        key=table_key,
    )
    if _has_rating_column(ranked_results):
        st.caption("🟢 BUY / 🔴 SELL · click a row to chart that symbol.")

    # Keep component details one click away instead of adding four more columns
    # to the main shortlist. The raw `reason` column stays in the table, so the
    # score explains usefulness without hiding the screener's original rationale.
    components_frame = _score_components_frame(ranked_results)
    if not components_frame.empty:
        with st.expander("Score components", expanded=False):
            st.dataframe(
                components_frame,
                width="stretch",
                hide_index=True,
                column_config=_decimal_column_config(components_frame),
                key=f"score_components_{selected.key}",
            )

    if "symbol" not in ranked_results.columns or selected.build_chart is None:
        return None

    symbols = [str(symbol).upper() for symbol in ranked_results["symbol"].tolist()]
    if not symbols:
        return None

    st.divider()
    st.subheader("Chart")

    # --- Two-widget sync: the results table AND the dropdown both pick the
    # charted symbol. The control the user *just* used wins.
    #
    # Streamlit gotcha: a keyed widget ignores its `index=`/default on reruns;
    # its value lives in `st.session_state[key]`. So the ONLY way to make a
    # table click move the dropdown is to write the picked symbol into the
    # selectbox's session_state key BEFORE the selectbox is instantiated.
    selected_rows = getattr(getattr(table_state, "selection", None), "rows", []) or []
    current_row = int(selected_rows[0]) if selected_rows else None

    selectbox_key = f"chart_symbol_{selected.key}"
    prev_row_key = f"chart_prev_table_row_{selected.key}"
    prev_row_symbol_key = f"chart_prev_table_symbol_{selected.key}"

    # A row number alone is not a stable identity: a fresh scan can reorder the
    # shortlist while Streamlit keeps (for example) row 2 selected. Capture the
    # symbol occupying that row as well, so a reordered table moves the dropdown
    # and chart to the company the user can currently see highlighted.
    current_row_symbol = (
        symbols[current_row]
        if current_row is not None and 0 <= current_row < len(symbols)
        else None
    )

    # A table click counts only when its row OR its symbol changed since the last
    # rerun. Otherwise a stale-but-persistent selection would override every
    # fresh dropdown change.
    table_changed = current_row_symbol is not None and (
        current_row != st.session_state.get(prev_row_key)
        or current_row_symbol != st.session_state.get(prev_row_symbol_key)
    )
    st.session_state[prev_row_key] = current_row
    st.session_state[prev_row_symbol_key] = current_row_symbol

    # Keep the selectbox's stored value valid (a screener re-run can change the
    # `symbols` list out from under a previously stored pick).
    if selectbox_key not in st.session_state or st.session_state[selectbox_key] not in symbols:
        st.session_state[selectbox_key] = symbols[0]
    # A fresh table click wins — push it into the selectbox state pre-widget.
    # ``table_changed`` implies the symbol is present, but the explicit check
    # lets the type checker narrow ``str | None`` to ``str`` here.
    if table_changed and current_row_symbol is not None:
        st.session_state[selectbox_key] = current_row_symbol

    chart_symbol = st.selectbox(
        "Chart symbol",
        symbols,
        key=selectbox_key,
        help="Click a table row OR use this dropdown — whichever you use last wins.",
    )

    return _render_cached_symbol_chart(
        selected=selected,
        chart_symbol=chart_symbol,
        universe_df=cache["universe_df"],
        data_loader=cache["data_loader"],
        params_for_chart=cache["params_for_chart"],
    )
