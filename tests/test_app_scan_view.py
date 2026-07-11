"""Render-path tests for ui/scan_view.py (TEST-007).

REF-002 (#97) moved the scan-results pipeline out of app.py into
ui/scan_view.py with no direct tests. House pattern: monkeypatch
``ui.scan_view.st`` with a recording fake and drive the real render paths —
summary + diagnostics, the empty-results branch, the AUTH-003 export gate,
failure expanders, and the two-widget table/dropdown chart sync.
"""

from __future__ import annotations

from types import SimpleNamespace

import pandas as pd
import pytest

from backend.screener_registry import ScreenerDefinition
from ui import scan_view


class _FakeColumn:
    def __init__(self, owner: _FakeStreamlit):
        self._owner = owner

    def metric(self, label, value, **_kwargs):
        self._owner.metrics.append((label, value))

    def caption(self, text, **_kwargs):
        self._owner.captions.append(str(text))


class _FakeExpander:
    def __init__(self, owner: _FakeStreamlit, label: str):
        owner.expanders.append(label)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeTableState(SimpleNamespace):
    """Mimics st.dataframe's return: .selection.rows drives the chart sync."""


class _FakeStreamlit:
    def __init__(self):
        self.session_state: dict = {}
        self.markdowns: list[str] = []
        self.captions: list[str] = []
        self.warnings: list[str] = []
        self.metrics: list[tuple[str, object]] = []
        self.expanders: list[str] = []
        self.dataframes: list[object] = []
        self.subheaders: list[str] = []
        self.download_buttons: list[dict] = []
        self.selectboxes: list[tuple[str, list[str]]] = []
        # Programmed selection rows for the NEXT st.dataframe call.
        self.table_selection_rows: list[int] = []
        # Programmed return for download_button (True = user clicked).
        self.download_clicked = False

    def markdown(self, text, **_kwargs):
        self.markdowns.append(str(text))

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def warning(self, text, **_kwargs):
        self.warnings.append(str(text))

    def expander(self, label, **_kwargs):
        return _FakeExpander(self, str(label))

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def dataframe(self, data, **_kwargs):
        self.dataframes.append(data)
        return _FakeTableState(
            selection=SimpleNamespace(rows=list(self.table_selection_rows))
        )

    def download_button(self, label, **kwargs):
        self.download_buttons.append({"label": label, **kwargs})
        return self.download_clicked

    def divider(self):
        pass

    def subheader(self, text, **_kwargs):
        self.subheaders.append(str(text))

    def selectbox(self, label, options, *, key, **_kwargs):
        self.selectboxes.append((str(label), list(options)))
        return self.session_state[key]


def _definition(*, with_chart: bool = True) -> ScreenerDefinition:
    return ScreenerDefinition(
        key="demo",
        name="Demo screener",
        description="test",
        universe="nifty_100",
        timeframe="1d",
        lookback_days=100,
        default_params={},
        module_name="demo",
        run=lambda **_kwargs: None,
        build_chart=(lambda **_kwargs: None) if with_chart else None,
    )


def _cache(results: pd.DataFrame) -> dict:
    return {
        "results": results,
        "stats": {
            "cache_hits": 3,
            "cache_misses": 1,
            "api_attempts": 4,
            "rate_limit_retries": 0,
        },
        "failures": [],
        "compute_failures": [],
        "universe_df": pd.DataFrame(),
        "data_loader": object(),
        "params_for_chart": {},
    }


def _results(symbols: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "symbol": symbols,
            "close": [100.0 + i for i in range(len(symbols))],
            "final_score": [90.0 - i for i in range(len(symbols))],
            "rating": ["BUY"] * len(symbols),
        }
    )


@pytest.fixture()
def fake_st(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setattr(scan_view, "st", fake)
    return fake


def test_empty_results_warn_and_skip_chart_export_and_fundamentals(fake_st, monkeypatch):
    panel_calls: list[object] = []
    monkeypatch.setattr(scan_view, "_render_fundamentals_panel", panel_calls.append)

    scan_view._render_scan_output(_definition(), _cache(_results([])), can_export=True)

    assert fake_st.warnings == ["The screener returned no rows."]
    assert fake_st.download_buttons == []
    assert panel_calls == []
    assert any("0 stock(s) shortlisted" in m for m in fake_st.markdowns)
    assert "Run details" in fake_st.expanders  # diagnostics still shown


def test_happy_path_charts_symbol_and_shows_fundamentals(fake_st, monkeypatch):
    panel_calls: list[object] = []
    monkeypatch.setattr(scan_view, "_render_fundamentals_panel", panel_calls.append)
    monkeypatch.setattr(
        scan_view, "_render_results_with_chart", lambda selected, results, cache: "INFY"
    )

    scan_view._render_scan_output(_definition(), _cache(_results(["INFY", "TCS"])), can_export=False)

    assert panel_calls == ["INFY"]
    assert any("2 stock(s) shortlisted" in m for m in fake_st.markdowns)
    # Diagnostics metrics rendered inside the expander.
    assert ("Cache hits", 3) in fake_st.metrics


def test_export_button_is_capability_gated(fake_st, monkeypatch):
    """AUTH-003: viewers never reach the download button or the bytes build."""
    monkeypatch.setattr(scan_view, "_render_fundamentals_panel", lambda symbol: None)
    monkeypatch.setattr(
        scan_view, "_render_results_with_chart", lambda selected, results, cache: None
    )

    scan_view._render_scan_output(_definition(), _cache(_results(["INFY"])), can_export=False)
    assert fake_st.download_buttons == []

    scan_view._render_scan_output(_definition(), _cache(_results(["INFY"])), can_export=True)
    assert len(fake_st.download_buttons) == 1
    assert fake_st.download_buttons[0]["file_name"] == "demo_results.csv"


def test_export_click_records_audit_event(fake_st, monkeypatch):
    """st.download_button doubles as the OBS-003 export trigger."""
    monkeypatch.setattr(scan_view, "_render_fundamentals_panel", lambda symbol: None)
    monkeypatch.setattr(
        scan_view, "_render_results_with_chart", lambda selected, results, cache: None
    )
    audit_events: list[dict] = []
    monkeypatch.setattr(
        scan_view, "record_audit_event", lambda **kwargs: audit_events.append(kwargs)
    )
    fake_st.download_clicked = True
    fake_st.session_state["_audit_user_email"] = "analyst@example.com"

    scan_view._render_scan_output(_definition(), _cache(_results(["INFY", "TCS"])), can_export=True)

    assert len(audit_events) == 1
    assert audit_events[0]["user_email"] == "analyst@example.com"
    assert audit_events[0]["metadata"]["row_count"] == 2
    assert audit_events[0]["metadata"]["file_name"] == "demo_results.csv"


def test_failure_expanders_render_with_redacted_messages(fake_st, monkeypatch):
    monkeypatch.setattr(scan_view, "_render_fundamentals_panel", lambda symbol: None)
    monkeypatch.setattr(
        scan_view, "_render_results_with_chart", lambda selected, results, cache: None
    )
    redacted: list[str] = []

    def _fake_redact(text):
        redacted.append(text)
        return "[REDACTED]"

    monkeypatch.setattr(scan_view, "_redact_secrets", _fake_redact)
    cache = _cache(_results(["INFY"]))
    cache["failures"] = [{"symbol": "TCS", "message": "token=super-secret"}]
    cache["compute_failures"] = [{"symbol": "WIPRO", "message": "boom"}]

    scan_view._render_scan_output(_definition(), cache, can_export=False)

    assert "Fetch failures" in fake_st.expanders
    assert "Compute failures" in fake_st.expanders
    assert "token=super-secret" in redacted and "boom" in redacted
    # Both failure frames were rendered, with messages replaced.
    failure_frames = fake_st.dataframes[-2:]
    assert all((frame["message"] == "[REDACTED]").all() for frame in failure_frames)


# ---------------------------------------------------------------------------
# _render_results_with_chart — the two-widget table/dropdown sync
# ---------------------------------------------------------------------------


def _chart_stub(monkeypatch, rendered: list[str]):
    def _fake_chart(*, selected, chart_symbol, universe_df, data_loader, params_for_chart):
        rendered.append(chart_symbol)
        return chart_symbol

    monkeypatch.setattr(scan_view, "_render_cached_symbol_chart", _fake_chart)


def test_chart_returns_none_without_symbol_column_or_chart_builder(fake_st, monkeypatch):
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    no_symbol = pd.DataFrame({"close": [1.0]})

    assert (
        scan_view._render_results_with_chart(_definition(), no_symbol, _cache(no_symbol)) is None
    )
    assert (
        scan_view._render_results_with_chart(
            _definition(with_chart=False), _results(["INFY"]), _cache(_results(["INFY"]))
        )
        is None
    )
    assert rendered == []


def test_chart_defaults_to_first_symbol_without_selection(fake_st, monkeypatch):
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    results = _results(["INFY", "TCS"])

    shown = scan_view._render_results_with_chart(_definition(), results, _cache(results))

    assert shown == "INFY"
    assert rendered == ["INFY"]
    assert fake_st.session_state["chart_symbol_demo"] == "INFY"
    # BUY/SELL legend caption rendered because a rating column exists.
    assert any("click a row" in caption for caption in fake_st.captions)


def test_fresh_table_click_moves_the_dropdown(fake_st, monkeypatch):
    """A CHANGED table row wins over the stored dropdown pick."""
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    results = _results(["INFY", "TCS", "WIPRO"])
    fake_st.session_state["chart_symbol_demo"] = "INFY"  # previous dropdown pick
    fake_st.table_selection_rows = [2]  # user just clicked the WIPRO row

    shown = scan_view._render_results_with_chart(_definition(), results, _cache(results))

    assert shown == "WIPRO"
    assert fake_st.session_state["chart_symbol_demo"] == "WIPRO"
    assert fake_st.session_state["chart_prev_table_row_demo"] == 2
    assert fake_st.session_state["chart_prev_table_symbol_demo"] == "WIPRO"


def test_reordered_results_keep_table_highlight_dropdown_and_chart_in_sync(
    fake_st, monkeypatch
):
    """A persistent row number must be re-evaluated when its symbol changes.

    Beginner note: Streamlit remembers a selected *row number* across reruns.
    If a fresh scan sorts the symbols differently, row 2 can now represent a
    different company. The chart and dropdown must follow the symbol currently
    highlighted in the table, not the symbol that used to occupy row 2.
    """
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    fake_st.table_selection_rows = [2]

    first_results = _results(["INFY", "TCS", "WIPRO"])
    first_shown = scan_view._render_results_with_chart(
        _definition(), first_results, _cache(first_results)
    )

    reordered_results = _results(["WIPRO", "TCS", "INFY"])
    second_shown = scan_view._render_results_with_chart(
        _definition(), reordered_results, _cache(reordered_results)
    )

    assert first_shown == "WIPRO"
    assert second_shown == "INFY"
    assert rendered == ["WIPRO", "INFY"]
    assert fake_st.session_state["chart_symbol_demo"] == "INFY"
    assert fake_st.session_state["chart_prev_table_row_demo"] == 2
    assert fake_st.session_state["chart_prev_table_symbol_demo"] == "INFY"


def test_stale_table_selection_does_not_override_dropdown(fake_st, monkeypatch):
    """An UNCHANGED (persistent) table selection must not clobber a fresh
    dropdown choice on the next rerun."""
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    results = _results(["INFY", "TCS", "WIPRO"])
    fake_st.session_state["chart_symbol_demo"] = "TCS"  # fresh dropdown pick
    fake_st.session_state["chart_prev_table_row_demo"] = 2  # same row as last rerun
    fake_st.session_state["chart_prev_table_symbol_demo"] = "WIPRO"
    fake_st.table_selection_rows = [2]  # persistent selection

    shown = scan_view._render_results_with_chart(_definition(), results, _cache(results))

    assert shown == "TCS"  # dropdown wins; stale click ignored
    assert fake_st.session_state["chart_symbol_demo"] == "TCS"


def test_stored_pick_invalidated_when_symbols_change(fake_st, monkeypatch):
    """A re-run that drops the previously charted symbol falls back to the first."""
    rendered: list[str] = []
    _chart_stub(monkeypatch, rendered)
    results = _results(["TCS", "WIPRO"])
    fake_st.session_state["chart_symbol_demo"] = "INFY"  # no longer shortlisted

    shown = scan_view._render_results_with_chart(_definition(), results, _cache(results))

    assert shown == "TCS"
    assert fake_st.session_state["chart_symbol_demo"] == "TCS"


def test_has_rating_column():
    assert scan_view._has_rating_column(pd.DataFrame({"rating": ["BUY"]})) is True
    assert scan_view._has_rating_column(pd.DataFrame({"signal": ["SELL"]})) is True
    assert scan_view._has_rating_column(pd.DataFrame({"close": [1.0]})) is False
