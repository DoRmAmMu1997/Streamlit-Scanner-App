"""Tests for the SCAN-002 scan-history repository helpers.

The repository is the public write/read API that SCAN-003 will call. These tests
use a temporary SQLite database and real ORM objects so they exercise the same
mapping code the app will use, without needing Streamlit or Dhan data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.storage.models import Base, ScanStatus


@pytest.fixture
def session():
    """Yield a clean in-memory SQLite session for each repository test."""
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        # Match the production SQLite engine behavior so relationship and raw
        # database cascades are both available in tests.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as active_session:
        yield active_session
    engine.dispose()


def test_repository_creates_run_results_and_failed_status(session):
    """A caller can create a run, store rows, and mark the run failed."""
    from backend.storage.repository import (
        create_scan_run,
        finish_scan_run,
        get_latest_scan_runs,
        get_scan_results,
        save_scan_results,
    )

    # The run header captures the facts needed to explain or replay the scan:
    # which screener, which universe, which params, which data date, and who
    # triggered it.
    run = create_scan_run(
        session,
        screener_key="technical_analysis_ai",
        universe_key="hemant_good_200",
        params={"max_symbols": 50, "as_of": dt.date(2026, 6, 4)},
        data_snapshot_date=dt.date(2026, 6, 3),
        app_version="0.2.0",
        git_commit_sha="a3ecc2e241442a0bffd3587843f414a2cfb3a01b",
        triggered_by="ui:hemant@example.com",
    )

    # Use one deterministic-style row and one AI-style row. This proves the same
    # table can keep typed columns plus the full flexible raw JSON payload.
    results = save_scan_results(
        session,
        run,
        [
            {
                "symbol": "RELIANCE",
                "signal_date": "2026-06-03",
                "close": Decimal("1234.5678"),
                "rating": "BUY",
                "reason": "close below lower envelope",
                "extra": {"threshold": Decimal("0.07")},
            },
            {
                "symbol": "TCS",
                "signal_date": dt.datetime(2026, 6, 3, 15, 30, tzinfo=dt.UTC),
                "close_price": "3890.00",
                "final_score": Decimal("82.50"),
                "rating": "STRONG BUY",
                "reason": "agent confirmed structure",
                "provenance": {
                    "model": "claude-sonnet-4-6",
                    "checked_at": dt.datetime(2026, 6, 4, 10, 0, tzinfo=dt.UTC),
                },
            },
        ],
    )
    # A failed scan can still have partial rows. SCAN-004 will show the error
    # message beside the rows that were saved before failure.
    finish_scan_run(
        session,
        run,
        status=ScanStatus.FAILED,
        error_message="Dhan rate limit stopped the scan",
    )
    session.commit()

    assert run.id is not None
    assert run.status is ScanStatus.FAILED
    assert run.error_message == "Dhan rate limit stopped the scan"
    assert run.finished_at is not None
    assert [result.symbol for result in results] == ["RELIANCE", "TCS"]

    latest = get_latest_scan_runs(session)
    assert [loaded.id for loaded in latest] == [run.id]

    by_symbol = {result.symbol: result for result in get_scan_results(session, run.id)}
    # Typed money columns keep Decimal precision for querying and display.
    assert by_symbol["RELIANCE"].close_price == Decimal("1234.5678")
    # JSON snapshots store Decimal values as strings so the audit copy is
    # lossless and JSON-serializable.
    assert by_symbol["RELIANCE"].raw_result_json["close"] == "1234.5678"
    assert by_symbol["RELIANCE"].raw_result_json["extra"]["threshold"] == "0.07"
    assert by_symbol["TCS"].close_price == Decimal("3890.0000")
    assert by_symbol["TCS"].final_score == Decimal("82.50")
    assert by_symbol["TCS"].provenance_json == {
        "model": "claude-sonnet-4-6",
        "checked_at": "2026-06-04T10:00:00+00:00",
    }


def test_repository_orders_latest_runs_newest_first(session):
    """History queries should show the most recent run first."""
    from backend.storage.repository import create_scan_run, get_latest_scan_runs

    first = create_scan_run(session, screener_key="envelope", universe_key="nifty_500")
    second = create_scan_run(session, screener_key="knoxville", universe_key="nifty_100")
    first.started_at = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    second.started_at = dt.datetime(2026, 6, 2, tzinfo=dt.UTC)
    session.commit()

    assert [run.screener_key for run in get_latest_scan_runs(session, limit=1)] == [
        "knoxville"
    ]
