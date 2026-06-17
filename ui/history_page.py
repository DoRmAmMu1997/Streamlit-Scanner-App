"""Scan history page (SCAN-004), extracted from app.py (REF-001).

Every scan run is already persisted by the SCAN-003 service (UI scans and the
headless daily job alike). This page reads that history back so users can
audit what ran, what it found, and what failed. Pure data-shaping helpers are
separated from Streamlit rendering so tests can cover the logic without a
browser.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.exc import OperationalError

from backend.audit import record_audit_event
from backend.observability import EVENT_EXPORT_DOWNLOADED
from backend.scanning import ScanStatus
from backend.storage import (
    ScanRun,
    count_scan_results_for_runs,
    get_latest_scan_runs,
    get_scan_results,
    list_distinct_screener_keys,
    list_distinct_triggered_by_values,
    list_distinct_universe_keys,
    session_scope,
)
from ui.common import _csv_safe, _decimal_column_config, _emoji_rating, _redact_secrets

# Emoji badges make run states scannable in a dense table, mirroring the
# BUY/SELL badges the results table already uses.
_HISTORY_STATUS_BADGES = {
    "running": "\U0001f535 RUNNING",
    "success": "\U0001f7e2 SUCCESS",
    "partial": "\U0001f7e1 PARTIAL",
    "failed": "\U0001f534 FAILED",
}

# How much of a long error message fits in the runs table. The full message is
# always shown in the run-details view below the table.
_HISTORY_ERROR_PREVIEW_CHARS = 80


def _as_utc(value: datetime) -> datetime:
    """Return an aware UTC datetime for naive or aware inputs.

    Beginner note:
    The database stores UTC, but SQLite hands timestamps back *naive* (without
    timezone info) while Postgres hands them back aware. A bare
    ``value.astimezone()`` on a naive value would wrongly assume local time, so
    naive values are stamped as the UTC they already are.
    """
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _format_utc_timestamp(value: datetime | None) -> str:
    """Format a run timestamp for display, or em-dash when missing."""
    if value is None:
        return "—"
    return _as_utc(value).strftime("%Y-%m-%d %H:%M UTC")


def _format_run_duration(
    started_at: datetime | None, finished_at: datetime | None
) -> str:
    """Return a short human duration, or 'still running' without an end time."""
    if started_at is None or finished_at is None:
        return "still running"
    seconds = max(0.0, (_as_utc(finished_at) - _as_utc(started_at)).total_seconds())
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{seconds / 60:.1f}m"


def _history_filter_kwargs(
    screener_choice: str | None,
    universe_choice: str | None,
    status_choice: str | None,
    date_range: tuple[date, ...] | list[date] | None,
    triggered_by_choice: str | None,
    symbol_text: str | None,
) -> dict[str, Any]:
    """Map raw widget values to ``get_latest_scan_runs`` keyword filters.

    Kept pure (no Streamlit) so tests can prove the mapping directly:
    - "All" or an empty dropdown choice means no filter for that field;
    - ``st.date_input`` hands back a 0-, 1-, or 2-item range while the user is
      mid-selection — 1 item means "from this day onward";
    - a blank/whitespace symbol means no symbol filter.
    """
    kwargs: dict[str, Any] = {}
    if screener_choice and screener_choice != "All":
        kwargs["screener_key"] = screener_choice
    if universe_choice and universe_choice != "All":
        kwargs["universe_key"] = universe_choice
    if status_choice and status_choice != "All":
        kwargs["status"] = ScanStatus(status_choice.lower())
    dates = tuple(date_range or ())
    if len(dates) >= 1 and dates[0] is not None:
        kwargs["started_from"] = dates[0]
    if len(dates) >= 2 and dates[1] is not None:
        kwargs["started_to"] = dates[1]
    if triggered_by_choice and triggered_by_choice != "All":
        kwargs["triggered_by"] = triggered_by_choice
    symbol = (symbol_text or "").strip()
    if symbol:
        kwargs["symbol"] = symbol
    return kwargs


def _history_filter_signature(
    screener_choice: str | None,
    universe_choice: str | None,
    status_choice: str | None,
    date_range: tuple[date, ...] | list[date] | None,
    triggered_by_choice: str | None,
    symbol_text: str | None,
) -> str:
    """Return a compact widget-key suffix that changes with every filter.

    Streamlit keeps table selections by widget key. Hashing all filter values
    gives a newly filtered table fresh selection state, so row 2 from an old
    result set cannot accidentally open row 2 from a different result set.
    """
    values = [
        screener_choice,
        universe_choice,
        status_choice,
        [value.isoformat() for value in tuple(date_range or ())],
        triggered_by_choice,
        (symbol_text or "").strip().upper(),
    ]
    payload = json.dumps(values, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _history_run_row(run: ScanRun, shortlisted: int) -> dict[str, Any]:
    """Convert one ScanRun ORM object into a plain display dict.

    Called INSIDE the database session on purpose: after ``session_scope()``
    exits, touching lazy attributes (especially ``run.results``) would raise
    ``DetachedInstanceError``. A plain dict has no such trap, so everything the
    page renders later is captured here, while the session is still open.
    """
    return {
        "run_id": int(run.id),
        "started": _format_utc_timestamp(run.started_at),
        "finished": _format_utc_timestamp(run.finished_at),
        "duration": _format_run_duration(run.started_at, run.finished_at),
        "screener": run.screener_key,
        "universe": run.universe_key,
        "status": run.status.value,
        "symbols_scanned": run.symbols_scanned,
        "shortlisted": int(shortlisted),
        "triggered_by": run.triggered_by or "—",
        "error_message": run.error_message or "",
    }


def _history_runs_frame(
    rows: list[dict[str, Any]],
    *,
    error_redactor: Callable[[str], str],
) -> pd.DataFrame:
    """Build the display DataFrame for the runs table.

    ``error_redactor`` receives the complete stored message before this helper
    shortens it for the table. That order matters: truncating a long bare secret
    first could leave a prefix that an exact-value redactor no longer recognizes.
    ``symbols_scanned`` may be ``None`` for pre-SCAN-004 runs; those show an
    em-dash instead of a misleading zero.
    """
    def error_preview(message: str) -> str:
        safe_message = error_redactor(message)
        if len(safe_message) > _HISTORY_ERROR_PREVIEW_CHARS:
            return safe_message[:_HISTORY_ERROR_PREVIEW_CHARS] + "…"
        return safe_message

    return pd.DataFrame(
        [
            {
                "Started": row["started"],
                "Finished": row["finished"],
                "Screener": row["screener"],
                "Universe": row["universe"],
                "Status": _HISTORY_STATUS_BADGES.get(row["status"], row["status"]),
                "Symbols scanned": (
                    "—"
                    if row["symbols_scanned"] is None
                    else str(int(row["symbols_scanned"]))
                ),
                "Shortlisted": int(row["shortlisted"]),
                "Triggered by": row["triggered_by"],
                "Error": error_preview(row["error_message"]),
            }
            for row in rows
        ]
    )


def _render_history_page() -> None:
    """Render the scan-history view: filters, runs table, and run details.

    Reads only — this page never writes to the database. SCAN-002 enabled
    SQLite WAL mode precisely so this read view stays usable while a scan is
    writing in another process (e.g. the headless daily job).
    """
    st.subheader("Scan history")
    st.caption(
        "Every scan run is recorded — from this UI and from the headless daily "
        "job. Click a run to inspect its shortlisted results."
    )

    # Populate filters from persisted history, not the live registry: deleted
    # screeners and old universes remain inspectable, and a broken screener
    # module cannot take down this read-only audit view.
    try:
        with session_scope() as session:
            screener_keys = list_distinct_screener_keys(session)
            universe_keys = list_distinct_universe_keys(session)
            triggered_by_values = list_distinct_triggered_by_values(session)
    except OperationalError:
        # The most common cause is a database that has never been migrated
        # (fresh checkout, or an old scanner.db missing the new column).
        st.error(
            "Scan history tables are missing or outdated. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        screener_choice = st.selectbox(
            "Screener",
            ["All", *screener_keys],
            key="history_screener_filter",
            help="Only screeners that appear in recorded history are listed.",
        )
    with filter_col2:
        universe_choice = st.selectbox(
            "Universe",
            ["All", *universe_keys],
            key="history_universe_filter",
            help="Only universes that appear in recorded history are listed.",
        )
    with filter_col3:
        status_choice = st.selectbox(
            "Status",
            ["All", *[status.value for status in ScanStatus]],
            format_func=lambda value: value if value == "All" else value.upper(),
            key="history_status_filter",
        )

    filter_col4, filter_col5, filter_col6 = st.columns(3)
    with filter_col4:
        # value=[] starts the range empty, so the default view is simply "the
        # latest runs" with no date restriction.
        date_range = st.date_input(
            "Started between",
            value=[],
            key="history_date_filter",
            help="Pick one day or a range. Leave empty to show the latest runs.",
        )
    with filter_col5:
        triggered_by_choice = st.selectbox(
            "Triggered by",
            ["All", *triggered_by_values],
            key="history_triggered_by_filter",
            help="Filter UI and scheduled runs by their recorded audit identity.",
        )
    with filter_col6:
        symbol_text = st.text_input(
            "Symbol",
            key="history_symbol_filter",
            help=(
                "Show only runs that shortlisted this symbol (exact match, "
                "case-insensitive)."
            ),
        )

    filters = _history_filter_kwargs(
        screener_choice,
        universe_choice,
        status_choice,
        date_range,
        triggered_by_choice,
        symbol_text,
    )
    with session_scope() as session:
        runs = get_latest_scan_runs(session, limit=50, **filters)
        counts = count_scan_results_for_runs(session, [run.id for run in runs])
        # Convert to plain dicts while the session is open — see _history_run_row.
        rows = [_history_run_row(run, counts[run.id]) for run in runs]

    if not rows:
        if filters:
            st.info("No scan runs match the current filters.")
        else:
            st.info(
                "No scan history yet. Run a screener from the Scanner view (or "
                "the daily scan job) and its run will appear here."
            )
        return

    frame = _history_runs_frame(rows, error_redactor=_redact_secrets)

    # Key the table by the filter signature: changing any filter mints a fresh
    # widget, which discards a stale row selection that would otherwise point at
    # the wrong run in the re-filtered list.
    signature = _history_filter_signature(
        screener_choice,
        universe_choice,
        status_choice,
        date_range,
        triggered_by_choice,
        symbol_text,
    )
    table_state = st.dataframe(
        frame,
        width="stretch",
        hide_index=True,
        selection_mode="single-row",
        on_select="rerun",
        key=f"history_runs_table_{signature}",
    )

    selected_rows = getattr(getattr(table_state, "selection", None), "rows", []) or []
    if not selected_rows:
        st.info("Click a run above to inspect its shortlisted results.", icon="👆")
        return
    selected_index = int(selected_rows[0])
    if not (0 <= selected_index < len(rows)):
        # Belt-and-braces with the signature key above: never render the wrong
        # run from a stale selection index.
        return

    _render_history_run_details(
        rows[selected_index], symbol_filter=symbol_text.strip().upper()
    )


def _render_history_run_details(row: dict[str, Any], *, symbol_filter: str = "") -> None:
    """Render one run's summary metrics, error state, and persisted results."""
    st.subheader(f"Run #{row['run_id']} — {row['screener']}")

    with st.container(border=True):
        col1, col2, col3 = st.columns(3)
        col1.metric("Status", _HISTORY_STATUS_BADGES.get(row["status"], row["status"]))
        col2.metric("Started", row["started"])
        col3.metric("Finished", row["finished"])
        col4, col5, col6 = st.columns(3)
        col4.metric("Duration", row["duration"])
        col5.metric(
            "Symbols scanned",
            "—" if row["symbols_scanned"] is None else int(row["symbols_scanned"]),
        )
        col6.metric("Shortlisted", row["shortlisted"])
        st.caption(
            f"Universe: `{row['universe']}` · Triggered by: `{row['triggered_by']}`"
        )

    # AC: failed runs must be visible AND understandable. The table preview
    # truncates; here the full (redacted) message is shown prominently.
    if row["error_message"] and row["status"] in ("failed", "partial"):
        st.error(_redact_secrets(row["error_message"]))

    with session_scope() as session:
        results = get_scan_results(session, row["run_id"])
        # Same detached-object rule as the runs table: copy scalars to plain
        # dicts inside the session. close_price is a Decimal; convert to float
        # so _decimal_column_config's float-dtype formatting applies.
        result_rows = [
            {
                "symbol": result.symbol,
                "signal_date": (
                    result.signal_date.isoformat() if result.signal_date else "—"
                ),
                "close": (
                    float(result.close_price)
                    if result.close_price is not None
                    else None
                ),
                "rating": result.rating or "",
                "reason": result.reason or "",
            }
            for result in results
        ]

    if symbol_filter:
        # Mirror the repository's exact, case-insensitive match so the run list
        # and this detail table always agree on what "filtered by symbol" means.
        result_rows = [
            r for r in result_rows if str(r["symbol"]).strip().upper() == symbol_filter
        ]

    if not result_rows:
        if row["status"] == "failed":
            st.info("This run failed before producing any results.")
        elif symbol_filter:
            st.info("This run has no shortlisted rows for the symbol filter.")
        else:
            st.info("This run produced no shortlisted results.")
        return

    results_df = pd.DataFrame(result_rows)
    st.dataframe(
        _emoji_rating(results_df),
        width="stretch",
        hide_index=True,
        column_config=_decimal_column_config(results_df),
        key=f"history_results_{row['run_id']}",
    )
    history_file_name = f"scan_run_{row['run_id']}_results.csv"
    # download_button returns True on the click rerun, doubling as the OBS-003
    # export trigger. The signed-in email is stashed in session_state by main().
    if st.download_button(
        "Download run results CSV",
        data=_csv_safe(results_df).to_csv(index=False).encode("utf-8"),
        file_name=history_file_name,
        mime="text/csv",
        key=f"history_csv_{row['run_id']}",
    ):
        record_audit_event(
            event=EVENT_EXPORT_DOWNLOADED,
            user_email=st.session_state.get("_audit_user_email"),
            metadata={
                "file_name": history_file_name,
                "row_count": len(results_df),
                "kind": "history_results",
                "run_id": int(row["run_id"]),
            },
        )
