"""Tests for the VALID-003B validation/signal-performance page helpers.

Like the scan-history page, the validation dashboard splits Streamlit rendering
from pure data shaping. The helpers tested here take widget values or a
``ValidationSummary`` and return repository kwargs or a display table, so they
run without a browser, a database, or a Streamlit runtime.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from decimal import Decimal
from types import SimpleNamespace

import pytest
from sqlalchemy.exc import OperationalError

import ui.validation_page as validation_page
from backend.storage.models import ForwardReturnStatus
from backend.validation.metrics import (
    BestWorstSignal,
    ValidationBenchmarkRow,
    ValidationDashboardSummary,
    ValidationMetricFilters,
    ValidationMetricRow,
    ValidationReturnBucket,
    ValidationSectorConcentrationRow,
    ValidationSummary,
    ValidationTimeSeriesPoint,
)
from ui.validation_page import (
    _SUMMARY_COLUMNS,
    _format_pct,
    _format_signal,
    _render_validation_page,
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


def _dashboard(rows: list[ValidationMetricRow]) -> ValidationDashboardSummary:
    return ValidationDashboardSummary(
        metric_summary=_summary(rows),
        return_distribution=(
            ValidationReturnBucket(
                screener_key="envelope",
                universe_key="nifty_500",
                horizon_days=20,
                bucket_label="0% to 10%",
                computed_count=1,
            ),
        ),
        benchmark_relative_rows=(
            ValidationBenchmarkRow(
                screener_key="envelope",
                universe_key="nifty_500",
                horizon_days=20,
                computed_count=1,
                hit_rate_pct=Decimal("100.0000"),
                average_excess_return_pct=Decimal("2.0000"),
                median_excess_return_pct=Decimal("2.0000"),
            ),
        ),
        signal_count_over_time=(
            ValidationTimeSeriesPoint(
                screener_key="envelope",
                universe_key="nifty_500",
                horizon_days=20,
                period_start=dt.date(2026, 1, 1),
                total_signals=1,
                computed_count=1,
                pending_count=0,
                insufficient_data_count=0,
            ),
        ),
        sector_concentration=(
            ValidationSectorConcentrationRow(
                screener_key="envelope",
                universe_key="nifty_500",
                horizon_days=20,
                sector="Technology",
                total_signals=1,
                computed_count=1,
                share_of_group_pct=Decimal("100.0000"),
                hit_rate_pct=Decimal("100.0000"),
                average_forward_return_pct=Decimal("10.0000"),
            ),
        ),
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


# ---------------------------------------------------------------------------
# _render_validation_page: Streamlit widgets -> summarize() -> rendered states
# ---------------------------------------------------------------------------


class _FakeColumn:
    """Minimal context manager for ``with st.columns(...)[i]:`` blocks."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeValidationSt:
    """Small fake Streamlit surface for render-level page tests.

    The real page is intentionally boring: choose filters, call the backend read
    model, then display a table or explanatory empty state. This fake keeps the
    tests focused on that contract without starting a Streamlit runtime.
    """

    def __init__(
        self,
        *,
        screener="All",
        universe="All",
        horizon="All",
        date_range=(),
    ):
        self.choices = {
            "Screener": screener,
            "Universe": universe,
            "Horizon": horizon,
        }
        self.date_range = date_range
        self.tables: list[SimpleNamespace] = []
        self.downloads: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.captions: list[str] = []
        self.subheaders: list[str] = []
        self.download_clicked = False

    def subheader(self, message, *_args, **_kwargs):
        self.subheaders.append(str(message))

    def caption(self, message, *_args, **_kwargs):
        self.captions.append(str(message))

    def markdown(self, message, *_args, **_kwargs):
        self.captions.append(str(message))

    def columns(self, count):
        return [_FakeColumn() for _ in range(count)]

    def selectbox(self, label, _options, **_kwargs):
        return self.choices[label]

    def date_input(self, *_args, **_kwargs):
        return self.date_range

    def dataframe(self, frame, **kwargs):
        self.tables.append(SimpleNamespace(frame=frame, kwargs=kwargs))

    def download_button(self, **kwargs):
        self.downloads.append(kwargs)
        return self.download_clicked

    def info(self, message, *_args, **_kwargs):
        self.infos.append(str(message))

    def error(self, message, *_args, **_kwargs):
        self.errors.append(str(message))


@contextmanager
def _fake_session_scope():
    yield object()


def test_render_validation_page_passes_widget_filters_to_dashboard_service(monkeypatch):
    """The rendered page must call the backend read model, not rebuild queries."""
    fake_st = _FakeValidationSt(
        screener="envelope",
        universe="nifty_500",
        horizon="60",
        date_range=(dt.date(2026, 1, 5), dt.date(2026, 1, 10)),
    )
    captured_kwargs: dict[str, object] = {}

    def summarize(_session, **kwargs):
        captured_kwargs.update(kwargs)
        return _dashboard([_row(horizon_days=60)])

    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        validation_page, "list_distinct_screener_keys", lambda _session: ["envelope"]
    )
    monkeypatch.setattr(
        validation_page, "list_distinct_universe_keys", lambda _session: ["nifty_500"]
    )
    monkeypatch.setattr(validation_page, "summarize_validation_dashboard", summarize)

    _render_validation_page(can_export=True)

    assert captured_kwargs == {
        "screener_key": "envelope",
        "universe_key": "nifty_500",
        "horizon_days": 60,
        "signal_date_from": dt.date(2026, 1, 5),
        "signal_date_to": dt.date(2026, 1, 10),
        "sector_lookup": {},
    }
    assert len(fake_st.tables) >= 1
    assert fake_st.tables[0].frame.iloc[0]["Horizon"] == "60D"


def test_render_validation_page_handles_summary_schema_operational_error(monkeypatch):
    """A partially migrated DB should show a friendly hint, not a traceback."""
    fake_st = _FakeValidationSt()

    def summarize(_session, **_kwargs):
        raise OperationalError("SELECT signal_forward_returns", {}, Exception("missing"))

    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(validation_page, "list_distinct_screener_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "list_distinct_universe_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "summarize_validation_dashboard", summarize)

    _render_validation_page(can_export=True)

    assert fake_st.errors == [
        "Validation tables are missing or outdated. "
        "Run `python -m alembic upgrade head` and reload this page."
    ]
    assert fake_st.tables == []


@pytest.mark.parametrize(
    ("status", "expected_message"),
    [
        (
            ForwardReturnStatus.PENDING,
            "Signals are recorded, but none have a computed forward return yet",
        ),
        (
            ForwardReturnStatus.INSUFFICIENT_DATA,
            "pending or marked insufficient",
        ),
    ],
)
def test_render_validation_page_explains_no_computed_rows_without_blaming_hit_rate(
    monkeypatch, status, expected_message
):
    """Pending and insufficient rows stay visible but never count as losses."""
    fake_st = _FakeValidationSt()
    row = _row(
        total_signals=1,
        computed_count=0,
        pending_count=1 if status is ForwardReturnStatus.PENDING else 0,
        insufficient_data_count=1 if status is ForwardReturnStatus.INSUFFICIENT_DATA else 0,
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

    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(validation_page, "list_distinct_screener_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "list_distinct_universe_keys", lambda _session: [])
    monkeypatch.setattr(
        validation_page,
        "summarize_validation_dashboard",
        lambda _session, **_kwargs: _dashboard([row]),
    )

    _render_validation_page(can_export=True)

    assert any(expected_message in message for message in fake_st.infos)
    assert fake_st.tables


def test_render_validation_page_reports_benchmark_gap_only_after_computed_rows(
    monkeypatch,
):
    """Benchmark/excess empty state is separate from the no-computed state."""
    fake_st = _FakeValidationSt()
    row = _row(average_excess_return_pct=None, median_excess_return_pct=None)

    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(validation_page, "list_distinct_screener_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "list_distinct_universe_keys", lambda _session: [])
    monkeypatch.setattr(
        validation_page,
        "summarize_validation_dashboard",
        lambda _session, **_kwargs: _dashboard([row]),
    )

    _render_validation_page(can_export=True)

    assert not fake_st.infos
    assert any("Benchmark/excess returns are unavailable" in text for text in fake_st.captions)


def test_render_validation_page_renders_dashboard_sections_and_safe_export(monkeypatch):
    fake_st = _FakeValidationSt()
    fake_st.download_clicked = True
    audits: list[dict[str, object]] = []
    row = _row(
        best_signal=_signal(symbol="=HACK", forward_return_pct=Decimal("10.0000")),
        worst_signal=_signal(symbol="-RISK", forward_return_pct=Decimal("-4.0000")),
    )

    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(validation_page, "list_distinct_screener_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "list_distinct_universe_keys", lambda _session: [])
    monkeypatch.setattr(
        validation_page,
        "summarize_validation_dashboard",
        lambda _session, **_kwargs: _dashboard([row]),
    )
    monkeypatch.setattr(
        validation_page,
        "record_audit_event",
        lambda **kwargs: audits.append(kwargs),
    )

    _render_validation_page(can_export=True)

    assert any("Return distribution" in title for title in fake_st.subheaders)
    assert any("Win rate by holding period" in title for title in fake_st.subheaders)
    assert any("Signal count over time" in title for title in fake_st.subheaders)
    assert any("Sector concentration" in title for title in fake_st.subheaders)
    assert len(fake_st.tables) >= 6
    assert fake_st.downloads
    csv_bytes = fake_st.downloads[0]["data"]
    assert isinstance(csv_bytes, bytes)
    csv_text = csv_bytes.decode("utf-8")
    assert "'=HACK" in csv_text
    assert "'-RISK" in csv_text
    assert audits
    assert audits[0]["event"] == "export_downloaded"


def test_viewer_validation_page_does_not_build_or_render_export(monkeypatch):
    fake_st = _FakeValidationSt()
    monkeypatch.setattr(validation_page, "st", fake_st)
    monkeypatch.setattr(validation_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(validation_page, "list_distinct_screener_keys", lambda _session: [])
    monkeypatch.setattr(validation_page, "list_distinct_universe_keys", lambda _session: [])
    monkeypatch.setattr(
        validation_page,
        "summarize_validation_dashboard",
        lambda _session, **_kwargs: _dashboard([_row()]),
    )

    _render_validation_page(can_export=False)

    assert fake_st.downloads == []
