"""Tests for the SCAN-004 scan-history page's pure helpers in app.py.

What this file proves
---------------------
The history page splits Streamlit rendering from data shaping. The shaping
helpers tested here are pure functions: widget values in, repository kwargs or
display tables out. That lets these tests run without a browser, a database, or
a Streamlit runtime — the same approach ``tests/test_app_orchestration.py``
uses for the scanner flow.

Beginner note:
``import app`` works outside Streamlit because app.py only *renders* when run
through ``streamlit run``; importing it just defines functions.
"""

from __future__ import annotations

import datetime as dt

import app
from backend.storage.models import ScanRun, ScanStatus


# ---------------------------------------------------------------------------
# _history_filter_kwargs: widget values -> repository keyword filters
# ---------------------------------------------------------------------------


def test_filter_kwargs_empty_widgets_mean_no_filters():
    """The default page state ("All", no dates, blank symbol) filters nothing."""
    assert app._history_filter_kwargs("All", "All", "All", (), "All", "") == {}
    assert app._history_filter_kwargs(None, None, None, None, None, None) == {}


def test_filter_kwargs_maps_each_widget_to_its_repository_filter():
    """Each populated widget becomes exactly one repository kwarg."""
    kwargs = app._history_filter_kwargs(
        "envelope",
        "nifty_500",
        "failed",
        (dt.date(2026, 6, 1), dt.date(2026, 6, 5)),
        "job:daily_scan",
        "  reliance  ",
    )
    assert kwargs == {
        "screener_key": "envelope",
        "universe_key": "nifty_500",
        "status": ScanStatus.FAILED,
        "started_from": dt.date(2026, 6, 1),
        "started_to": dt.date(2026, 6, 5),
        "triggered_by": "job:daily_scan",
        # Whitespace is stripped; case is left to the repository's
        # case-insensitive comparison.
        "symbol": "reliance",
    }


def test_filter_kwargs_handles_partial_date_range():
    """st.date_input yields a 1-item tuple mid-selection; that means from-only."""
    kwargs = app._history_filter_kwargs(
        "All", "All", "All", (dt.date(2026, 6, 1),), "All", ""
    )
    assert kwargs == {"started_from": dt.date(2026, 6, 1)}


def test_filter_signature_changes_for_every_history_filter():
    """Changing any filter must mint a fresh table-selection widget key."""
    baseline = app._history_filter_signature("All", "All", "All", (), "All", "")
    variants = [
        app._history_filter_signature("envelope", "All", "All", (), "All", ""),
        app._history_filter_signature("All", "nifty_500", "All", (), "All", ""),
        app._history_filter_signature("All", "All", "failed", (), "All", ""),
        app._history_filter_signature(
            "All", "All", "All", (dt.date(2026, 6, 1),), "All", ""
        ),
        app._history_filter_signature(
            "All", "All", "All", (), "job:daily_scan", ""
        ),
        app._history_filter_signature("All", "All", "All", (), "All", "RELIANCE"),
    ]
    assert all(signature != baseline for signature in variants)
    assert len(set(variants)) == len(variants)


# ---------------------------------------------------------------------------
# Timestamp / duration formatting
# ---------------------------------------------------------------------------


def test_format_utc_timestamp_treats_naive_values_as_utc():
    """SQLite returns naive UTC datetimes; they must not be shifted to local time."""
    naive = dt.datetime(2026, 6, 10, 9, 30)
    assert app._format_utc_timestamp(naive) == "2026-06-10 09:30 UTC"


def test_format_utc_timestamp_converts_aware_values_to_utc():
    """Postgres returns aware datetimes; other zones convert into UTC."""
    ist = dt.timezone(dt.timedelta(hours=5, minutes=30))
    aware = dt.datetime(2026, 6, 10, 15, 0, tzinfo=ist)
    assert app._format_utc_timestamp(aware) == "2026-06-10 09:30 UTC"


def test_format_utc_timestamp_handles_missing_value():
    assert app._format_utc_timestamp(None) == "—"


def test_format_run_duration_handles_running_and_finished_runs():
    """No finished_at means the run is still going; otherwise show s/m units."""
    start = dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC)
    assert app._format_run_duration(start, None) == "still running"
    assert app._format_run_duration(start, start + dt.timedelta(seconds=42)) == "42s"
    assert app._format_run_duration(start, start + dt.timedelta(minutes=3)) == "3.0m"


# ---------------------------------------------------------------------------
# _history_run_row + _history_runs_frame: ORM rows -> display table
# ---------------------------------------------------------------------------


def _make_run(**overrides) -> ScanRun:
    """Build an unmapped ScanRun instance for pure display tests.

    Constructing the ORM class directly (without a session) is enough here
    because the row builder only reads scalar attributes.
    """
    defaults = dict(
        id=7,
        started_at=dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 10, 9, 1, tzinfo=dt.UTC),
        status=ScanStatus.SUCCESS,
        screener_key="envelope",
        universe_key="nifty_500",
        symbols_scanned=500,
        triggered_by="job:daily_scan",
        error_message=None,
    )
    defaults.update(overrides)
    run = ScanRun()
    for key, value in defaults.items():
        setattr(run, key, value)
    return run


def test_history_run_row_captures_every_page_column():
    """The plain dict carries everything the page renders later."""
    row = app._history_run_row(_make_run(), shortlisted=5)

    assert row == {
        "run_id": 7,
        "started": "2026-06-10 09:00 UTC",
        "finished": "2026-06-10 09:01 UTC",
        # Exactly one minute crosses the seconds/minutes display boundary.
        "duration": "1.0m",
        "screener": "envelope",
        "universe": "nifty_500",
        "status": "success",
        "symbols_scanned": 500,
        "shortlisted": 5,
        "triggered_by": "job:daily_scan",
        "error_message": "",
    }


def test_history_runs_frame_formats_legacy_and_failed_rows():
    """Pre-SCAN-004 rows show an em-dash; failures show a badge + error preview."""
    rows = [
        app._history_run_row(_make_run(), shortlisted=5),
        app._history_run_row(
            _make_run(
                id=8,
                status=ScanStatus.FAILED,
                symbols_scanned=None,  # recorded before the column existed
                error_message="The screener raised RuntimeError " + "x" * 100,
            ),
            shortlisted=0,
        ),
    ]

    frame = app._history_runs_frame(rows, error_redactor=lambda text: text)

    assert list(frame.columns) == [
        "Started",
        "Finished",
        "Screener",
        "Universe",
        "Status",
        "Symbols scanned",
        "Shortlisted",
        "Triggered by",
        "Error",
    ]
    ok_row, failed_row = frame.iloc[0], frame.iloc[1]
    assert ok_row["Status"].endswith("SUCCESS")
    assert ok_row["Symbols scanned"] == "500"
    assert ok_row["Error"] == ""
    # AC: failed runs are visible and understandable at a glance.
    assert failed_row["Status"].endswith("FAILED")
    assert failed_row["Symbols scanned"] == "—"
    assert failed_row["Error"].startswith("The screener raised RuntimeError")
    # Long messages are previewed in the table; the details view shows them fully.
    assert len(failed_row["Error"]) <= app._HISTORY_ERROR_PREVIEW_CHARS + 1
    assert failed_row["Error"].endswith("…")


def test_history_error_is_redacted_before_preview_truncation():
    """A long bare secret must not leak a prefix when the preview is shortened."""
    secret = "S" * (app._HISTORY_ERROR_PREVIEW_CHARS + 30)
    rows = [
        app._history_run_row(
            _make_run(
                status=ScanStatus.FAILED,
                error_message=f"Provider returned secret {secret}",
            ),
            shortlisted=0,
        )
    ]
    redactor_inputs: list[str] = []

    def exact_value_redactor(text: str) -> str:
        redactor_inputs.append(text)
        return text.replace(secret, "***REDACTED***")

    frame = app._history_runs_frame(rows, error_redactor=exact_value_redactor)

    assert redactor_inputs == [f"Provider returned secret {secret}"]
    assert secret[: app._HISTORY_ERROR_PREVIEW_CHARS] not in frame.iloc[0]["Error"]
    assert "***REDACTED***" in frame.iloc[0]["Error"]
