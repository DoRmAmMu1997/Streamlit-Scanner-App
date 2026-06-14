"""Tests for the SCAN-003 scan service (backend/scanning/service.py).

The service runs a screener and persists the run + results. These tests inject a
``session_factory`` bound to a temporary in-memory SQLite database and use tiny
fake screeners/loaders, so they never touch Streamlit, Dhan, or the real
``data/scanner.db``.
"""

from __future__ import annotations

import logging
from contextlib import contextmanager
from datetime import UTC, date, datetime
from decimal import Decimal

import pandas as pd
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from backend.observability import (
    EVENT_SCAN_COMPLETED,
    EVENT_SCAN_FAILED,
    EVENT_SCAN_PARTIAL,
    EVENT_SCAN_STARTED,
    EVENT_SYMBOL_SCAN_FAILED,
)
from backend.scanner_base import BaseScanner
from backend.scanning import ScanStatus, run_scan
from backend.scanning.result_contract import AIEvaluationRecord, AIProvenance
from backend.storage.repository import (
    get_ai_evaluations,
    get_latest_scan_runs,
    get_scan_results,
)

# The ``db_engine`` and ``session_factory`` fixtures these tests use live in
# tests/conftest.py, shared with the other scan-history test modules.


class _FakeLoader:
    """Minimal data loader exposing only the ``last_failures`` the service reads."""

    def __init__(self, last_failures=None):
        self.last_failures = list(last_failures or [])


def _base_params() -> dict:
    return {"start_date": date(2016, 6, 2), "end_date": date(2026, 6, 2), "period": 20}


def _two_buy_rows() -> pd.DataFrame:
    def provenance(rule: str, value: float) -> dict:
        return {
            "triggered_rules": [rule],
            "indicator_values": {"signal_value": value},
            "source": "deterministic",
        }

    return pd.DataFrame(
        [
            {"symbol": "RELIANCE", "rating": "BUY", "signal_date": "2026-06-01",
             "close": 1234.5, "reason": "oversold bounce",
             "provenance": provenance("oversold_bounce", 31.5)},
            {"symbol": "TCS", "rating": "BUY", "signal_date": "2026-06-01",
             "close": 3890.0, "reason": "breakout",
             "provenance": provenance("breakout", 1.2)},
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
        "provenance",
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

        assert rows[0].provenance_json == {
            "screener_key": "envelope",
            "screener_version": None,
            "triggered_rules": ["oversold_bounce"],
            "indicator_values": {"signal_value": 31.5},
            "params_snapshot": {
                "start_date": "2016-06-02",
                "end_date": "2026-06-02",
                "period": 20,
            },
            "data_snapshot_date": "2026-06-02",
            "source": "deterministic",
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


def test_base_scanner_skips_invalid_emitted_row_and_run_scan_is_partial(
    db_engine, session_factory
):
    class _MixedContractScanner(BaseScanner):
        SCREENER = {
            "key": "mixed_contract",
            "name": "Mixed Contract",
            "description": "Test scanner",
            "universe": "test",
            "timeframe": "daily",
            "lookback_days": 1,
            "default_params": {},
        }

        def compute_signal(self, symbol, candles, params):
            indicator_values = {"close": 10.0}
            if symbol == "BAD":
                indicator_values = {"api_key": ["secret-value"]}
            return {
                "symbol": symbol,
                "rating": "BUY",
                "signal_date": pd.Timestamp("2026-06-01"),
                "close": 10.0,
                "reason": "valid signal",
                "provenance": {
                    "triggered_rules": ["rule"],
                    "indicator_values": indicator_values,
                    "source": "deterministic",
                },
            }

    class _BatchLoader(_FakeLoader):
        def load_universe_history(self, **_kwargs):
            return type(
                "_Batch",
                (),
                {
                    "frames": {
                        "GOOD": pd.DataFrame({"close": [10.0]}),
                        "BAD": pd.DataFrame({"close": [10.0]}),
                    }
                },
            )()

    scanner = _MixedContractScanner()
    result = run_scan(
        screener_key="mixed_contract",
        universe_key="test",
        run_callable=scanner.run,
        universe_df=pd.DataFrame({"symbol": ["GOOD", "BAD"]}),
        data_loader=_BatchLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.PARTIAL
    assert result.results["symbol"].tolist() == ["GOOD"]
    assert len(result.compute_failures) == 1
    assert result.rejected_result_rows == 1
    assert result.compute_failures[0]["symbol"] == "BAD"
    assert "secret-value" not in result.compute_failures[0]["message"]
    with Session(db_engine) as session:
        assert get_latest_scan_runs(session)[0].status is ScanStatus.PARTIAL


def test_base_scanner_marks_run_failed_when_every_emitted_row_is_invalid(
    session_factory,
):
    class _InvalidContractScanner(BaseScanner):
        SCREENER = {
            "key": "invalid_contract",
            "name": "Invalid Contract",
            "description": "Test scanner",
            "universe": "test",
            "timeframe": "daily",
            "lookback_days": 1,
            "default_params": {},
        }

        def compute_signal(self, symbol, candles, params):
            return {
                "symbol": symbol,
                "rating": "BUY",
                "signal_date": "2026-06-01",
                "close": 10.0,
                "reason": "invalid signal",
                "provenance": {
                    "triggered_rules": ["rule"],
                    "indicator_values": {"nested": [1, 2]},
                    "source": "deterministic",
                },
            }

    class _BatchLoader(_FakeLoader):
        def load_universe_history(self, **_kwargs):
            return type(
                "_Batch",
                (),
                {"frames": {"BAD": pd.DataFrame({"close": [10.0]})}},
            )()

    scanner = _InvalidContractScanner()
    result = run_scan(
        screener_key="invalid_contract",
        universe_key="test",
        run_callable=scanner.run,
        universe_df=pd.DataFrame({"symbol": ["BAD"]}),
        data_loader=_BatchLoader(),
        params=_base_params(),
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.FAILED
    assert result.results.empty
    assert result.rejected_result_rows == 1


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
    assert result.status is ScanStatus.PARTIAL
    assert result.rejected_result_rows == 1
    assert result.run_id is not None
    assert len(result.results) == 3

    with Session(db_engine) as session:
        rows = get_scan_results(session, result.run_id)
        assert [r.symbol for r in rows] == ["RELIANCE", "TCS"]
        assert get_latest_scan_runs(session)[0].status is ScanStatus.PARTIAL
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
    assert result.status is ScanStatus.FAILED
    assert result.rejected_result_rows == 1
    with Session(db_engine) as session:
        runs = get_latest_scan_runs(session)
        assert len(runs) == 1
        assert runs[0].status is ScanStatus.FAILED
        assert get_scan_results(session, runs[0].id) == []


def test_run_scan_collects_and_atomically_persists_ai_evaluation_callbacks(
    db_engine, session_factory
):
    def screener_run(_universe_df, _data_loader, params):
        params["ai_evaluation_callback"](
            AIEvaluationRecord(
                symbol="RELIANCE",
                signal_date=date(2026, 6, 1),
                outcome="approved",
                verdict="BUY",
                confidence=Decimal("0.91"),
                decision_reason="token=secret-reason",
                provenance=AIProvenance(
                    model_name="gpt-test",
                    prompt_version="v1",
                    prompt_sha256="b" * 64,
                    generated_at=datetime(2026, 6, 13, 8, 0, tzinfo=UTC),
                    cache_hit=False,
                    evidence_references=[],
                ),
                validated_verdict_json={"rating": "BUY"},
            )
        )
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
    with Session(db_engine) as session:
        evaluations = get_ai_evaluations(session, result.run_id)
        assert len(evaluations) == 1
        assert evaluations[0].symbol == "RELIANCE"
        assert evaluations[0].verdict_label == "BUY"
        assert evaluations[0].confidence == Decimal("0.9100")
        assert "secret-reason" not in str(evaluations[0].validated_verdict_json)


def test_invalid_ai_evaluation_rolls_back_shortlist_rows_and_fails_run(
    db_engine, session_factory
):
    def screener_run(_universe_df, _data_loader, params):
        params["ai_evaluation_callback"](
            {
                "symbol": "RELIANCE",
                "outcome": "maybe",
                "verdict": "BUY",
                "confidence": "0.9",
                "provenance": {
                    "model_name": "gpt-test",
                    "prompt_version": "v1",
                    "prompt_sha256": "b" * 64,
                    "generated_at": "2026-06-13T08:00:00+00:00",
                    "cache_hit": False,
                    "evidence_references": [],
                },
            }
        )
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

    assert result.status is ScanStatus.FAILED
    with Session(db_engine) as session:
        assert get_scan_results(session, result.run_id) == []
        assert get_ai_evaluations(session, result.run_id) == []
        assert get_latest_scan_runs(session)[0].status is ScanStatus.FAILED


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
            raise OperationalError(
                "INSERT",
                {},
                RuntimeError("token=DATABASESECRET should stay hidden"),
            )
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
    assert result.status is ScanStatus.FAILED
    assert result.run_id is not None
    assert result.error_message == (
        "Could not persist scan results (OperationalError)."
    )
    assert len(failed) == 1
    assert failed[0]["phase"] == "persistence"
    assert failed[0]["error_type"] == "OperationalError"
    assert failed[0]["status"] == "failed"
    assert failed[0]["persisted_results_count"] == 0
    assert failed[0]["ai_evaluation_count"] == 0
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


class _ProvenanceScanner(BaseScanner):
    """A minimal screener that emits PROV-002 provenance via the base helper."""

    SCREENER = {
        "key": "envelope",
        "name": "Provenance Demo",
        "description": "Test-only screener that records provenance.",
        "universe": "test",
        "timeframe": "daily",
        "lookback_days": 10,
        "default_params": {},
    }

    def compute_signal(self, symbol, candles, params):
        return {
            "symbol": symbol,
            "rating": "BUY",
            "signal_date": "2026-06-01",
            "close": 80.0,
            "reason": "at/below the lower envelope band",
            "provenance": self.build_provenance(
                triggered_rules=["close_at_or_below_lower_envelope_band"],
                indicator_values={"close": 80.0, "env_lower": 82.5},
            ),
        }


def test_run_scan_persists_screener_provenance_end_to_end(db_engine, session_factory):
    """A screener's provenance must reach scan_results.provenance_json intact.

    This is the whole PROV-002 contract through the real service + repository: a
    row carrying a ``build_provenance`` dict is normalized and stored as the
    canonical envelope, while the UI DataFrame keeps the raw provenance column.
    """
    scanner = _ProvenanceScanner()

    def screener_run(_universe_df, _data_loader, _params):
        # Build the frame exactly as BaseScanner.run would (one compute_signal row
        # selected down to result_columns), without needing a candle-backed loader.
        row = scanner.compute_signal("DISCOUNT", pd.DataFrame(), _params)
        return pd.DataFrame([row], columns=scanner.result_columns)

    result = run_scan(
        screener_key="envelope",
        universe_key="hemant_super_45",
        run_callable=screener_run,
        universe_df=pd.DataFrame({"symbol": ["DISCOUNT"]}),
        data_loader=_FakeLoader(),
        params={**_base_params(), "max_symbols": None},
        session_factory=session_factory,
    )

    assert result.status is ScanStatus.SUCCESS
    # The UI frame keeps the raw provenance column (only the display path drops it).
    assert "provenance" in result.results.columns

    with Session(db_engine) as session:
        rows = get_scan_results(session, result.run_id)
        assert [r.symbol for r in rows] == ["DISCOUNT"]
        provenance = rows[0].provenance_json
        assert provenance["source"] == "deterministic"
        assert provenance["screener_key"] == "envelope"
        assert provenance["screener_version"] == "1.0.0"
        assert provenance["triggered_rules"] == ["close_at_or_below_lower_envelope_band"]
        assert provenance["indicator_values"]["env_lower"] == 82.5
        # Run-level context the screener did not set is filled by the normalizer.
        assert provenance["data_snapshot_date"] == "2026-06-02"
