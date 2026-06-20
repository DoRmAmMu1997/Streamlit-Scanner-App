"""Tests for the JOB-003 latest-vs-previous scan comparison read model."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from backend.storage.models import ScanStatus


def _seed_run(
    db_session,
    *,
    started_at: dt.datetime,
    rows: list[dict],
    screener_key: str = "envelope",
    universe_key: str = "nifty_500",
    status: ScanStatus = ScanStatus.SUCCESS,
):
    """Create one finalized scan run with persisted result rows."""
    from backend.storage.repository import create_scan_run, save_scan_results

    run = create_scan_run(
        db_session,
        screener_key=screener_key,
        universe_key=universe_key,
        symbols_scanned=500,
    )
    run.status = status
    run.started_at = started_at
    run.finished_at = started_at + dt.timedelta(minutes=1)
    save_scan_results(db_session, run, rows)
    db_session.commit()
    return run


def test_build_scan_comparison_classifies_new_repeated_and_dropped_symbols(db_session):
    """The latest shortlist is compared against the immediately previous shortlist."""
    from backend.scanning.comparison import build_scan_comparison

    previous = _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 19, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "RELIANCE", "rating": "BUY", "final_score": Decimal("70.00")},
            {"symbol": "TCS", "rating": "BUY", "final_score": Decimal("80.00")},
        ],
    )
    latest = _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "TCS", "rating": "BUY", "final_score": Decimal("82.00")},
            {"symbol": "INFY", "rating": "BUY", "final_score": Decimal("65.00")},
        ],
    )

    comparison = build_scan_comparison(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
    )

    assert comparison.latest_run.run_id == latest.id
    assert comparison.previous_run is not None
    assert comparison.previous_run.run_id == previous.id
    assert [row.symbol for row in comparison.new_today] == ["INFY"]
    assert [row.symbol for row in comparison.repeated_from_yesterday] == ["TCS"]
    assert [row.symbol for row in comparison.dropped_today] == ["RELIANCE"]


def test_build_scan_comparison_uses_score_fallback_and_source_match(db_session):
    """Scores use final_score first, then confidence, and compare only matching sources."""
    from backend.scanning.comparison import build_scan_comparison

    _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 19, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "FINAL", "final_score": Decimal("40.00")},
            {"symbol": "CONF", "confidence": 4},
            {"symbol": "MISMATCH", "confidence": 6},
            {"symbol": "BADCONF", "confidence": "NaN"},
            {"symbol": "DOWN", "final_score": Decimal("90.00")},
        ],
    )
    _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "FINAL", "final_score": Decimal("45.50")},
            {"symbol": "CONF", "confidence": "7"},
            {"symbol": "MISMATCH", "final_score": Decimal("8.00")},
            {"symbol": "BADCONF", "confidence": 9},
            {"symbol": "DOWN", "final_score": Decimal("84.25")},
        ],
    )

    comparison = build_scan_comparison(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
    )

    improved = {row.symbol: row for row in comparison.improved_score}
    degraded = {row.symbol: row for row in comparison.degraded_score}
    assert improved["FINAL"].score_source == "final_score"
    assert improved["FINAL"].score_delta == Decimal("5.50")
    assert improved["CONF"].score_source == "confidence"
    assert improved["CONF"].score_delta == Decimal("3")
    assert degraded["DOWN"].score_delta == Decimal("-5.75")
    assert "BADCONF" not in improved
    assert "BADCONF" not in degraded
    assert "MISMATCH" not in improved
    assert "MISMATCH" not in degraded


def test_scan_comparison_export_frame_has_stable_section_rows(db_session):
    """CSV export flattens all sections with a change-type column."""
    from backend.scanning.comparison import build_scan_comparison

    previous = _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 19, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "OLD", "rating": "BUY", "reason": "=danger"},
            {"symbol": "KEEP", "final_score": Decimal("2.00")},
        ],
    )
    latest = _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC),
        rows=[
            {"symbol": "KEEP", "final_score": Decimal("3.00")},
            {"symbol": "NEW", "rating": "BUY", "reason": "fresh"},
        ],
    )

    frame = build_scan_comparison(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
    ).to_export_frame()

    assert list(frame.columns) == [
        "Change type",
        "Symbol",
        "Latest run",
        "Previous run",
        "Latest rating",
        "Previous rating",
        "Latest signal date",
        "Previous signal date",
        "Latest close",
        "Previous close",
        "Latest score",
        "Previous score",
        "Score source",
        "Score delta",
        "Latest reason",
        "Previous reason",
    ]
    assert list(frame["Change type"]) == [
        "New today",
        "Repeated from yesterday",
        "Dropped today",
        "Improved score",
    ]
    assert list(frame["Symbol"]) == ["NEW", "KEEP", "OLD", "KEEP"]
    assert frame.iloc[0]["Latest run"] == latest.id
    assert pd.isna(frame.iloc[0]["Previous run"])
    assert pd.isna(frame.iloc[2]["Latest run"])
    assert frame.iloc[2]["Previous run"] == previous.id


def test_build_scan_comparison_handles_missing_previous_run(db_session):
    """One finalized run can render latest context but has no comparison sections."""
    from backend.scanning.comparison import build_scan_comparison

    latest = _seed_run(
        db_session,
        started_at=dt.datetime(2026, 6, 20, 9, 0, tzinfo=dt.UTC),
        rows=[{"symbol": "RELIANCE", "final_score": Decimal("10.00")}],
    )

    comparison = build_scan_comparison(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
    )

    assert comparison.latest_run.run_id == latest.id
    assert comparison.previous_run is None
    assert comparison.new_today == ()
    assert comparison.to_export_frame().empty
