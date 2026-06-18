"""Tests for the SCAN-002 scan-history repository helpers.

The repository is the public write/read API that SCAN-003 will call. These tests
use a temporary SQLite database and real ORM objects so they exercise the same
mapping code the app will use, without needing Streamlit or Dhan data.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from backend.scanning.result_contract import AIEvaluationRecord, AIProvenance
from backend.storage.models import ScanStatus

# The ``db_session`` fixture these tests use lives in tests/conftest.py,
# shared with the other scan-history test modules.


def test_repository_creates_run_results_and_failed_status(db_session):
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
        db_session,
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
        db_session,
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
        db_session,
        run,
        status=ScanStatus.FAILED,
        error_message="Dhan rate limit stopped the scan",
    )
    db_session.commit()

    assert run.id is not None
    assert run.status is ScanStatus.FAILED
    assert run.error_message == "Dhan rate limit stopped the scan"
    assert run.finished_at is not None
    assert [result.symbol for result in results] == ["RELIANCE", "TCS"]

    latest = get_latest_scan_runs(db_session)
    assert [loaded.id for loaded in latest] == [run.id]

    by_symbol = {result.symbol: result for result in get_scan_results(db_session, run.id)}
    # Typed money columns keep Decimal precision for querying and display.
    assert by_symbol["RELIANCE"].close_price == Decimal("1234.5678")
    # JSON snapshots store Decimal values as strings so the audit copy is
    # lossless and JSON-serializable.
    assert by_symbol["RELIANCE"].raw_result_json["close"] == "1234.5678"
    assert by_symbol["RELIANCE"].raw_result_json["extra"]["threshold"] == "0.07"
    assert by_symbol["TCS"].close_price == Decimal("3890.0000")
    assert by_symbol["TCS"].final_score == Decimal("82.50")
    assert by_symbol["TCS"].raw_result_json["final_score"] == "82.50"
    assert by_symbol["TCS"].provenance_json == {
        "model": "claude-sonnet-4-6",
        "checked_at": "2026-06-04T10:00:00+00:00",
    }


def test_repository_orders_latest_runs_newest_first(db_session):
    """History queries should show the most recent run first."""
    from backend.storage.repository import create_scan_run, get_latest_scan_runs

    first = create_scan_run(db_session, screener_key="envelope", universe_key="nifty_500")
    second = create_scan_run(db_session, screener_key="knoxville", universe_key="nifty_100")
    first.started_at = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    second.started_at = dt.datetime(2026, 6, 2, tzinfo=dt.UTC)
    db_session.commit()

    assert [run.screener_key for run in get_latest_scan_runs(db_session, limit=1)] == [
        "knoxville"
    ]


def test_repository_breaks_started_at_ties_by_id_descending(db_session):
    """Runs sharing a started_at fall back to a deterministic newest-id-first order."""
    from backend.storage.repository import create_scan_run, get_latest_scan_runs

    # Two runs can land on the same started_at when a daily job fires back-to-back
    # or in fast tests. Without the id tie-breaker the database is free to return
    # same-timestamp rows in any order, which would make SCAN-004's history page
    # flicker between refreshes.
    same_started_at = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)
    earlier = create_scan_run(db_session, screener_key="envelope", universe_key="nifty_500")
    later = create_scan_run(db_session, screener_key="knoxville", universe_key="nifty_100")
    earlier.started_at = same_started_at
    later.started_at = same_started_at
    db_session.commit()

    # ``later`` was inserted second, so it holds the higher primary key and must
    # sort first under the id.desc() tie-breaker.
    assert later.id > earlier.id
    assert [run.id for run in get_latest_scan_runs(db_session)] == [later.id, earlier.id]


# ---------------------------------------------------------------------------
# SCAN-004: history-page filters, counts, and the symbols_scanned column
# ---------------------------------------------------------------------------


def _seed_history(db_session):
    """Insert three runs across two screeners/days for filter tests.

    Layout (all UTC):
    - run_a: envelope on 2026-06-01, shortlists RELIANCE + TCS
    - run_b: knoxville on 2026-06-02, shortlists WIPRO
    - run_c: envelope on 2026-06-03, no results (a failed run)
    """
    from backend.storage.repository import create_scan_run, save_scan_results

    run_a = create_scan_run(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
        symbols_scanned=500,
        triggered_by="ui:analyst@example.com",
    )
    run_b = create_scan_run(
        db_session,
        screener_key="knoxville",
        universe_key="nifty_100",
        symbols_scanned=100,
        triggered_by="job:daily_scan",
    )
    run_c = create_scan_run(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
        triggered_by="ui:admin@example.com",
    )
    run_a.status = ScanStatus.SUCCESS
    run_b.status = ScanStatus.PARTIAL
    run_c.status = ScanStatus.FAILED
    run_a.started_at = dt.datetime(2026, 6, 1, 10, 0, tzinfo=dt.UTC)
    run_b.started_at = dt.datetime(2026, 6, 2, 10, 0, tzinfo=dt.UTC)
    run_c.started_at = dt.datetime(2026, 6, 3, 10, 0, tzinfo=dt.UTC)
    save_scan_results(
        db_session,
        run_a,
        [{"symbol": "RELIANCE", "rating": "BUY"}, {"symbol": "TCS", "rating": "BUY"}],
    )
    save_scan_results(db_session, run_b, [{"symbol": "WIPRO", "rating": "BUY"}])
    db_session.commit()
    return run_a, run_b, run_c


def test_get_latest_scan_runs_filters_by_screener_key(db_session):
    """The screener filter keeps only that screener's runs, newest first."""
    from backend.storage.repository import get_latest_scan_runs

    run_a, _run_b, run_c = _seed_history(db_session)

    filtered = get_latest_scan_runs(db_session, screener_key="envelope")
    assert [run.id for run in filtered] == [run_c.id, run_a.id]


def test_get_latest_scan_runs_date_range_is_inclusive_on_both_ends(db_session):
    """started_from/started_to are calendar days; both boundary days count."""
    from backend.storage.repository import get_latest_scan_runs

    run_a, run_b, run_c = _seed_history(db_session)

    # The exact from/to days of the range must both be included.
    filtered = get_latest_scan_runs(
        db_session, started_from=dt.date(2026, 6, 1), started_to=dt.date(2026, 6, 2)
    )
    assert [run.id for run in filtered] == [run_b.id, run_a.id]

    # A single from-day with no upper bound keeps everything from that day on.
    filtered = get_latest_scan_runs(db_session, started_from=dt.date(2026, 6, 2))
    assert [run.id for run in filtered] == [run_c.id, run_b.id]


def test_get_latest_scan_runs_symbol_filter_is_exact_and_case_insensitive(db_session):
    """The symbol filter matches whole symbols regardless of case, not prefixes."""
    from backend.storage.repository import get_latest_scan_runs

    run_a, _run_b, _run_c = _seed_history(db_session)

    # Lowercase input still finds the run that shortlisted RELIANCE.
    assert [run.id for run in get_latest_scan_runs(db_session, symbol="reliance")] == [
        run_a.id
    ]
    # A prefix must NOT match: ticker symbols are codes, not prose.
    assert get_latest_scan_runs(db_session, symbol="RELI") == []


def test_get_latest_scan_runs_combines_filters(db_session):
    """All history filters AND together so each selected constraint is honored."""
    from backend.storage.repository import get_latest_scan_runs

    run_a, _run_b, _run_c = _seed_history(db_session)

    filtered = get_latest_scan_runs(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
        status=ScanStatus.SUCCESS,
        started_from=dt.date(2026, 6, 1),
        started_to=dt.date(2026, 6, 1),
        triggered_by="ui:analyst@example.com",
        symbol="TCS",
    )
    assert [run.id for run in filtered] == [run_a.id]

    # The same symbol under the wrong screener matches nothing.
    assert (
        get_latest_scan_runs(db_session, screener_key="knoxville", symbol="TCS") == []
    )


def test_get_latest_scan_runs_filters_by_universe_status_and_trigger(db_session):
    """The SCAN-004 dropdown filters map to exact persisted run metadata."""
    from backend.storage.repository import get_latest_scan_runs

    run_a, run_b, run_c = _seed_history(db_session)

    assert [
        run.id for run in get_latest_scan_runs(db_session, universe_key="nifty_500")
    ] == [run_c.id, run_a.id]
    assert [
        run.id for run in get_latest_scan_runs(db_session, status=ScanStatus.PARTIAL)
    ] == [run_b.id]
    assert [
        run.id
        for run in get_latest_scan_runs(
            db_session, triggered_by="ui:analyst@example.com"
        )
    ] == [run_a.id]


def test_count_scan_results_for_runs_includes_zero_for_empty_runs(db_session):
    """Every requested run id appears in the counts, even with no results."""
    from backend.storage.repository import count_scan_results_for_runs

    run_a, run_b, run_c = _seed_history(db_session)

    counts = count_scan_results_for_runs(db_session, [run_a.id, run_b.id, run_c.id])
    assert counts == {run_a.id: 2, run_b.id: 1, run_c.id: 0}

    # An empty request returns an empty mapping instead of querying.
    assert count_scan_results_for_runs(db_session, []) == {}


def test_create_scan_run_persists_symbols_scanned_and_defaults_to_none(db_session):
    """symbols_scanned stores the universe size; older callers default to NULL."""
    from backend.storage.repository import create_scan_run

    with_count = create_scan_run(
        db_session, screener_key="envelope", universe_key="nifty_500", symbols_scanned=500
    )
    without_count = create_scan_run(
        db_session, screener_key="envelope", universe_key="nifty_500"
    )
    db_session.commit()

    assert with_count.symbols_scanned == 500
    # Pre-SCAN-004 rows (and callers that do not know the size) stay NULL; the
    # history page renders that as an em-dash rather than a misleading zero.
    assert without_count.symbols_scanned is None


def test_finish_scan_run_persists_secret_safe_data_quality_receipt(db_session):
    """Data-quality metadata is stored as JSON without leaking raw secrets."""
    from backend.storage.repository import create_scan_run, finish_scan_run

    run = create_scan_run(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
    )
    finish_scan_run(
        db_session,
        run,
        status=ScanStatus.PARTIAL,
        error_message="quality failure",
        data_quality_json={
            "schema_version": 1,
            "findings": [
                {
                    "symbol": "RELIANCE",
                    "severity": "fatal",
                    "code": "HIGH_BELOW_LOW",
                    "message": "token=quality-secret",
                }
            ],
        },
    )
    db_session.commit()

    assert run.data_quality_json["findings"][0]["message"] == "token=***REDACTED***"


def test_list_distinct_screener_keys_is_sorted_and_deduplicated(db_session):
    """The history filter dropdown gets each recorded screener exactly once."""
    from backend.storage.repository import list_distinct_screener_keys

    _seed_history(db_session)  # two envelope runs + one knoxville run

    assert list_distinct_screener_keys(db_session) == ["envelope", "knoxville"]


def test_list_distinct_history_filter_values_are_sorted_and_deduplicated(db_session):
    """Universe and trigger dropdowns should contain clean recorded values."""
    from backend.storage.repository import (
        list_distinct_triggered_by_values,
        list_distinct_universe_keys,
    )

    _seed_history(db_session)

    assert list_distinct_universe_keys(db_session) == ["nifty_100", "nifty_500"]
    assert list_distinct_triggered_by_values(db_session) == [
        "job:daily_scan",
        "ui:admin@example.com",
        "ui:analyst@example.com",
    ]


def test_create_scan_run_uses_recursive_secret_safe_json_normalization(db_session):
    from backend.storage.repository import create_scan_run

    run = create_scan_run(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
        params={
            "as_of": dt.date(2026, 6, 13),
            "threshold": Decimal("0.1250"),
            "callback": lambda: None,
            "nested": {
                "api_key": "raw-secret",
                "message": "token=inline-secret",
            },
        },
    )

    assert run.params_json == {
        "as_of": "2026-06-13",
        "threshold": "0.1250",
        "nested": {
            "api_key": "***REDACTED***",
            "message": "token=***REDACTED***",
        },
    }


def test_repository_saves_and_gets_sanitized_ai_evaluations(db_session):
    from backend.storage.repository import (
        create_scan_run,
        get_ai_evaluations,
        save_ai_evaluations,
    )

    run = create_scan_run(
        db_session,
        screener_key="technical_analysis_ai",
        universe_key="nifty_500",
    )
    saved = save_ai_evaluations(
        db_session,
        run,
        [
            AIEvaluationRecord(
                symbol="TCS",
                signal_date=dt.date(2026, 6, 13),
                outcome="approved",
                verdict="BUY",
                confidence=Decimal("8.75"),
                decision_reason="token=reason-secret",
                provenance=AIProvenance(
                    model_name="gpt-test",
                    prompt_version="v1",
                    prompt_sha256="b" * 64,
                    generated_at=dt.datetime(2026, 6, 13, 8, 0, tzinfo=dt.UTC),
                    cache_hit=False,
                    verdict="BUY",
                    confidence=Decimal("8.75"),
                    decision_reason="token=reason-secret",
                    evidence_references=[],
                ),
                validated_verdict_json={
                    "rating": "BUY",
                    "api_key": "verdict-secret",
                },
            )
        ],
    )
    db_session.commit()

    assert saved[0].run_id == run.id
    loaded = get_ai_evaluations(db_session, run.id)
    assert len(loaded) == 1
    assert loaded[0].outcome == "approved"
    assert loaded[0].confidence == Decimal("8.7500")
    assert loaded[0].validated_verdict_json["api_key"] == "***REDACTED***"
    assert "reason-secret" not in str(loaded[0].validated_verdict_json)
    assert loaded[0].provenance_json["verdict"] == "BUY"
    assert loaded[0].provenance_json["confidence"] == "8.75"
    assert "reason-secret" not in loaded[0].provenance_json["decision_reason"]


def test_repository_allows_error_ai_receipts_without_decision_fields(db_session):
    from backend.storage.repository import create_scan_run, save_ai_evaluations

    run = create_scan_run(
        db_session,
        screener_key="technical_analysis_ai",
        universe_key="nifty_500",
    )
    saved = save_ai_evaluations(
        db_session,
        run,
        [
            AIEvaluationRecord(
                symbol="TCS",
                signal_date=dt.date(2026, 6, 13),
                outcome="error",
                verdict=None,
                confidence=None,
                decision_reason=None,
                provenance=AIProvenance(
                    model_name="gpt-test",
                    prompt_version="v1",
                    prompt_sha256="b" * 64,
                    generated_at=dt.datetime(2026, 6, 13, 8, 0, tzinfo=dt.UTC),
                    cache_hit=False,
                ),
            )
        ],
    )

    assert saved[0].verdict_label is None
    assert saved[0].confidence is None
    assert saved[0].provenance_json["verdict"] is None
    assert saved[0].provenance_json["confidence"] is None
    assert saved[0].provenance_json["decision_reason"] is None


def test_repository_rejects_verdict_json_that_contradicts_the_trusted_receipt(
    db_session,
):
    from backend.storage.repository import create_scan_run, save_ai_evaluations

    run = create_scan_run(
        db_session,
        screener_key="technical_analysis_ai",
        universe_key="nifty_500",
    )
    record = AIEvaluationRecord(
        symbol="TCS",
        signal_date=dt.date(2026, 6, 13),
        outcome="approved",
        verdict="BUY",
        confidence=Decimal("8"),
        decision_reason="Validated reason.",
        provenance=AIProvenance(
            model_name="gpt-test",
            prompt_version="v1",
            prompt_sha256="b" * 64,
            generated_at=dt.datetime(2026, 6, 13, 8, 0, tzinfo=dt.UTC),
            cache_hit=False,
            verdict="BUY",
            confidence=Decimal("8"),
            decision_reason="Validated reason.",
        ),
        validated_verdict_json={
            "verdict": "SELL",
            "confidence": 1,
            "decision_reason": "Contradictory reason.",
        },
    )

    with pytest.raises(ValueError, match="validated_verdict_json verdict"):
        save_ai_evaluations(db_session, run, [record])


def test_storage_package_exports_ai_evaluation_api():
    from backend import storage

    assert storage.AIEvaluation is not None
    assert callable(storage.save_ai_evaluations)
    assert callable(storage.get_ai_evaluations)


def test_get_signals_needing_forward_returns_returns_missing_and_pending_only(db_session):
    """VALID-002 should retry missing/pending horizons and skip terminal rows."""
    from backend.storage.models import ForwardReturnStatus, SignalForwardReturn
    from backend.storage.repository import (
        create_scan_run,
        get_signals_needing_forward_returns,
        save_scan_results,
    )

    run = create_scan_run(db_session, screener_key="envelope", universe_key="nifty_500")
    missing, pending, computed, insufficient = save_scan_results(
        db_session,
        run,
        [
            {"symbol": "MISSING", "signal_date": dt.date(2026, 1, 5)},
            {"symbol": "PENDING", "signal_date": dt.date(2026, 1, 5)},
            {"symbol": "COMPUTED", "signal_date": dt.date(2026, 1, 5)},
            {"symbol": "INSUFF", "signal_date": dt.date(2026, 1, 5)},
        ],
    )
    db_session.add_all(
        [
            SignalForwardReturn(result_id=pending.id, horizon_days=20),
            SignalForwardReturn(
                result_id=computed.id,
                horizon_days=20,
                status=ForwardReturnStatus.COMPUTED,
            ),
            SignalForwardReturn(
                result_id=insufficient.id,
                horizon_days=20,
                status=ForwardReturnStatus.INSUFFICIENT_DATA,
            ),
        ]
    )
    db_session.commit()

    rows = get_signals_needing_forward_returns(db_session, horizons=(20,))

    assert [row.id for row in rows] == [missing.id, pending.id]
    assert rows[0].run.universe_key == "nifty_500"


def test_upsert_forward_return_updates_pending_row_in_place(db_session):
    """VALID-002 re-runs must update by (result_id, horizon_days), not duplicate."""
    from sqlalchemy import select

    from backend.storage.models import ForwardReturnStatus, SignalForwardReturn
    from backend.storage.repository import (
        create_scan_run,
        save_scan_results,
        upsert_forward_return,
    )
    from backend.validation.forward_return import ForwardReturnPoint

    run = create_scan_run(db_session, screener_key="envelope", universe_key="nifty_500")
    [result] = save_scan_results(
        db_session,
        run,
        [{"symbol": "RELIANCE", "signal_date": dt.date(2026, 1, 5)}],
    )
    db_session.add(SignalForwardReturn(result_id=result.id, horizon_days=20))
    db_session.commit()

    row = upsert_forward_return(
        db_session,
        result_id=result.id,
        point=ForwardReturnPoint(
            horizon_days=20,
            status=ForwardReturnStatus.COMPUTED,
            entry_date=dt.date(2026, 1, 6),
            exit_date=dt.date(2026, 2, 3),
            entry_price=Decimal("100.0000"),
            exit_price=Decimal("112.5000"),
            forward_return_pct=Decimal("12.5000"),
            max_adverse_excursion_pct=Decimal("-4.2000"),
            max_favorable_excursion_pct=Decimal("15.1000"),
        ),
    )
    db_session.commit()

    rows = list(db_session.scalars(select(SignalForwardReturn)))
    assert len(rows) == 1
    assert rows[0].id == row.id
    assert rows[0].status is ForwardReturnStatus.COMPUTED
    assert rows[0].forward_return_pct == Decimal("12.5000")
    assert rows[0].computed_at is not None
