"""Validation / Signal Performance page (VALID-003B).

VALID-003A added the backend read model: ``summarize_validation_metrics`` rolls
stored ``signal_forward_returns`` rows up into screener/universe/horizon
performance metrics. This page renders that summary so a user can answer "which
screener has actually earned trust after 20 / 60 / 120 trading days?" rather than
"which stock looks good today?".

It is deliberately **read-only**: it never computes forward returns
(``compute_pending_forward_returns`` belongs to a CLI/scheduled job) and never
writes to the database. As with the scan-history page, pure data-shaping helpers
are split from Streamlit rendering so tests can cover them without a browser.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pandas as pd
import streamlit as st
from sqlalchemy.exc import OperationalError

from backend.audit import record_audit_event
from backend.observability import EVENT_EXPORT_DOWNLOADED
from backend.storage import (
    list_distinct_screener_keys,
    list_distinct_universe_keys,
    session_scope,
)
from backend.validation import (
    FORWARD_RETURN_HORIZONS,
    load_universe_sector_lookup,
    summarize_validation_dashboard,
)
from backend.validation.metrics import (
    BestWorstSignal,
    ValidationDashboardSummary,
    ValidationSummary,
)
from ui.common import _csv_safe

# Column order for the summary table. Kept as a module constant so a test can
# assert the contract without re-listing every header inline.
_SUMMARY_COLUMNS = (
    "Screener",
    "Universe",
    "Horizon",
    "First signal",
    "Last signal",
    "Total signals",
    "Computed",
    "Pending",
    "Insufficient",
    "Hit rate %",
    "Avg return %",
    "Median return %",
    "Avg excess %",
    "Median excess %",
    "Avg MAE %",
    "Avg MFE %",
    "Best signal",
    "Worst signal",
)


def _format_pct(value: Decimal | None) -> str:
    """Render a stored 4-dp ``Decimal`` percentage as a 2-dp display string.

    The aggregate metrics keep full ``Decimal`` precision; this is display-only
    rounding. ``None`` (no computed rows, or no benchmark/excess) shows an
    em-dash so a missing measurement never reads as ``0.00%``.
    """
    if value is None:
        return "—"
    return f"{value:.2f}%"


def _format_signal(signal: BestWorstSignal | None) -> str:
    """Render a best/worst signal as ``SYMBOL +x.xx% (signal_date)``.

    ``None`` means the group has no computed return to rank, so it shows an
    em-dash rather than a fabricated row.
    """
    if signal is None:
        return "—"
    signal_date = signal.signal_date.isoformat() if signal.signal_date else "—"
    return f"{signal.symbol} {_format_pct(signal.forward_return_pct)} ({signal_date})"


def _validation_filter_kwargs(
    screener_choice: str | None,
    universe_choice: str | None,
    horizon_choice: str | None,
    date_range: tuple[object, ...] | list[object] | None,
) -> dict[str, Any]:
    """Map raw widget values to ``summarize_validation_metrics`` keyword filters.

    Pure (no Streamlit) so tests can prove the plumbing directly:
    - "All"/blank/``None`` means no filter for that field;
    - the horizon dropdown stores the horizon as a string (e.g. ``"60"``) or "All";
    - ``st.date_input`` hands back a 0-, 1-, or 2-item range while the user is
      mid-selection — 1 item means "from this signal date onward".
    """
    kwargs: dict[str, Any] = {}
    if screener_choice and screener_choice != "All":
        kwargs["screener_key"] = screener_choice
    if universe_choice and universe_choice != "All":
        kwargs["universe_key"] = universe_choice
    if horizon_choice and horizon_choice != "All":
        kwargs["horizon_days"] = int(horizon_choice)
    dates = tuple(date_range or ())
    if len(dates) >= 1 and dates[0] is not None:
        kwargs["signal_date_from"] = dates[0]
    if len(dates) >= 2 and dates[1] is not None:
        kwargs["signal_date_to"] = dates[1]
    return kwargs


def _validation_summary_frame(summary: ValidationSummary) -> pd.DataFrame:
    """Build the display DataFrame for the summary table.

    One row per ``(screener, universe, horizon)`` group. ``Decimal`` metrics are
    formatted to display strings (with em-dash for missing values) so the table
    reads cleanly and never shows a null benchmark column as ``0.00%``.

    Trade-off (v1, table-first): formatting to strings means the percentage
    columns sort lexically, not numerically. That is acceptable for the first
    dashboard; if numeric sorting is wanted later, switch the percentage columns
    to floats and format them with ``st.column_config.NumberColumn(format=…)``.
    """
    return pd.DataFrame(
        [
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "First signal": (
                    row.first_signal_date.isoformat() if row.first_signal_date else "—"
                ),
                "Last signal": (
                    row.last_signal_date.isoformat() if row.last_signal_date else "—"
                ),
                "Total signals": row.total_signals,
                "Computed": row.computed_count,
                "Pending": row.pending_count,
                "Insufficient": row.insufficient_data_count,
                "Hit rate %": _format_pct(row.hit_rate_pct),
                "Avg return %": _format_pct(row.average_forward_return_pct),
                "Median return %": _format_pct(row.median_forward_return_pct),
                "Avg excess %": _format_pct(row.average_excess_return_pct),
                "Median excess %": _format_pct(row.median_excess_return_pct),
                "Avg MAE %": _format_pct(row.average_mae_pct),
                "Avg MFE %": _format_pct(row.average_mfe_pct),
                "Best signal": _format_signal(row.best_signal),
                "Worst signal": _format_signal(row.worst_signal),
            }
            for row in summary.rows
        ],
        columns=list(_SUMMARY_COLUMNS),
    )


def _validation_distribution_frame(dashboard: ValidationDashboardSummary) -> pd.DataFrame:
    """Return the computed-return histogram rows for display."""
    return pd.DataFrame(
        [
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Return bucket": row.bucket_label,
                "Computed signals": row.computed_count,
            }
            for row in dashboard.return_distribution
        ],
        columns=["Screener", "Universe", "Horizon", "Return bucket", "Computed signals"],
    )


def _validation_horizon_frame(dashboard: ValidationDashboardSummary) -> pd.DataFrame:
    """Return win-rate rows grouped by screener/universe/horizon.

    The win-rate and benchmark-relative sections are two projections of the same
    per-horizon ``benchmark_relative_rows`` tuple: this one shows the hit rate.
    """
    return pd.DataFrame(
        [
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Computed": row.computed_count,
                "Hit rate %": _format_pct(row.hit_rate_pct),
            }
            for row in dashboard.benchmark_relative_rows
        ],
        columns=["Screener", "Universe", "Horizon", "Computed", "Hit rate %"],
    )


def _validation_benchmark_frame(dashboard: ValidationDashboardSummary) -> pd.DataFrame:
    """Return benchmark-relative rows without fabricating missing excess values."""
    return pd.DataFrame(
        [
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Computed": row.computed_count,
                "Avg excess %": _format_pct(row.average_excess_return_pct),
                "Median excess %": _format_pct(row.median_excess_return_pct),
            }
            for row in dashboard.benchmark_relative_rows
        ],
        columns=[
            "Screener",
            "Universe",
            "Horizon",
            "Computed",
            "Avg excess %",
            "Median excess %",
        ],
    )


def _validation_time_series_frame(dashboard: ValidationDashboardSummary) -> pd.DataFrame:
    """Return monthly signal-count rows for the dashboard timeline section."""
    return pd.DataFrame(
        [
            {
                "Month": point.period_start.isoformat(),
                "Screener": point.screener_key,
                "Universe": point.universe_key,
                "Horizon": f"{point.horizon_days}D",
                "Total": point.total_signals,
                "Computed": point.computed_count,
                "Pending": point.pending_count,
                "Insufficient": point.insufficient_data_count,
            }
            for point in dashboard.signal_count_over_time
        ],
        columns=[
            "Month",
            "Screener",
            "Universe",
            "Horizon",
            "Total",
            "Computed",
            "Pending",
            "Insufficient",
        ],
    )


def _validation_sector_frame(dashboard: ValidationDashboardSummary) -> pd.DataFrame:
    """Return sector concentration rows, with Unknown when metadata is absent."""
    return pd.DataFrame(
        [
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Sector": row.sector,
                "Total signals": row.total_signals,
                "Computed": row.computed_count,
                "Share %": _format_pct(row.share_of_group_pct),
                "Hit rate %": _format_pct(row.hit_rate_pct),
                "Avg return %": _format_pct(row.average_forward_return_pct),
            }
            for row in dashboard.sector_concentration
        ],
        columns=[
            "Screener",
            "Universe",
            "Horizon",
            "Sector",
            "Total signals",
            "Computed",
            "Share %",
            "Hit rate %",
            "Avg return %",
        ],
    )


def _validation_best_worst_frame(summary: ValidationSummary) -> pd.DataFrame:
    """Return best/worst signal details as a scan-friendly table."""
    rows: list[dict[str, Any]] = []
    for row in summary.rows:
        rows.append(
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Rank": "Best",
                "Signal": _format_signal(row.best_signal),
            }
        )
        rows.append(
            {
                "Screener": row.screener_key,
                "Universe": row.universe_key,
                "Horizon": f"{row.horizon_days}D",
                "Rank": "Worst",
                "Signal": _format_signal(row.worst_signal),
            }
        )
    return pd.DataFrame(
        rows,
        columns=["Screener", "Universe", "Horizon", "Rank", "Signal"],
    )


def _validation_summary_csv(summary_frame: pd.DataFrame) -> bytes:
    """Return CSV bytes using the shared spreadsheet-formula safety wrapper."""
    return _csv_safe(summary_frame).to_csv(index=False).encode("utf-8")


@st.cache_data(ttl=600, show_spinner=False)
def _cached_sector_lookup(universe_keys: tuple[str, ...]) -> dict[tuple[str, str], str]:
    """Cache the local sector lookup so universe CSVs aren't re-read every rerun.

    Streamlit reruns the page for every widget interaction; without this cache
    each rerun would re-read and iterate every relevant universe CSV. A 10-minute
    TTL matches the other universe-derived caches in ``app.py``.
    """
    return load_universe_sector_lookup(universe_keys)


def _render_validation_page() -> None:
    """Render the read-only validation / signal-performance dashboard.

    Reads only: it calls ``summarize_validation_metrics`` over rows the VALID-002
    job already computed and never triggers a compute pass from the UI.
    """
    st.subheader("Validation / Signal Performance")
    st.caption(
        "Which screeners have actually produced good forward returns after "
        "20 / 60 / 120 trading days. Read-only over already-computed rows — "
        "the forward-return compute pass runs as a separate job."
    )

    # Filter options come from recorded scan history, not the live registry, so a
    # renamed/removed screener still has inspectable performance and a broken
    # screener module cannot take down this read-only view.
    try:
        with session_scope() as session:
            screener_keys = list_distinct_screener_keys(session)
            universe_keys = list_distinct_universe_keys(session)
    except OperationalError:
        st.error(
            "Validation tables are missing or outdated. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return

    filter_col1, filter_col2, filter_col3 = st.columns(3)
    with filter_col1:
        screener_choice = st.selectbox(
            "Screener",
            ["All", *screener_keys],
            key="validation_screener_filter",
            help="Only screeners that appear in recorded history are listed.",
        )
    with filter_col2:
        universe_choice = st.selectbox(
            "Universe",
            ["All", *universe_keys],
            key="validation_universe_filter",
            help="Only universes that appear in recorded history are listed.",
        )
    with filter_col3:
        horizon_choice = st.selectbox(
            "Horizon",
            ["All", *[str(horizon) for horizon in FORWARD_RETURN_HORIZONS]],
            format_func=lambda value: value if value == "All" else f"{value}D",
            key="validation_horizon_filter",
            help="Forward window in trading days.",
        )

    # value=[] starts the range empty, so the default view is "all signal dates".
    date_range = st.date_input(
        "Signal date between",
        value=[],
        key="validation_date_filter",
        help="Pick one day or a range over signal dates. Leave empty for all.",
    )

    filters = _validation_filter_kwargs(
        screener_choice, universe_choice, horizon_choice, date_range
    )
    sector_universes = (
        [filters["universe_key"]]
        if "universe_key" in filters
        else list(universe_keys)
    )
    sector_lookup = _cached_sector_lookup(tuple(sector_universes))
    try:
        with session_scope() as session:
            # This is the dashboard's only validation-data read. Keeping the
            # call here (instead of hand-written UI queries) preserves the
            # VALID-003A/004 contract: repository/service code owns SQL and
            # grouping, while the page owns widgets and display states. The
            # returned frozen dataclasses have no lazy ORM attributes, so they
            # are safe after the session closes.
            dashboard = summarize_validation_dashboard(
                session,
                **filters,
                sector_lookup=sector_lookup,
            )
    except OperationalError:
        # A partially migrated database can still list old scan history but fail
        # once the VALID tables are joined. Show the same operator hint as the
        # filter bootstrap path rather than leaking a raw SQLAlchemy traceback.
        st.error(
            "Validation tables are missing or outdated. "
            "Run `python -m alembic upgrade head` and reload this page."
        )
        return

    summary = dashboard.metric_summary
    if not summary.rows:
        if filters:
            st.info("No validation rows match the current filters.")
        else:
            st.info(
                "No forward-return rows yet. Once the validation job computes "
                "forward returns for stored signals, screener performance will "
                "appear here."
            )
        return

    summary_frame = _validation_summary_frame(summary)
    st.dataframe(
        summary_frame,
        width="stretch",
        hide_index=True,
        key="validation_summary_table",
    )
    validation_file_name = "validation_signal_performance.csv"
    # The download button is also the OBS-003 export audit trigger. The page is
    # otherwise read-only; this records only that a user downloaded the displayed
    # summary, not a mutation of validation data.
    if st.download_button(
        label="Download validation summary CSV",
        data=_validation_summary_csv(summary_frame),
        file_name=validation_file_name,
        mime="text/csv",
        key="validation_summary_csv",
    ):
        session_state = getattr(st, "session_state", {})
        record_audit_event(
            event=EVENT_EXPORT_DOWNLOADED,
            user_email=session_state.get("_audit_user_email"),
            metadata={
                "file_name": validation_file_name,
                "row_count": len(summary_frame),
                "kind": "validation_summary",
            },
        )

    _render_dashboard_sections(dashboard)

    # Empty-state notes that still make sense once the table itself is shown.
    # These are mutually exclusive: with zero computed rows every excess is also
    # null, so the "no computed rows" note already covers it; the benchmark note
    # only adds value once returns exist but no benchmark was configured.
    if summary.computed_measurements == 0:
        st.info(
            "Signals are recorded, but none have a computed forward return yet — "
            "rows are still pending or marked insufficient, so hit rate and "
            "return metrics stay blank instead of treating them as losses."
        )
    elif all(row.average_excess_return_pct is None for row in summary.rows):
        st.caption(
            "Benchmark/excess returns are unavailable because benchmark "
            "instruments are not configured yet."
        )


def _render_dashboard_sections(dashboard: ValidationDashboardSummary) -> None:
    """Render the VALID-004 dashboard sections as compact, sortable tables."""
    sections = [
        ("Return distribution", _validation_distribution_frame(dashboard)),
        ("Win rate by holding period", _validation_horizon_frame(dashboard)),
        ("Benchmark-relative performance", _validation_benchmark_frame(dashboard)),
        ("Signal count over time", _validation_time_series_frame(dashboard)),
        ("Sector concentration", _validation_sector_frame(dashboard)),
        ("Best/worst signals", _validation_best_worst_frame(dashboard.metric_summary)),
    ]
    for title, frame in sections:
        st.subheader(title)
        if frame.empty:
            st.caption("No computed rows are available for this section yet.")
            continue
        st.dataframe(
            frame,
            width="stretch",
            hide_index=True,
            key=f"validation_{title.lower().replace('/', '_').replace(' ', '_')}",
        )
