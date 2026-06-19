"""Tests for the VALID-003B validation/signal-performance page helpers.

Like the scan-history page, the validation dashboard splits Streamlit rendering
from pure data shaping. The helpers tested here take widget values or a
``ValidationSummary`` and return repository kwargs or a display table, so they
run without a browser, a database, or a Streamlit runtime.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from backend.validation.metrics import (
    BestWorstSignal,
    ValidationMetricFilters,
    ValidationMetricRow,
    ValidationSummary,
)
from ui.validation_page import (
    _SUMMARY_COLUMNS,
    _format_pct,
    _format_signal,
    _validation_filter_kwargs,
    _validation_summary_frame,
)

# ---------------------------------------------------------------------------
# _format_pct: stored Decimal -> 2-dp display string
# ---------------------------------------------------------------------------


def test_format_pct_rounds_to_two_decimals_and_keeps_sign():
    assert _format_pct(Decimal("2.5000")) == "2.50%"
    assert _format_pct(Decimal("-5.0000")) == "-5.00%"
    assert _format_pct(Decimal("0")) == "0.00%"
    # Half-even rounding of the 4-dp store down to 2 dp is display-only.
    assert _format_pct(Decimal("12.3456")) == "12.35%"


def test_format_pct_missing_value_is_em_dash():
    """A missing metric (no computed rows, or no benchmark) never reads as 0%."""
    assert _format_pct(None) == "—"


# ---------------------------------------------------------------------------
# _format_signal: best/worst signal -> compact display string
# ---------------------------------------------------------------------------


def _signal(**overrides) -> BestWorstSignal:
    defaults = dict(
        run_id=1,
        result_id=1,
        symbol="RELIANCE",
        signal_date=dt.date(2026, 1, 5),
        horizon_days=20,
        forward_return_pct=Decimal("10.0000"),
        excess_return_pct=Decimal("2.0000"),
    )
    defaults.update(overrides)
    return BestWorstSignal(**defaults)


def test_format_signal_shows_symbol_return_and_date():
    assert _format_signal(_signal()) == "RELIANCE 10.00% (2026-01-05)"


def test_format_signal_handles_missing_date_and_missing_signal():
    assert _format_signal(_signal(signal_date=None)) == "RELIANCE 10.00% (—)"
    assert _format_signal(None) == "—"


# ---------------------------------------------------------------------------
# _validation_filter_kwargs: widget values -> summarize() kwargs
# ---------------------------------------------------------------------------


def test_filter_kwargs_empty_widgets_mean_no_filters():
    assert _validation_filter_kwargs("All", "All", "All", ()) == {}
    assert _validation_filter_kwargs(None, None, None, None) == {}


def test_filter_kwargs_maps_each_widget_to_its_summarize_argument():
    kwargs = _validation_filter_kwargs(
        "envelope",
        "nifty_500",
        "60",
        (dt.date(2026, 1, 5), dt.date(2026, 1, 10)),
    )
    assert kwargs == {
        "screener_key": "envelope",
        "universe_key": "nifty_500",
        "horizon_days": 60,
        "signal_date_from": dt.date(2026, 1, 5),
        "signal_date_to": dt.date(2026, 1, 10),
    }


def test_filter_kwargs_handles_partial_date_range():
    """st.date_input yields a 1-item range mid-selection; that means from-only."""
    kwargs = _validation_filter_kwargs("All", "All", "20", (dt.date(2026, 1, 5),))
    assert kwargs == {"horizon_days": 20, "signal_date_from": dt.date(2026, 1, 5)}


# ---------------------------------------------------------------------------
# _validation_summary_frame: ValidationSummary -> display table
# ---------------------------------------------------------------------------


def _row(**overrides) -> ValidationMetricRow:
    defaults = dict(
        screener_key="envelope",
        universe_key="nifty_500",
        horizon_days=20,
        first_signal_date=dt.date(2026, 1, 5),
        last_signal_date=dt.date(2026, 1, 10),
        total_signals=4,
        computed_count=2,
        pending_count=1,
        insufficient_data_count=1,
        hit_rate_pct=Decimal("50.0000"),
        average_forward_return_pct=Decimal("2.5000"),
        median_forward_return_pct=Decimal("2.5000"),
        average_excess_return_pct=Decimal("2.0000"),
        median_excess_return_pct=Decimal("2.0000"),
        average_mae_pct=Decimal("-6.0000"),
        average_mfe_pct=Decimal("10.0000"),
        best_signal=_signal(symbol="RELIANCE", forward_return_pct=Decimal("10.0000")),
        worst_signal=_signal(
            result_id=2,
            symbol="TCS",
            signal_date=dt.date(2026, 1, 6),
            forward_return_pct=Decimal("-5.0000"),
            excess_return_pct=None,
        ),
    )
    defaults.update(overrides)
    return ValidationMetricRow(**defaults)


def _summary(rows: list[ValidationMetricRow]) -> ValidationSummary:
    return ValidationSummary(
        filters=ValidationMetricFilters(),
        rows=tuple(rows),
        total_measurements=sum(r.total_signals for r in rows),
        computed_measurements=sum(r.computed_count for r in rows),
        pending_measurements=sum(r.pending_count for r in rows),
        insufficient_data_measurements=sum(r.insufficient_data_count for r in rows),
    )


def test_summary_frame_empty_has_columns_but_no_rows():
    """An empty summary still produces the full, stable column contract."""
    frame = _validation_summary_frame(_summary([]))
    assert list(frame.columns) == list(_SUMMARY_COLUMNS)
    assert len(frame) == 0


def test_summary_frame_renders_mixed_status_row_with_formatted_values():
    frame = _validation_summary_frame(_summary([_row()]))

    assert list(frame.columns) == list(_SUMMARY_COLUMNS)
    cells = frame.iloc[0]
    assert cells["Screener"] == "envelope"
    assert cells["Universe"] == "nifty_500"
    assert cells["Horizon"] == "20D"
    assert cells["First signal"] == "2026-01-05"
    assert cells["Last signal"] == "2026-01-10"
    # Counts stay separate so pending/insufficient never read as losses.
    assert cells["Total signals"] == 4
    assert cells["Computed"] == 2
    assert cells["Pending"] == 1
    assert cells["Insufficient"] == 1
    assert cells["Hit rate %"] == "50.00%"
    assert cells["Avg return %"] == "2.50%"
    assert cells["Avg excess %"] == "2.00%"
    assert cells["Avg MAE %"] == "-6.00%"
    assert cells["Avg MFE %"] == "10.00%"
    # Best/worst show the ranked symbol, its return, and its signal date.
    assert cells["Best signal"] == "RELIANCE 10.00% (2026-01-05)"
    assert cells["Worst signal"] == "TCS -5.00% (2026-01-06)"


def test_summary_frame_missing_metrics_show_em_dash_not_zero():
    """No computed rows / no benchmark must render as em-dash, not 0.00%."""
    pending_only = _row(
        total_signals=2,
        computed_count=0,
        pending_count=2,
        insufficient_data_count=0,
        hit_rate_pct=None,
        average_forward_return_pct=None,
        median_forward_return_pct=None,
        average_excess_return_pct=None,
        median_excess_return_pct=None,
        average_mae_pct=None,
        average_mfe_pct=None,
        best_signal=None,
        worst_signal=None,
    )
    cells = _validation_summary_frame(_summary([pending_only])).iloc[0]
    assert cells["Computed"] == 0
    assert cells["Hit rate %"] == "—"
    assert cells["Avg return %"] == "—"
    assert cells["Avg excess %"] == "—"
    assert cells["Best signal"] == "—"
    assert cells["Worst signal"] == "—"
