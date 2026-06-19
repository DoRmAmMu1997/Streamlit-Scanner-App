"""VALID-003A aggregate metrics tests over stored forward-return rows."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

from sqlalchemy.orm import Session

from backend.storage.models import ForwardReturnStatus, ScanStatus, SignalForwardReturn
from backend.storage.repository import create_scan_run, save_scan_results
from backend.validation.metrics import summarize_validation_metrics


def _seed_run(
    session: Session,
    *,
    screener_key: str = "envelope_knoxville_buy",
    universe_key: str = "nifty_500",
    status: ScanStatus = ScanStatus.SUCCESS,
):
    # Metrics only aggregate SUCCESS/PARTIAL runs, so seeded runs must be marked
    # finished rather than left in the default RUNNING state.
    run = create_scan_run(
        session,
        screener_key=screener_key,
        universe_key=universe_key,
        data_snapshot_date=dt.date(2026, 1, 31),
    )
    run.status = status
    return run


def _seed_result(
    session: Session,
    *,
    run,
    symbol: str,
    signal_date: dt.date,
):
    [result] = save_scan_results(
        session,
        run,
        [
            {
                "symbol": symbol,
                "signal_date": signal_date,
                "close": Decimal("100.0000"),
                "rating": "BUY",
            }
        ],
    )
    return result


def _add_forward_return(
    session: Session,
    *,
    result,
    horizon_days: int = 20,
    status: ForwardReturnStatus = ForwardReturnStatus.COMPUTED,
    forward_return_pct: Decimal | None = None,
    excess_return_pct: Decimal | None = None,
    mae_pct: Decimal | None = None,
    mfe_pct: Decimal | None = None,
) -> None:
    session.add(
        SignalForwardReturn(
            result_id=result.id,
            horizon_days=horizon_days,
            status=status,
            forward_return_pct=forward_return_pct,
            excess_return_pct=excess_return_pct,
            max_adverse_excursion_pct=mae_pct,
            max_favorable_excursion_pct=mfe_pct,
        )
    )


def test_summarize_one_group_keeps_pending_insufficient_visible_without_losses(db_session):
    run = _seed_run(db_session)
    winner = _seed_result(
        db_session, run=run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    loser = _seed_result(db_session, run=run, symbol="TCS", signal_date=dt.date(2026, 1, 6))
    pending = _seed_result(
        db_session, run=run, symbol="INFY", signal_date=dt.date(2026, 1, 7)
    )
    insufficient = _seed_result(
        db_session, run=run, symbol="WIPRO", signal_date=dt.date(2026, 1, 8)
    )
    _add_forward_return(
        db_session,
        result=winner,
        forward_return_pct=Decimal("10.0000"),
        excess_return_pct=Decimal("2.0000"),
        mae_pct=Decimal("-4.0000"),
        mfe_pct=Decimal("16.0000"),
    )
    _add_forward_return(
        db_session,
        result=loser,
        forward_return_pct=Decimal("-5.0000"),
        excess_return_pct=None,
        mae_pct=Decimal("-8.0000"),
        mfe_pct=Decimal("4.0000"),
    )
    _add_forward_return(db_session, result=pending, status=ForwardReturnStatus.PENDING)
    _add_forward_return(
        db_session, result=insufficient, status=ForwardReturnStatus.INSUFFICIENT_DATA
    )
    db_session.commit()

    summary = summarize_validation_metrics(db_session)

    assert summary.total_measurements == 4
    assert summary.computed_measurements == 2
    assert len(summary.rows) == 1
    row = summary.rows[0]
    assert row.screener_key == "envelope_knoxville_buy"
    assert row.universe_key == "nifty_500"
    assert row.horizon_days == 20
    assert row.total_signals == 4
    assert row.computed_count == 2
    assert row.pending_count == 1
    assert row.insufficient_data_count == 1
    assert row.hit_rate_pct == Decimal("50.0000")
    assert row.average_forward_return_pct == Decimal("2.5000")
    assert row.median_forward_return_pct == Decimal("2.5000")
    assert row.average_excess_return_pct == Decimal("2.0000")
    assert row.median_excess_return_pct == Decimal("2.0000")
    assert row.average_mae_pct == Decimal("-6.0000")
    assert row.average_mfe_pct == Decimal("10.0000")
    assert row.best_signal is not None
    assert row.best_signal.symbol == "RELIANCE"
    assert row.best_signal.forward_return_pct == Decimal("10.0000")
    assert row.worst_signal is not None
    assert row.worst_signal.symbol == "TCS"
    assert row.worst_signal.forward_return_pct == Decimal("-5.0000")


def test_summarize_groups_multiple_horizons_and_screeners(db_session):
    envelope_run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    bollinger_run = _seed_run(db_session, screener_key="bollinger", universe_key="fno")
    envelope_result = _seed_result(
        db_session, run=envelope_run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    bollinger_result = _seed_result(
        db_session, run=bollinger_run, symbol="TCS", signal_date=dt.date(2026, 1, 5)
    )
    _add_forward_return(
        db_session,
        result=envelope_result,
        horizon_days=20,
        forward_return_pct=Decimal("8.0000"),
    )
    _add_forward_return(
        db_session,
        result=envelope_result,
        horizon_days=60,
        forward_return_pct=Decimal("12.0000"),
    )
    _add_forward_return(
        db_session,
        result=bollinger_result,
        horizon_days=20,
        forward_return_pct=Decimal("-3.0000"),
    )
    db_session.commit()

    summary = summarize_validation_metrics(db_session)

    assert [
        (row.screener_key, row.universe_key, row.horizon_days)
        for row in summary.rows
    ] == [
        ("bollinger", "fno", 20),
        ("envelope", "nifty_500", 20),
        ("envelope", "nifty_500", 60),
    ]


def test_summarize_filters_by_dimensions_and_inclusive_signal_date_range(db_session):
    run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    before = _seed_result(
        db_session, run=run, symbol="BEFORE", signal_date=dt.date(2026, 1, 4)
    )
    start = _seed_result(
        db_session, run=run, symbol="START", signal_date=dt.date(2026, 1, 5)
    )
    end = _seed_result(db_session, run=run, symbol="END", signal_date=dt.date(2026, 1, 10))
    after = _seed_result(
        db_session, run=run, symbol="AFTER", signal_date=dt.date(2026, 1, 11)
    )
    for result, value in [
        (before, Decimal("1.0000")),
        (start, Decimal("2.0000")),
        (end, Decimal("4.0000")),
        (after, Decimal("8.0000")),
    ]:
        _add_forward_return(db_session, result=result, forward_return_pct=value)
    db_session.commit()

    summary = summarize_validation_metrics(
        db_session,
        screener_key="envelope",
        universe_key="nifty_500",
        horizon_days=20,
        signal_date_from=dt.date(2026, 1, 5),
        signal_date_to=dt.date(2026, 1, 10),
    )

    assert summary.filters.signal_date_from == dt.date(2026, 1, 5)
    assert len(summary.rows) == 1
    row = summary.rows[0]
    assert row.total_signals == 2
    assert row.average_forward_return_pct == Decimal("3.0000")
    assert row.best_signal is not None
    assert row.best_signal.symbol == "END"
    assert row.worst_signal is not None
    assert row.worst_signal.symbol == "START"


def test_summarize_missing_excess_values_do_not_fabricate_benchmark_metrics(db_session):
    run = _seed_run(db_session)
    result = _seed_result(
        db_session, run=run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    _add_forward_return(
        db_session,
        result=result,
        forward_return_pct=Decimal("7.5000"),
        excess_return_pct=None,
    )
    db_session.commit()

    row = summarize_validation_metrics(db_session).rows[0]

    assert row.average_forward_return_pct == Decimal("7.5000")
    assert row.median_forward_return_pct == Decimal("7.5000")
    assert row.average_excess_return_pct is None
    assert row.median_excess_return_pct is None


def test_summarize_best_worst_uses_deterministic_ties(db_session):
    run = _seed_run(db_session)
    first = _seed_result(db_session, run=run, symbol="AAA", signal_date=dt.date(2026, 1, 5))
    second = _seed_result(
        db_session, run=run, symbol="BBB", signal_date=dt.date(2026, 1, 6)
    )
    _add_forward_return(db_session, result=first, forward_return_pct=Decimal("5.0000"))
    _add_forward_return(db_session, result=second, forward_return_pct=Decimal("5.0000"))
    db_session.commit()

    row = summarize_validation_metrics(db_session).rows[0]

    assert row.best_signal is not None
    assert row.best_signal.symbol == "AAA"
    assert row.worst_signal is not None
    assert row.worst_signal.symbol == "AAA"


def test_summarize_no_computed_rows_returns_none_metrics_safely(db_session):
    run = _seed_run(db_session)
    pending = _seed_result(
        db_session, run=run, symbol="INFY", signal_date=dt.date(2026, 1, 5)
    )
    insufficient = _seed_result(
        db_session, run=run, symbol="WIPRO", signal_date=dt.date(2026, 1, 6)
    )
    _add_forward_return(db_session, result=pending, status=ForwardReturnStatus.PENDING)
    _add_forward_return(
        db_session, result=insufficient, status=ForwardReturnStatus.INSUFFICIENT_DATA
    )
    db_session.commit()

    row = summarize_validation_metrics(db_session).rows[0]

    assert row.total_signals == 2
    assert row.computed_count == 0
    assert row.hit_rate_pct is None
    assert row.average_forward_return_pct is None
    assert row.median_forward_return_pct is None
    assert row.average_excess_return_pct is None
    assert row.median_excess_return_pct is None
    assert row.average_mae_pct is None
    assert row.average_mfe_pct is None
    assert row.best_signal is None
    assert row.worst_signal is None


def test_summarize_excludes_non_success_and_running_runs(db_session):
    success_run = _seed_run(db_session, status=ScanStatus.SUCCESS)
    failed_run = _seed_run(db_session, status=ScanStatus.FAILED)
    running_run = _seed_run(db_session, status=ScanStatus.RUNNING)
    kept = _seed_result(
        db_session, run=success_run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    from_failed = _seed_result(
        db_session, run=failed_run, symbol="TCS", signal_date=dt.date(2026, 1, 5)
    )
    from_running = _seed_result(
        db_session, run=running_run, symbol="INFY", signal_date=dt.date(2026, 1, 5)
    )
    _add_forward_return(db_session, result=kept, forward_return_pct=Decimal("5.0000"))
    _add_forward_return(db_session, result=from_failed, forward_return_pct=Decimal("9.0000"))
    _add_forward_return(db_session, result=from_running, forward_return_pct=Decimal("9.0000"))
    db_session.commit()

    summary = summarize_validation_metrics(db_session)

    # Only the SUCCESS run's signal survives; the FAILED/RUNNING signals (which
    # share the same screener/universe/horizon group) never reach the metrics.
    assert summary.total_measurements == 1
    assert len(summary.rows) == 1
    assert summary.rows[0].best_signal is not None
    assert summary.rows[0].best_signal.symbol == "RELIANCE"


def test_summarize_dedupes_repeated_signal_keeping_latest_run(db_session):
    earlier_run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    later_run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    earlier_run.started_at = dt.datetime(2026, 1, 10, tzinfo=dt.UTC)
    later_run.started_at = dt.datetime(2026, 1, 11, tzinfo=dt.UTC)
    earlier = _seed_result(
        db_session, run=earlier_run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    later = _seed_result(
        db_session, run=later_run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    _add_forward_return(db_session, result=earlier, forward_return_pct=Decimal("3.0000"))
    _add_forward_return(db_session, result=later, forward_return_pct=Decimal("9.0000"))
    db_session.commit()

    summary = summarize_validation_metrics(db_session)

    # The same signal measured by two runs counts once, using the later run.
    assert summary.total_measurements == 1
    row = summary.rows[0]
    assert row.total_signals == 1
    assert row.computed_count == 1
    assert row.average_forward_return_pct == Decimal("9.0000")
    assert row.best_signal is not None
    assert row.best_signal.forward_return_pct == Decimal("9.0000")


def test_summarize_counts_each_horizon_as_a_measurement(db_session):
    run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    signal = _seed_result(
        db_session, run=run, symbol="RELIANCE", signal_date=dt.date(2026, 1, 5)
    )
    for horizon, value in [
        (20, Decimal("4.0000")),
        (60, Decimal("8.0000")),
        (120, Decimal("12.0000")),
    ]:
        _add_forward_return(
            db_session, result=signal, horizon_days=horizon, forward_return_pct=value
        )
    db_session.commit()

    summary = summarize_validation_metrics(db_session)

    # One signal across three horizons -> three measurement rows in three groups,
    # while each per-horizon row still counts a single signal.
    assert summary.total_measurements == 3
    assert summary.computed_measurements == 3
    assert [row.horizon_days for row in summary.rows] == [20, 60, 120]
    assert {row.total_signals for row in summary.rows} == {1}


def test_summarize_includes_null_signal_date_until_a_date_filter_applies(db_session):
    run = _seed_run(db_session, screener_key="envelope", universe_key="nifty_500")
    [undated] = save_scan_results(
        db_session, run, [{"symbol": "RELIANCE", "signal_date": None, "rating": "BUY"}]
    )
    _add_forward_return(db_session, result=undated, forward_return_pct=Decimal("6.0000"))
    db_session.commit()

    unfiltered = summarize_validation_metrics(db_session)
    assert unfiltered.total_measurements == 1
    row = unfiltered.rows[0]
    assert row.total_signals == 1
    assert row.first_signal_date is None
    assert row.last_signal_date is None

    # A signal-date filter is meaningless for an undated row, so it drops out.
    filtered = summarize_validation_metrics(db_session, signal_date_from=dt.date(2026, 1, 1))
    assert filtered.total_measurements == 0
    assert filtered.rows == ()


def test_validation_package_exports_metrics_api():
    from backend import validation

    assert validation.BestWorstSignal is not None
    assert validation.ValidationMetricRow is not None
    assert validation.ValidationSummary is not None
    assert callable(validation.summarize_validation_metrics)
