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

from backend.storage import (
    list_distinct_screener_keys,
    list_distinct_universe_keys,
    session_scope,
)
from backend.validation import (
    FORWARD_RETURN_HORIZONS,
    summarize_validation_metrics,
)
from backend.validation.metrics import BestWorstSignal, ValidationSummary

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
    with session_scope() as session:
        # ``summarize_validation_metrics`` returns plain frozen dataclasses (no
        # ORM rows), so the summary is safe to use after the session closes.
        summary = summarize_validation_metrics(session, **filters)

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

    st.dataframe(
        _validation_summary_frame(summary),
        width="stretch",
        hide_index=True,
        key="validation_summary_table",
    )

    # Empty-state notes that still make sense once the table itself is shown.
    # These are mutually exclusive: with zero computed rows every excess is also
    # null, so the "no computed rows" note already covers it; the benchmark note
    # only adds value once returns exist but no benchmark was configured.
    if summary.computed_measurements == 0:
        st.info(
            "Signals are recorded, but none have a computed forward return yet — "
            "the holding windows are still pending."
        )
    elif all(row.average_excess_return_pct is None for row in summary.rows):
        st.caption(
            "Benchmark/excess returns are unavailable because benchmark "
            "instruments are not configured yet."
        )
