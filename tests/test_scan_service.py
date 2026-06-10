"""Tests for the SCAN-003 scan service (backend/scanning/service.py).

The service runs a screener and persists the run + results. These tests inject a
``session_factory`` bound to a temporary in-memory SQLite database and use tiny
fake screeners/loaders, so they never touch Streamlit, Dhan, or the real
``data/scanner.db``.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session

from backend.scanning import ScanStatus, run_scan
from backend.storage.models import Base
from backend.storage.repository import get_latest_scan_runs, get_scan_results


@pytest.fixture
def db_engine():
    """An in-memory SQLite engine with the scan-history tables created."""
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def session_factory(db_engine):
    """A transactional session factory bound to the in-memory engine.

    Mirrors ``backend.storage.database.session_scope`` (commit on success, roll
    back on error) but points at the test database instead of the real one.
    """

    @contextmanager
    def factory():
        with Session(db_engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return factory


class _FakeLoader:
    """Minimal data loader exposing only the ``last_failures`` the service reads."""

    def __init__(self, last_failures=None):
        self.last_failures = list(last_failures or [])


def _base_params() -> dict:
    return {"start_date": date(2016, 6, 2), "end_date": date(2026, 6, 2), "period": 20}


def _two_buy_rows() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "RELIANCE", "rating": "BUY", "signal_date": "2026-06-01",
             "close": 1234.5, "reason": "oversold bounce"},
            {"symbol": "TCS", "rating": "BUY", "signal_date": "2026-06-01",
             "close": 3890.0, "reason": "breakout"},
        ]
    )


# ---------------------------------------------------------------------------
# Successful run
# ---------------------------------------------------------------------------


def test_run_scan_success_persists_run_and_results(db_engine, session_factory):
    """A clean run is SUCCESS, returns its rows, and writes the run + results."""
    params = _base_params()
    # A callback in the caller's params must be stripped before it is persisted.
    params["progress_callback"] = lambda *_a: None

    def screener_run(_universe_df, _data_loader, _params):
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
        data_loader=_FakeLoader(),
        params=params,
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is not None
    assert list(result.results["symbol"]) == ["RELIANCE", "TCS"]

    # Re-query the database to prove the run + results were written.
    with Session(db_engine) as session:
        runs = get_latest_scan_runs(session)
        assert len(runs) == 1
        assert runs[0].status is ScanStatus.SUCCESS
        assert runs[0].screener_key == "envelope"
        assert runs[0].universe_key == "hemant_super_45"
        assert runs[0].data_snapshot_date == date(2026, 6, 2)
        assert runs[0].triggered_by == "ui"
        # Callables never reach the JSON params snapshot.
        assert "progress_callback" not in (runs[0].params_json or {})
        rows = get_scan_results(session, result.run_id)
        assert [r.symbol for r in rows] == ["RELIANCE", "TCS"]


def test_run_scan_creates_running_row_before_invoking_screener(
    db_engine, session_factory
):
    """The RUNNING audit row should exist while the screener is still executing.

    Beginner note:
    This is the regression test for the SCAN-003 review finding. The fake
    screener opens a brand-new session while ``run_scan`` is still inside
    ``run_callable``. If the service creates the header only after the screener
    returns, this inner query sees zero rows and the test fails.
    """

    def screener_run(_universe_df, _data_loader, _params):
        # This assertion runs inside the screener callback. A separate Session
        # proves the RUNNING row was committed, not merely added to an uncommitted
        # transaction that only this service can see.
        with Session(db_engine) as session:
            runs = get_latest_scan_runs(session)
            assert len(runs) == 1
            assert runs[0].status is ScanStatus.RUNNING
            assert runs[0].finished_at is None
            assert runs[0].started_at is not None
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is not None


# ---------------------------------------------------------------------------
# Failed run (screener raises)
# ---------------------------------------------------------------------------


def test_run_scan_records_failed_run_when_screener_raises(db_engine, session_factory):
    """A screener exception is caught, recorded FAILED, and never re-raised.

    The exception text intentionally contains a fake secret. The service may
    store the exception type (``RuntimeError``) because that helps operators, but
    it must never store the raw message because real broker/API exceptions can
    include credentials or request payloads.
    """

    def boom(_universe_df, _data_loader, _params):
        raise RuntimeError("token=SUPERSECRET should not be stored")

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=boom,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.FAILED
    assert result.results.empty
    # The stored/returned message must be secret-free (no raw exception text).
    assert "SUPERSECRET" not in (result.error_message or "")
    assert "RuntimeError" in (result.error_message or "")

    with Session(db_engine) as session:
        runs = get_latest_scan_runs(session)
        assert runs[0].status is ScanStatus.FAILED
        # started_at comes from the pre-scan RUNNING row; finished_at is added
        # after the exception is caught. Having both timestamps is what makes a
        # failed long-running scan auditable in the future history page.
        assert runs[0].started_at is not None
        assert runs[0].finished_at is not None
        assert runs[0].started_at <= runs[0].finished_at
        assert "SUPERSECRET" not in (runs[0].error_message or "")
        assert "RuntimeError" in (runs[0].error_message or "")
        assert get_scan_results(session, runs[0].id) == []


# ---------------------------------------------------------------------------
# Partial runs (some symbols failed)
# ---------------------------------------------------------------------------


def test_run_scan_marks_partial_on_loader_failure(db_engine, session_factory):
    """Usable rows + data-loader failures => PARTIAL (recorded as such)."""

    def screener_run(_universe_df, _data_loader, _params):
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS", "WIPRO"]}),
        data_loader=_FakeLoader(last_failures=[{"symbol": "WIPRO", "error": "timeout"}]),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.PARTIAL
    assert result.run_id is not None
    with Session(db_engine) as session:
        assert get_latest_scan_runs(session)[0].status is ScanStatus.PARTIAL


def test_run_scan_marks_partial_on_compute_failure(session_factory):
    """A per-symbol compute failure reported via the service callback => PARTIAL."""

    def screener_run(_universe_df, _data_loader, params):
        params["compute_failure_callback"]({"symbol": "TCS", "message": "bad candles"})
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.PARTIAL
    assert result.compute_failures == [{"symbol": "TCS", "message": "bad candles"}]


# ---------------------------------------------------------------------------
# Database resilience: persistence must never break the scan
# ---------------------------------------------------------------------------


def test_run_scan_returns_results_even_when_persistence_fails():
    """If the database is unavailable, the user still gets results (run_id None)."""

    @contextmanager
    def broken_factory():
        raise RuntimeError("database is unavailable")
        yield  # pragma: no cover - unreachable; documents the generator shape

    def screener_run(_universe_df, _data_loader, _params):
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=broken_factory,
    )

    assert result.status is ScanStatus.SUCCESS
    assert list(result.results["symbol"]) == ["RELIANCE", "TCS"]
    assert result.run_id is None


def test_run_scan_records_universe_size_as_symbols_scanned(db_engine, session_factory):
    """SCAN-004: the run header stores how many symbols were handed to the screener.

    The history page shows "symbols scanned" next to "shortlisted results" so a
    user can tell "2 hits out of 3 candidates" from "2 hits out of 500". The
    service is the only place that sees the universe frame, so it records the
    size when it creates the audit header.
    """

    def screener_run(_universe_df, _data_loader, _params):
        return _two_buy_rows()

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS", "WIPRO"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.run_id is not None
    with Session(db_engine) as session:
        runs = get_latest_scan_runs(session)
        assert runs[0].symbols_scanned == 3
