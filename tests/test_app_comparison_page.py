"""Tests for the JOB-003 Streamlit scan comparison page helpers.

Beginner note:
Streamlit needs a browser to run for real, which is too heavy for unit tests.
Instead we ``monkeypatch`` the page module's ``st`` with a tiny fake
(``_FakeComparisonSt``) that records what the page *tried* to render - subheaders,
tables, downloads, info/error messages - so each test can assert on that record.
We also stub the database calls (``session_scope``, ``list_finalized_scan_groups``,
``build_scan_comparison``) so the tests never touch a real database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from decimal import Decimal
from types import SimpleNamespace

import pandas as pd
from sqlalchemy.exc import OperationalError

import ui.comparison_page as comparison_page
from backend.scanning.comparison import ComparisonRun, ScanComparison


class _FakeColumn:
    """Stand-in for a Streamlit column (``st.columns(...)`` element).

    Supports the context-manager protocol (``with col:``) and ``.metric(...)`` so
    the page's column usage runs without a real Streamlit runtime.
    """

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False

    def metric(self, *_args, **_kwargs):
        return None


class _FakeComparisonSt:
    """Small fake Streamlit surface for render-level comparison page tests."""

    def __init__(self, *, screener="envelope", universe="nifty_500"):
        self.choices = {"Screener": screener, "Universe": universe}
        self.tables: list[SimpleNamespace] = []
        self.downloads: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.infos: list[str] = []
        self.captions: list[str] = []
        self.subheaders: list[str] = []
        self.metrics: list[tuple[str, object]] = []
        self.download_clicked = False
        self.session_state = {"_audit_user_email": "analyst@example.com"}

    def subheader(self, message, *_args, **_kwargs):
        self.subheaders.append(str(message))

    def caption(self, message, *_args, **_kwargs):
        self.captions.append(str(message))

    def columns(self, count):
        return [_FakeColumn() for _ in range(count)]

    def selectbox(self, label, options, **_kwargs):
        assert self.choices[label] in options
        return self.choices[label]

    def dataframe(self, frame, **kwargs):
        self.tables.append(SimpleNamespace(frame=frame, kwargs=kwargs))

    def download_button(self, **kwargs):
        self.downloads.append(kwargs)
        return self.download_clicked

    def info(self, message, *_args, **_kwargs):
        self.infos.append(str(message))

    def error(self, message, *_args, **_kwargs):
        self.errors.append(str(message))

    def metric(self, label, value, *_args, **_kwargs):
        self.metrics.append((str(label), value))


@contextmanager
def _fake_session_scope():
    """Replace ``session_scope`` with a no-op context yielding a dummy session.

    The repository calls are stubbed in each test, so the yielded object is never
    actually used to talk to a database - it just satisfies the ``with`` block.
    """
    yield object()


@dataclass(frozen=True)
class _Row:
    """Minimal stand-in for ``ComparisonRow`` with test-friendly defaults.

    The page only reads attributes off the rows it renders, so a small dataclass
    is enough; defaults keep each test's row construction terse.
    """

    symbol: str
    latest_rating: str = "BUY"
    previous_rating: str | None = None
    latest_signal_date: str | None = None
    previous_signal_date: str | None = None
    latest_close: Decimal | None = None
    previous_close: Decimal | None = None
    latest_score: Decimal | None = None
    previous_score: Decimal | None = None
    score_source: str | None = None
    score_delta: Decimal | None = None
    latest_reason: str = ""
    previous_reason: str = ""


def _comparison() -> ScanComparison:
    """Build a fully populated two-run ``ScanComparison`` fixture for render tests.

    Covers all five sections (one row each, except the empty degraded bucket) so a
    single render exercises every table and the CSV export path.
    """
    latest = ComparisonRun(
        run_id=2,
        started="2026-06-20 09:00 UTC",
        finished="2026-06-20 09:01 UTC",
        status="success",
        screener_key="envelope",
        universe_key="nifty_500",
        symbols_scanned=500,
        shortlisted=2,
    )
    previous = ComparisonRun(
        run_id=1,
        started="2026-06-19 09:00 UTC",
        finished="2026-06-19 09:01 UTC",
        status="success",
        screener_key="envelope",
        universe_key="nifty_500",
        symbols_scanned=500,
        shortlisted=2,
    )
    return ScanComparison(
        latest_run=latest,
        previous_run=previous,
        new_today=(_Row("INFY", latest_reason="fresh"),),
        repeated_from_yesterday=(_Row("TCS", previous_rating="BUY"),),
        dropped_today=(
            _Row("RELIANCE", latest_rating="", previous_rating="BUY", previous_reason="old"),
        ),
        improved_score=(
            _Row(
                "TCS",
                previous_rating="BUY",
                latest_score=Decimal("8"),
                previous_score=Decimal("6"),
                score_source="confidence",
                score_delta=Decimal("2"),
            ),
        ),
        degraded_score=(),
    )


def test_comparison_universe_options_follow_selected_screener():
    groups = [("alpha", "nifty_500"), ("alpha", "fno"), ("beta", "nifty_100")]

    assert comparison_page._comparison_screener_options(groups) == ["alpha", "beta"]
    assert comparison_page._comparison_universe_options(groups, "alpha") == [
        "fno",
        "nifty_500",
    ]


def test_comparison_export_csv_is_formula_safe():
    frame = pd.DataFrame([{"Symbol": "TCS", "Latest reason": "=cmd"}])

    payload = comparison_page._comparison_export_csv(frame)

    assert payload.decode("utf-8").splitlines()[1].endswith("'=cmd")


def test_comparison_download_file_token_is_conservative():
    assert comparison_page._safe_file_token("../=evil key") == "evil_key"
    assert comparison_page._safe_file_token("   ") == "unknown"


def test_render_comparison_page_passes_selected_pair_to_read_model(monkeypatch):
    fake_st = _FakeComparisonSt(screener="envelope", universe="nifty_500")
    captured: dict[str, object] = {}

    def build(_session, **kwargs):
        captured.update(kwargs)
        return _comparison()

    monkeypatch.setattr(comparison_page, "st", fake_st)
    monkeypatch.setattr(comparison_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        comparison_page,
        "list_finalized_scan_groups",
        lambda _session: [("envelope", "nifty_500")],
    )
    monkeypatch.setattr(comparison_page, "build_scan_comparison", build)

    comparison_page._render_comparison_page()

    assert captured == {"screener_key": "envelope", "universe_key": "nifty_500"}
    assert [table.kwargs["key"] for table in fake_st.tables] == [
        "comparison_new_today",
        "comparison_repeated_from_yesterday",
        "comparison_dropped_today",
        "comparison_improved_score",
    ]
    assert fake_st.downloads[0]["file_name"] == "scan_comparison_envelope_nifty_500.csv"


def test_render_comparison_page_audits_csv_export(monkeypatch):
    fake_st = _FakeComparisonSt()
    fake_st.download_clicked = True
    audit_events: list[dict[str, object]] = []

    monkeypatch.setattr(comparison_page, "st", fake_st)
    monkeypatch.setattr(comparison_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        comparison_page,
        "list_finalized_scan_groups",
        lambda _session: [("envelope", "nifty_500")],
    )
    monkeypatch.setattr(
        comparison_page,
        "build_scan_comparison",
        lambda _session, **_kwargs: _comparison(),
    )
    monkeypatch.setattr(
        comparison_page,
        "record_audit_event",
        lambda **kwargs: audit_events.append(kwargs),
    )

    comparison_page._render_comparison_page()

    assert audit_events == [
        {
            "event": comparison_page.EVENT_EXPORT_DOWNLOADED,
            "user_email": "analyst@example.com",
            "metadata": {
                "file_name": "scan_comparison_envelope_nifty_500.csv",
                "row_count": 4,
                "kind": "scan_comparison",
                "screener_key": "envelope",
                "universe_key": "nifty_500",
                "latest_run_id": 2,
                "previous_run_id": 1,
            },
        }
    ]


def test_render_comparison_page_handles_schema_operational_error(monkeypatch):
    fake_st = _FakeComparisonSt()

    def fail(_session):
        raise OperationalError("SELECT scan_runs", {}, Exception("missing"))

    monkeypatch.setattr(comparison_page, "st", fake_st)
    monkeypatch.setattr(comparison_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(comparison_page, "list_finalized_scan_groups", fail)

    comparison_page._render_comparison_page()

    assert fake_st.errors == [
        "Scan comparison tables are missing or outdated. "
        "Run `python -m alembic upgrade head` and reload this page."
    ]
    assert fake_st.tables == []


def test_render_comparison_page_explains_when_only_one_run_exists(monkeypatch):
    latest_only = ScanComparison(
        latest_run=_comparison().latest_run,
        previous_run=None,
    )
    fake_st = _FakeComparisonSt()

    monkeypatch.setattr(comparison_page, "st", fake_st)
    monkeypatch.setattr(comparison_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        comparison_page,
        "list_finalized_scan_groups",
        lambda _session: [("envelope", "nifty_500")],
    )
    monkeypatch.setattr(
        comparison_page,
        "build_scan_comparison",
        lambda _session, **_kwargs: latest_only,
    )

    comparison_page._render_comparison_page()

    assert any("Need at least two finalized runs" in message for message in fake_st.infos)
    assert fake_st.tables == []


def test_render_comparison_page_handles_value_error_from_build(monkeypatch):
    fake_st = _FakeComparisonSt()

    def build(_session, **_kwargs):
        raise ValueError("No finalized scan runs found for envelope/nifty_500.")

    monkeypatch.setattr(comparison_page, "st", fake_st)
    monkeypatch.setattr(comparison_page, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        comparison_page,
        "list_finalized_scan_groups",
        lambda _session: [("envelope", "nifty_500")],
    )
    monkeypatch.setattr(comparison_page, "build_scan_comparison", build)

    comparison_page._render_comparison_page()

    assert any(
        "No finalized runs are available" in message for message in fake_st.infos
    )
    assert fake_st.tables == []
