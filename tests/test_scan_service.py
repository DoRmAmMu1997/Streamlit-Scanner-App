"""Tests for the SCAN-003 scan service (backend/scanning/service.py).

The service runs a screener and persists the run + results. These tests inject a
``session_factory`` bound to a temporary in-memory SQLite database and use tiny
fake screeners/loaders, so they never touch Streamlit, Dhan, or the real
``data/scanner.db``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import date

import pandas as pd
from sqlalchemy.orm import Session

from backend.observability import (
    EVENT_SCAN_COMPLETED,
    EVENT_SCAN_FAILED,
    EVENT_SCAN_PARTIAL,
    EVENT_SCAN_STARTED,
    EVENT_SYMBOL_SCAN_FAILED,
)
from backend.scanning import ScanStatus, run_scan
from backend.storage.repository import get_latest_scan_runs, get_scan_results

# The ``db_engine`` and ``session_factory`` fixtures these tests use live in
# tests/conftest.py, shared with the other scan-history test modules.


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
    """A successful scan persists normalized copies but returns legacy UI data.

    This checks both sides of the service boundary. The caller receives the
    original DataFrame shape, while a fresh database session sees the canonical
    provenance generated immediately before persistence.
    """
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
    # PROV-001A normalizes only copies sent to persistence. Streamlit must keep
    # receiving the exact legacy DataFrame produced by the screener.
    assert "provenance_json" not in result.results.columns
    assert list(result.results.columns) == [
        "symbol",
        "rating",
        "signal_date",
        "close",
        "reason",
    ]

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

        # This screener supplied no provenance, so the service fills a small,
        # predictable envelope from run-level context. ``source`` stays None
        # because orchestration cannot know whether arbitrary legacy logic was
        # deterministic, AI-assisted, or hybrid.
        assert rows[0].provenance_json == {
            "screener_key": "envelope",
            "screener_version": None,
            "triggered_rules": [],
            "indicator_values": {},
            "params_snapshot": {
                "start_date": "2016-06-02",
                "end_date": "2026-06-02",
                "period": 20,
            },
            "data_snapshot_date": "2026-06-02",
            "source": None,
            "notes": None,
            "ai": None,
        }
        # The repository's raw audit copy receives the normalized persistence
        # row, while the in-memory DataFrame above remains untouched.
        assert rows[0].raw_result_json["provenance_json"] == rows[0].provenance_json


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


def test_run_scan_persists_good_rows_when_one_row_breaks_the_contract(
    db_engine, session_factory, caplog
):
    """One malformed row must not erase scan history for the whole run.

    A screener bug (for example a bad merge/reindex) can produce a single row
    with a NaN symbol. That row can never be persisted under the PROV-001A
    contract, but the other rows are perfectly good audit data. The service
    skips the unusable row with a warning instead of failing the entire
    ``save_scan_results`` write, matching the per-symbol failure philosophy
    used by the scanner and the data loader.
    """

    def screener_run(_universe_df, _data_loader, _params):
        frame = _two_buy_rows()
        bad_row = {
            "symbol": float("nan"),
            "rating": "BUY",
            "signal_date": "2026-06-01",
            "close": 10.0,
            "reason": "merge artifact",
        }
        return pd.concat([frame, pd.DataFrame([bad_row])], ignore_index=True)

    with caplog.at_level(logging.WARNING, logger="backend.scanning.service"):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            run_callable=screener_run,
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS", "BAD"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=session_factory,
        )

    # The in-memory DataFrame for Streamlit keeps all three rows; only the
    # persistence copies are filtered.
    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is not None
    assert len(result.results) == 3

    with Session(db_engine) as session:
        rows = get_scan_results(session, result.run_id)
        assert [r.symbol for r in rows] == ["RELIANCE", "TCS"]
    assert any("skipping" in record.message.lower() for record in caplog.records)


def test_run_scan_treats_every_row_failing_the_contract_as_persistence_failure(
    db_engine, session_factory
):
    """If no row at all can satisfy the contract, the failure must stay loud.

    Recording an empty-but-successful run would be misleading audit data: the
    screener clearly produced results, yet history would say nothing happened.
    A systemic contract failure therefore still follows the existing
    persistence-failure path (run marked FAILED, no result rows stored).
    """

    def screener_run(_universe_df, _data_loader, _params):
        return pd.DataFrame([{"symbol": float("nan"), "rating": "BUY"}])

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["BAD"]}),
        data_loader=_FakeLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    # The caller still gets the in-memory results despite the history failure.
    assert len(result.results) == 1
    with Session(db_engine) as session:
        runs = get_latest_scan_runs(session)
        assert len(runs) == 1
        assert runs[0].status is ScanStatus.FAILED
        assert get_scan_results(session, runs[0].id) == []


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


# ---------------------------------------------------------------------------
# OBS-001 structured events
# ---------------------------------------------------------------------------


def _event_fields(caplog, event_name: str) -> list[dict]:
    """Return the structured-fields dict for each captured log_event of this name.

    ``log_event`` attaches the event name and its fields to the LogRecord, so the
    test can read them straight off ``caplog.records`` without parsing text.
    """
    return [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == event_name
    ]


def test_run_scan_emits_started_and_completed_events_after_persistence(
    db_engine, session_factory, caplog, monkeypatch
):
    """A successful terminal event should describe an already-finished DB row.

    This ordering matters in production: a log consumer may react immediately
    to ``scan_completed``. Emitting it while the durable row still says RUNNING
    would tell operators and automation two contradictory stories.
    """
    from backend.scanning import service

    def screener_run(_universe_df, _data_loader, _params):
        return _two_buy_rows()

    statuses_when_completed: list[ScanStatus] = []
    real_log_event = service.log_event

    def observing_log_event(logger, event_name, **fields):
        if event_name == EVENT_SCAN_COMPLETED:
            with Session(db_engine) as session:
                run = session.get(
                    service.ScanRun,
                    fields["run_id"],
                )
                assert run is not None
                statuses_when_completed.append(run.status)
        real_log_event(logger, event_name, **fields)

    monkeypatch.setattr(service, "log_event", observing_log_event)

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            scan_name="Daily Envelope",
            run_callable=screener_run,
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=session_factory,
        )

    started = _event_fields(caplog, EVENT_SCAN_STARTED)
    completed = _event_fields(caplog, EVENT_SCAN_COMPLETED)
    assert len(started) == 1
    assert started[0]["run_id"] == result.run_id
    assert started[0]["screener_key"] == "envelope"
    assert started[0]["scan_name"] == "Daily Envelope"
    assert started[0]["symbols_scanned"] == 2
    assert len(completed) == 1
    assert completed[0]["run_id"] == result.run_id
    assert completed[0]["status"] == "success"
    assert completed[0]["results_count"] == 2
    assert completed[0]["scan_name"] == "Daily Envelope"
    assert "duration_seconds" in completed[0]
    assert statuses_when_completed == [ScanStatus.SUCCESS]


def test_run_scan_emits_scan_partial_instead_of_scan_completed(
    session_factory, caplog
):
    """Usable rows with symbol failures get their own terminal event."""

    def screener_run(_universe_df, _data_loader, params):
        params["compute_failure_callback"](
            {"symbol": "TCS", "message": "bad candles"}
        )
        return _two_buy_rows()

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            scan_name="Partial Envelope",
            run_callable=screener_run,
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=session_factory,
        )

    partial = _event_fields(caplog, EVENT_SCAN_PARTIAL)
    assert result.status is ScanStatus.PARTIAL
    assert len(partial) == 1
    assert partial[0]["run_id"] == result.run_id
    assert partial[0]["scan_name"] == "Partial Envelope"
    assert partial[0]["results_count"] == 2
    assert _event_fields(caplog, EVENT_SCAN_COMPLETED) == []


def test_run_scan_emits_scan_failed_event_when_screener_raises(session_factory, caplog):
    """A screener exception emits scan_failed (type only) and no scan_completed."""

    def boom(_universe_df, _data_loader, _params):
        raise RuntimeError("token=NOPE should not appear in any field")

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            run_callable=boom,
            universe_df=pd.DataFrame({"symbol": ["RELIANCE"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=session_factory,
        )

    failed = _event_fields(caplog, EVENT_SCAN_FAILED)
    assert len(failed) == 1
    assert failed[0]["run_id"] == result.run_id
    assert failed[0]["error_type"] == "RuntimeError"
    assert failed[0]["phase"] == "screener"
    # A failed run reports scan_failed instead of scan_completed.
    assert _event_fields(caplog, EVENT_SCAN_COMPLETED) == []


def test_run_scan_emits_symbol_scan_failed_per_failed_symbol(session_factory, caplog):
    """Loader + compute failures each emit symbol_scan_failed with symbol + run_id."""

    def screener_run(_universe_df, _data_loader, params):
        params["compute_failure_callback"]({"symbol": "TCS", "message": "bad candles"})
        return _two_buy_rows()

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            run_callable=screener_run,
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS", "WIPRO"]}),
            data_loader=_FakeLoader(
                last_failures=[{"symbol": "WIPRO", "message": "timeout"}]
            ),
            params=_base_params(),
            session_factory=session_factory,
        )

    symbol_events = _event_fields(caplog, EVENT_SYMBOL_SCAN_FAILED)
    assert {event["symbol"] for event in symbol_events} == {"WIPRO", "TCS"}
    assert all(event["run_id"] == result.run_id for event in symbol_events)


def test_run_scan_emits_persistence_failure_without_false_completion(
    session_factory, caplog
):
    """A database write failure must not be logged as a completed scan.

    The first transaction creates the RUNNING header. The second transaction is
    deliberately broken while saving results, and later transactions use the
    real factory so the service can perform its best-effort FAILED stamp.
    """
    calls = 0

    @contextmanager
    def fail_result_persistence():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("token=DATABASESECRET should stay hidden")
        with session_factory() as session:
            yield session

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            run_callable=lambda *_args: _two_buy_rows(),
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=fail_result_persistence,
        )

    failed = _event_fields(caplog, EVENT_SCAN_FAILED)
    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is not None
    assert len(failed) == 1
    assert failed[0]["phase"] == "persistence"
    assert failed[0]["error_type"] == "RuntimeError"
    assert "DATABASESECRET" not in str(failed[0])
    assert _event_fields(caplog, EVENT_SCAN_COMPLETED) == []
    assert _event_fields(caplog, EVENT_SCAN_PARTIAL) == []


def test_run_scan_emits_header_failure_without_false_completion(caplog):
    """A scan may still return rows when the audit header cannot be created."""

    @contextmanager
    def broken_factory():
        raise RuntimeError("token=HEADERSECRET should stay hidden")
        yield  # pragma: no cover - documents the context-manager shape

    with caplog.at_level(logging.INFO):
        result = run_scan(
            screener_key="envelope",
            universe_key="hemant_super_45",
            run_callable=lambda *_args: _two_buy_rows(),
            universe_df=pd.DataFrame({"symbol": ["RELIANCE", "TCS"]}),
            data_loader=_FakeLoader(),
            params=_base_params(),
            session_factory=broken_factory,
        )

    failed = _event_fields(caplog, EVENT_SCAN_FAILED)
    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is None
    assert len(failed) == 1
    assert failed[0]["phase"] == "create_header"
    assert failed[0]["error_type"] == "RuntimeError"
    assert "HEADERSECRET" not in str(failed[0])
    assert _event_fields(caplog, EVENT_SCAN_COMPLETED) == []
