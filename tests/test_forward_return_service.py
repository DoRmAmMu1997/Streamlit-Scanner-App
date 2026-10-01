"""VALID-002 service tests for filling signal_forward_returns."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import func, select

from backend.storage.models import ForwardReturnStatus, SignalForwardReturn
from backend.storage.repository import create_scan_run, save_scan_results
from backend.validation.benchmarks import BenchmarkSpec
from backend.validation.service import compute_pending_forward_returns


class _FakeDailyLoader:
    def __init__(self, frames: dict[str, pd.DataFrame]) -> None:
        self.frames = frames
        self.calls: list[dict[str, Any]] = []

    def get_daily_history(
        self,
        instrument: dict[str, object] | pd.Series,
        start_date: dt.date,
        end_date: dt.date,
        force_refresh: bool = False,
        *,
        preserve_malformed_rows: bool = False,
    ) -> tuple[pd.DataFrame, bool]:
        del force_refresh, preserve_malformed_rows
        row = dict(instrument)
        symbol = str(row["symbol"]).upper()
        self.calls.append({"symbol": symbol, "start_date": start_date, "end_date": end_date})
        return self.frames.get(symbol, pd.DataFrame()), True


def _candles(rows: list[tuple[str, str, str, str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "timestamp": day,
                "open": open_,
                "high": high,
                "low": low,
                "close": close,
                "volume": 1000,
            }
            for day, open_, high, low, close in rows
        ]
    )


def _universe(symbols: list[tuple[str, str]]) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "security_id": security_id,
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "mapping_status": "mapped",
            }
            for symbol, security_id in symbols
        ]
    )


def _seed_signal(session_factory, *, universe_key: str = "nifty_500") -> int:
    with session_factory() as session:
        run = create_scan_run(
            session,
            screener_key="envelope_knoxville_buy",
            universe_key=universe_key,
            data_snapshot_date=dt.date(2026, 1, 10),
        )
        [result] = save_scan_results(
            session,
            run,
            [
                {
                    "symbol": "RELIANCE",
                    "signal_date": dt.date(2026, 1, 5),
                    "close": Decimal("92.0000"),
                    "rating": "BUY",
                }
            ],
        )
        return result.id


def test_service_upserts_forward_return_and_benchmark_without_duplicates(session_factory):
    result_id = _seed_signal(session_factory)
    loader = _FakeDailyLoader(
        {
            "RELIANCE": _candles(
                [
                    ("2026-01-05", "90", "95", "88", "92"),
                    ("2026-01-06", "100", "106", "98", "104"),
                    ("2026-01-07", "105", "120", "95", "110"),
                    ("2026-01-09", "111", "118", "99", "115"),
                ]
            ),
            "NIFTY TEST": _candles(
                [
                    ("2026-01-06", "200", "206", "198", "204"),
                    ("2026-01-09", "210", "222", "205", "220"),
                ]
            ),
        }
    )

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _key: BenchmarkSpec(
            key="nifty_test",
            symbol="NIFTY TEST",
            security_id="INDEX123",
        ),
    )
    assert summary.computed == 1
    assert summary.pending == 0
    assert summary.insufficient == 0
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.result_id == result_id
        assert row.status is ForwardReturnStatus.COMPUTED
        assert row.forward_return_pct == Decimal("15.0000")
        assert row.benchmark_key == "nifty_test"
        assert row.benchmark_return_pct == Decimal("10.0000")
        assert row.excess_return_pct == Decimal("5.0000")

    second = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _key: BenchmarkSpec(
            key="nifty_test",
            symbol="NIFTY TEST",
            security_id="INDEX123",
        ),
    )
    assert second.total_signals == 0
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignalForwardReturn)) == 1


def test_service_computes_stock_return_when_benchmark_is_unresolved(session_factory):
    _seed_signal(session_factory, universe_key="fno")
    loader = _FakeDailyLoader(
        {
            "RELIANCE": _candles(
                [
                    ("2026-01-05", "90", "95", "88", "92"),
                    ("2026-01-06", "100", "106", "98", "104"),
                    ("2026-01-07", "105", "120", "95", "110"),
                    ("2026-01-09", "111", "118", "99", "115"),
                ]
            )
        }
    )

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
        # Force the unresolved path explicitly: since VALID-002B ships verified
        # ids, the default resolver now resolves "fno", so this test injects an
        # always-None resolver to keep covering the graceful-null behaviour.
        benchmark_resolver=lambda _key: None,
    )
    assert summary.computed == 1
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.forward_return_pct == Decimal("15.0000")
        assert row.benchmark_key is None
        assert row.benchmark_return_pct is None
        assert row.excess_return_pct is None
        assert row.benchmark_retry_pending is False


def test_service_marks_missing_symbol_mapping_insufficient_without_loading_data(session_factory):
    _seed_signal(session_factory)
    loader = _FakeDailyLoader({})

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("TCS", "532540")]),
    )
    assert summary.insufficient == 1
    assert loader.calls == []
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.status is ForwardReturnStatus.INSUFFICIENT_DATA
        assert row.forward_return_pct is None


def test_service_recomputes_pending_signal_to_computed_on_later_run(session_factory):
    """A window that has not elapsed yet stays PENDING, then upserts to COMPUTED.

    This is the retryability contract end-to-end: the first pass cannot see the
    exit bar's date as having passed (no lookahead), so it records PENDING; a later
    pass — once ``as_of`` reaches the exit date — re-selects that same pending row
    (terminal rows would be skipped) and updates it in place, never duplicating.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader(
        {
            "RELIANCE": _candles(
                [
                    ("2026-01-05", "90", "95", "88", "92"),    # signal bar
                    ("2026-01-06", "100", "106", "98", "104"),  # entry (open=100)
                    ("2026-01-07", "105", "120", "95", "110"),
                    ("2026-01-08", "111", "118", "99", "115"),  # exit (close=115)
                ]
            )
        }
    )

    # First pass: as_of is BEFORE the exit bar's date, so the window has not closed.
    first = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 7),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )
    assert first.pending == 1
    assert first.computed == 0
    with session_factory() as session:
        pending_row = session.scalars(select(SignalForwardReturn)).one()
        pending_id = pending_row.id
        assert pending_row.status is ForwardReturnStatus.PENDING
        assert pending_row.forward_return_pct is None
        assert pending_row.computed_at is None

    # Second pass: as_of now past the exit date — the same row flips to COMPUTED.
    second = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )
    assert second.total_signals == 1  # the pending row was retryable, so re-selected
    assert second.computed == 1
    with session_factory() as session:
        assert session.scalar(select(func.count()).select_from(SignalForwardReturn)) == 1
        computed_row = session.scalars(select(SignalForwardReturn)).one()
        assert computed_row.id == pending_id  # updated in place, not a new row
        assert computed_row.status is ForwardReturnStatus.COMPUTED
        assert computed_row.forward_return_pct == Decimal("15.0000")
        assert computed_row.computed_at is not None


def test_service_keeps_signal_pending_and_retryable_when_universe_cannot_load(session_factory):
    """A missing/corrupt universe is an environment fault → PENDING, not terminal.

    Contrast with the symbol-missing case above (INSUFFICIENT_DATA, terminal): there
    the universe loaded and simply had no row for the symbol. Here the universe load
    itself fails, which must not permanently brand every signal of that universe as
    un-measurable — it stays PENDING so a later run can still compute it. No candle
    fetch is attempted because there is no instrument to fetch.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader({})

    def _broken_universe(_key: str) -> pd.DataFrame:
        raise FileNotFoundError("universe CSV not generated yet")

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(20, 60),
        universe_loader=_broken_universe,
    )
    assert summary.pending == 2  # one row per horizon
    assert summary.insufficient == 0
    assert loader.calls == []  # nothing to fetch without an instrument
    with session_factory() as session:
        statuses = session.scalars(select(SignalForwardReturn.status)).all()
        assert set(statuses) == {ForwardReturnStatus.PENDING}

    # Retryable: the pending rows are still selected on a later pass.
    retry = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(20, 60),
        universe_loader=_broken_universe,
    )
    assert retry.total_signals == 1


def test_service_fetches_only_signal_date_through_as_of(session_factory):
    """Bound provider requests to observable historical dates.

    Beginner note:
        The old horizon-times-three buffer requested future data; this test fails if that future range is
        restored.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader(
        {
            "RELIANCE": _candles(
                [
                    ("2026-01-05", "90", "95", "88", "92"),
                    ("2026-01-06", "100", "106", "98", "104"),
                ]
            )
        }
    )

    compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 8),
        horizons=(120,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _key: None,
    )

    assert loader.calls == [
        {
            "symbol": "RELIANCE",
            "start_date": dt.date(2026, 1, 5),
            "end_date": dt.date(2026, 1, 8),
        }
    ]


def test_service_keeps_future_signal_pending_without_any_fetch(session_factory):
    """Leave future signals pending without asking for an inverted date range.

    Beginner note:
        A signal after as_of cannot have an observable entry, so even universe resolution and provider
        fetching are unnecessary.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader({})

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 4),
        horizons=(20,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )

    assert summary.pending == 1
    assert loader.calls == []


def test_service_keeps_malformed_stock_data_retryable(session_factory):
    """Service policy differs from the pure calculator's terminal-looking result.

    Beginner note:
    The calculator has no provider and can only say "unavailable". The worker
    knows malformed downloaded data may be repaired on a later fetch, so it
    stores PENDING instead of permanently closing the signal.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader(
        {
            "RELIANCE": _candles(
                [
                    ("2026-01-05", "90", "95", "88", "92"),
                    ("bad-date", "100", "106", "98", "104"),
                ]
            )
        }
    )

    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        as_of=dt.date(2026, 1, 8),
        horizons=(1,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )

    assert summary.pending == 1
    with session_factory() as session:
        assert session.scalars(select(SignalForwardReturn.status)).one() is ForwardReturnStatus.PENDING


def test_service_repairs_benchmark_only_without_refetching_or_mutating_stock(session_factory):
    """Recover an index leg while preserving every completed stock fact.

    Beginner note:
        Refetching stock could rewrite history after a provider revision. A benchmark-only retry must use
        stored dates and leave computed_at and all stock fields identical.
    """
    _seed_signal(session_factory)
    spec = BenchmarkSpec(key="nifty_test", symbol="NIFTY TEST", security_id="INDEX123")
    stock = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-06", "100", "106", "98", "104"),
            ("2026-01-09", "111", "118", "99", "115"),
        ]
    )
    first_loader = _FakeDailyLoader({"RELIANCE": stock, "NIFTY TEST": pd.DataFrame()})
    compute_pending_forward_returns(
        session_factory,
        first_loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(2,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _key: spec,
    )
    with session_factory() as session:
        before = session.scalars(select(SignalForwardReturn)).one()
        stock_facts = (
            before.status,
            before.entry_date,
            before.exit_date,
            before.entry_price,
            before.exit_price,
            before.forward_return_pct,
            before.max_adverse_excursion_pct,
            before.max_favorable_excursion_pct,
            before.computed_at,
        )
        assert before.benchmark_retry_pending is True

    retry_loader = _FakeDailyLoader(
        {
            "NIFTY TEST": _candles(
                [
                    ("2026-01-06", "200", "206", "198", "204"),
                    ("2026-01-09", "210", "222", "205", "220"),
                ]
            )
        }
    )
    summary = compute_pending_forward_returns(
        session_factory,
        retry_loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(2,),
        universe_loader=lambda _key: pytest.fail("benchmark-only retry loaded the universe"),
        benchmark_resolver=lambda _key: spec,
    )

    assert [call["symbol"] for call in retry_loader.calls] == ["NIFTY TEST"]
    assert summary.benchmark_computed == 1
    with session_factory() as session:
        after = session.scalars(select(SignalForwardReturn)).one()
        assert (
            after.status,
            after.entry_date,
            after.exit_date,
            after.entry_price,
            after.exit_price,
            after.forward_return_pct,
            after.max_adverse_excursion_pct,
            after.max_favorable_excursion_pct,
            after.computed_at,
        ) == stock_facts
        assert after.benchmark_return_pct == Decimal("10.0000")
        assert after.excess_return_pct == Decimal("5.0000")
        assert after.benchmark_retry_pending is False


@pytest.mark.parametrize(
    ("horizons", "limit"),
    [((True,), None), ((0,), None), ((-1,), None), ((1.5,), None), ((20,), True), ((20,), 0)],
)
def test_service_rejects_invalid_inputs_before_opening_a_session(horizons, limit):
    """Reject invalid runtime inputs before database or provider work.

    Beginner note:
        Coercing fractional horizons or boolean limits would silently select different work; the forbidden
        factory proves rejection precedes I/O.
    """
    def forbidden_factory():
        pytest.fail("invalid input opened a database session")

    with pytest.raises(ValueError, match="positive integer"):
        compute_pending_forward_returns(
            forbidden_factory,
            _FakeDailyLoader({}),
            horizons=horizons,
            limit=limit,
        )


def test_service_deduplicates_horizons_in_order_and_empty_is_noop(session_factory):
    """Process each requested horizon once and let empty requests do no work.

    Beginner note:
        Repeated horizon values must not inflate counts or duplicate receipts, and an empty request should
        not require provider credentials.
    """
    _seed_signal(session_factory)
    loader = _FakeDailyLoader({})

    empty = compute_pending_forward_returns(session_factory, loader, horizons=())
    summary = compute_pending_forward_returns(
        session_factory,
        loader,
        horizons=(20, 20, 60, 20),
        universe_loader=lambda _key: (_ for _ in ()).throw(FileNotFoundError()),
    )

    assert empty.total_signals == 0
    assert summary.pending == 2
    with session_factory() as session:
        rows = session.scalars(select(SignalForwardReturn).order_by(SignalForwardReturn.id)).all()
        assert [row.horizon_days for row in rows] == [20, 60]


@pytest.mark.parametrize("failure", ["loader", "universe", "mapping", "malformed"])
def test_mixed_horizons_preserve_completed_receipt_on_every_retry_failure(session_factory, failure):
    """Retry failures must never erase the already measured one-day return.

    Beginner note:
        The original bug recomputed all requested horizons and replaced a +4%
        terminal row with an empty failure receipt when only day 20 needed work.
    """
    _seed_signal(session_factory)
    frame = _candles([("2026-01-05", "90", "95", "88", "92"),
                      ("2026-01-06", "100", "106", "98", "104")])
    loader = _FakeDailyLoader({"RELIANCE": frame})
    options = dict(as_of=dt.date(2026, 1, 8), horizons=(1, 20), benchmark_resolver=lambda _: None)
    compute_pending_forward_returns(session_factory, loader,
        universe_loader=lambda _: _universe([("RELIANCE", "500325")]), **options)
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn).where(SignalForwardReturn.horizon_days == 1)).one()
        before = tuple(getattr(row, c.name) for c in row.__table__.columns)

    def universe(_key):
        if failure == "universe":
            raise FileNotFoundError()
        return _universe([("TCS" if failure == "mapping" else "RELIANCE", "500325")])

    class FailingLoader(_FakeDailyLoader):
        def get_daily_history(self, *args, **kwargs):
            if failure == "loader":
                raise OSError("transient")
            return super().get_daily_history(*args, **kwargs)

    if failure == "malformed":
        frame.loc[1, "open"] = None
    compute_pending_forward_returns(session_factory, FailingLoader({"RELIANCE": frame}),
                                   universe_loader=universe, **options)
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn).where(SignalForwardReturn.horizon_days == 1)).one()
        assert tuple(getattr(row, c.name) for c in row.__table__.columns) == before


def test_provider_calls_allow_independent_writer_and_concurrent_terminalization(file_session_factory):
    """A concurrent writer commits during both providers; its receipt wins.

    Beginner note:
        This uses the production-like file fixture and a genuinely independent
        session. An encompassing write transaction would lock the writer during
        the second fetch. A Python-only terminal check would overwrite its +9%.
    """
    from backend.storage.repository import upsert_forward_return
    from backend.validation.forward_return import ForwardReturnPoint

    result_id = _seed_signal(file_session_factory)
    other_id = _seed_signal(file_session_factory)
    frame = _candles([("2026-01-05", "90", "95", "88", "92"),
                      ("2026-01-06", "100", "106", "98", "104")])
    writes = []

    class WritingLoader(_FakeDailyLoader):
        def get_daily_history(self, instrument, start_date, end_date, force_refresh=False, **_kwargs):
            with file_session_factory() as writer:
                target = result_id if instrument["symbol"] == "RELIANCE" else other_id
                upsert_forward_return(writer, result_id=target, point=ForwardReturnPoint(
                    horizon_days=1, status=ForwardReturnStatus.COMPUTED,
                    forward_return_pct=Decimal("9"),
                ))
            writes.append(instrument["symbol"])
            return super().get_daily_history(instrument, start_date, end_date, force_refresh)

    compute_pending_forward_returns(
        file_session_factory, WritingLoader({"RELIANCE": frame, "INDEX": frame}),
        as_of=dt.date(2026, 1, 8), horizons=(1,), limit=1,
        universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _: BenchmarkSpec(key="index", symbol="INDEX", security_id="13"),
    )
    assert writes == ["RELIANCE", "INDEX"]
    with file_session_factory() as session:
        rows = session.scalars(select(SignalForwardReturn)).all()
        assert len(rows) == 2
        assert all(row.forward_return_pct == Decimal("9") for row in rows)


def test_later_signal_failure_rolls_back_all_its_horizons_preserving_previous_commit(
    file_session_factory, monkeypatch,
):
    """A failure on the second horizon rolls back that signal, not the batch.

    Beginner note:
        Counting before commit would falsely report the rolled-back signal;
        committing horizon by horizon would leave half a signal persisted.
    """
    from backend.validation import service

    first = _seed_signal(file_session_factory)
    second = _seed_signal(file_session_factory)
    original = service.upsert_forward_return

    def fail_second_horizon(session, **kwargs):
        if kwargs["result_id"] == second and kwargs["point"].horizon_days == 20:
            raise RuntimeError("injected write failure")
        return original(session, **kwargs)

    monkeypatch.setattr(service, "upsert_forward_return", fail_second_horizon)
    with pytest.raises(service.ForwardReturnBatchError) as caught:
        compute_pending_forward_returns(
            file_session_factory, _FakeDailyLoader({}), horizons=(1, 20),
            universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
        )
    assert caught.value.summary.total_signals == 1
    assert caught.value.summary.pending == 2
    with file_session_factory() as session:
        rows = session.scalars(select(SignalForwardReturn).order_by(SignalForwardReturn.id)).all()
        assert [row.result_id for row in rows] == [first, first, second, second]
        # The failed signal keeps only attempt receipts: no measurement facts.
        failed_rows = [row for row in rows if row.result_id == second]
        assert all(row.status is ForwardReturnStatus.PENDING for row in failed_rows)
        assert all(row.last_attempted_at is not None and row.entry_date is None for row in failed_rows)


def test_limit_one_rotates_pending_signals_across_invocations(session_factory):
    """Repeated transient failures must give the second signal its next turn."""
    first = _seed_signal(session_factory)
    second = _seed_signal(session_factory)
    for _ in range(2):
        result = compute_pending_forward_returns(
            session_factory, _FakeDailyLoader({}), horizons=(1, 20), limit=1,
            universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
        )
        assert result.total_signals == 1
    with session_factory() as session:
        assert set(session.scalars(select(SignalForwardReturn.result_id))) == {first, second}


@pytest.mark.parametrize("configured", [True, False])
def test_benchmark_retry_stays_pending_for_malformed_data_or_clears_if_unconfigured(session_factory, configured):
    """Missing index data retries; intentionally absent configuration exits queue."""
    from backend.storage.repository import upsert_forward_return
    from backend.validation.forward_return import ForwardReturnPoint

    result_id = _seed_signal(session_factory)
    with session_factory() as session:
        upsert_forward_return(session, result_id=result_id, point=ForwardReturnPoint(
            horizon_days=1, status=ForwardReturnStatus.COMPUTED,
            entry_date=dt.date(2026, 1, 6), exit_date=dt.date(2026, 1, 6),
            forward_return_pct=Decimal("4"),
        ), benchmark_retry_pending=True)
    malformed = _candles([("2026-01-06", "100", "106", "98", "104"),
                          ("bad-date", "100", "106", "98", "104")])
    compute_pending_forward_returns(
        session_factory, _FakeDailyLoader({"INDEX": malformed}), horizons=(1,),
        as_of=dt.date(2026, 1, 8), universe_loader=lambda _: pytest.fail("stock fetched"),
        benchmark_resolver=lambda _: BenchmarkSpec(key="index", symbol="INDEX", security_id="13")
            if configured else None,
    )
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.forward_return_pct == Decimal("4")
        assert row.benchmark_return_pct is None
        assert row.benchmark_retry_pending is configured


@pytest.mark.parametrize("entry,exit_", [(None, None), (dt.date(2026, 1, 6), dt.date(2026, 2, 1))])
def test_configured_benchmark_with_unusable_or_future_stock_dates_stays_retryable(session_factory, entry, exit_):
    """Unusable stored dates cannot prove benchmark success or no configuration.

    Beginner note:
        Legacy computed stock rows may lack dates, and an as-of replay may stop
        before an already measured exit. Neither case permits fetching a future
        window or treating configured benchmark work as deliberately absent.
    """
    from backend.storage.repository import upsert_forward_return
    from backend.validation.forward_return import ForwardReturnPoint

    result_id = _seed_signal(session_factory)
    with session_factory() as session:
        upsert_forward_return(session, result_id=result_id, point=ForwardReturnPoint(
            horizon_days=1, status=ForwardReturnStatus.COMPUTED,
            entry_date=entry, exit_date=exit_, forward_return_pct=Decimal("4"),
        ), benchmark_retry_pending=True)
    loader = _FakeDailyLoader({})
    compute_pending_forward_returns(
        session_factory, loader, horizons=(1,), as_of=dt.date(2026, 1, 8),
        benchmark_resolver=lambda _: BenchmarkSpec(key="index", symbol="INDEX", security_id="13"),
    )
    assert not loader.calls
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.benchmark_retry_pending


@pytest.mark.parametrize("source", ["vendor", "cache"])
@pytest.mark.parametrize("bad_field", ["open", "timestamp"])
def test_real_loader_preserves_malformed_stock_for_service_and_calculator(
    session_factory, tmp_path, source, bad_field,
):
    """The real normalizer and range slicer must not fabricate a later entry.

    Beginner note:
        Only the SDK network response is replaced. Previously Jan 6's null open
        or invalid timestamp vanished before validation, making Jan 7 a false
        one-day entry. The same evidence must survive a Parquet round trip so
        the worker retries and both calculator and scanner reject the frame.
    """
    from types import SimpleNamespace

    from backend.daily_data_loader import DailyDataLoader
    from backend.dhan_client import DhanDataClient
    from backend.validation.forward_return import compute_forward_return

    _seed_signal(session_factory)
    raw = _candles([
        ("2026-01-05", "90", "95", "88", "92"),
        ("2026-01-06", "100", "106", "98", "104"),
        ("2026-01-07", "100", "110", "98", "107"),
    ])
    raw.loc[1, bad_field] = None if bad_field == "open" else "bad-date"
    network_calls = []

    def network(**kwargs):
        network_calls.append(kwargs)
        return {"status": "success", "data": raw.to_dict("records")}

    loader = DailyDataLoader(
        DhanDataClient(raw_client=SimpleNamespace(historical_daily_data=network)),
        cache_dir=tmp_path, request_delay_seconds=0, fetch_workers=1,
    )
    if source == "cache":
        raw.to_parquet(loader.cache_path("RELIANCE", "500325"), index=False)
    universe = _universe([("RELIANCE", "500325")])
    summary = compute_pending_forward_returns(
        session_factory, loader, horizons=(1,), as_of=dt.date(2026, 1, 7),
        universe_loader=lambda _: universe, benchmark_resolver=lambda _: None,
    )
    assert summary.pending == 1
    assert summary.computed == 0
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.status is ForwardReturnStatus.PENDING
        assert row.entry_date is None
        assert row.forward_return_pct is None
    raw_view, from_cache = loader.get_daily_history(
        universe.iloc[0], dt.date(2026, 1, 5), dt.date(2026, 1, 7), preserve_malformed_rows=True,
    )
    assert from_cache
    assert len(raw_view) == 3
    point = compute_forward_return(raw_view, dt.date(2026, 1, 5), 1, as_of=dt.date(2026, 1, 7))
    assert point.status is ForwardReturnStatus.INSUFFICIENT_DATA
    # Owner decision: only validation opts in to raw rows. Scans strip the
    # malformed row exactly as before VALID-005 instead of quarantining the
    # whole symbol from every screener.
    clean_view, _ = loader.get_daily_history(universe.iloc[0], dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    assert len(clean_view) == 2
    scan = loader.load_universe_history(universe, dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    assert "RELIANCE" in scan.frames
    assert not scan.failures
    assert len(network_calls) == (1 if source == "vendor" else 0)


@pytest.mark.parametrize("source", ["vendor", "cache"])
def test_real_loader_keeps_malformed_benchmark_retryable(session_factory, tmp_path, source):
    """A bad raw index row must remain visible even when both aligned dates exist.

    Beginner note:
        Dropping an invalid extra timestamp used to make the benchmark look
        complete. Stock measurements may finish, but that fabricated index leg
        must stay null and retryable through real normalization and cache slicing.
    """
    from types import SimpleNamespace

    from backend.daily_data_loader import DailyDataLoader
    from backend.dhan_client import DhanDataClient
    from backend.validation.benchmarks import compute_benchmark_leg

    _seed_signal(session_factory)
    stock = _candles([
        ("2026-01-05", "90", "95", "88", "92"),
        ("2026-01-06", "100", "106", "98", "104"),
        ("2026-01-07", "100", "110", "98", "107"),
    ])
    index = pd.concat([stock, stock.iloc[[1]].assign(timestamp="bad-date")], ignore_index=True)

    def network(**kwargs):
        frame = stock if kwargs["security_id"] == "500325" else index
        return {"status": "success", "data": frame.to_dict("records")}

    loader = DailyDataLoader(
        DhanDataClient(raw_client=SimpleNamespace(historical_daily_data=network)),
        cache_dir=tmp_path, request_delay_seconds=0, fetch_workers=1,
    )
    if source == "cache":
        stock.to_parquet(loader.cache_path("RELIANCE", "500325"), index=False)
        index.to_parquet(loader.cache_path("INDEX", "13"), index=False)
    spec = BenchmarkSpec(key="index", symbol="INDEX", security_id="13")
    summary = compute_pending_forward_returns(
        session_factory, loader, horizons=(2,), as_of=dt.date(2026, 1, 7),
        universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
        benchmark_resolver=lambda _: spec,
    )
    assert summary.computed == 1
    assert summary.benchmark_missing == 1
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.benchmark_retry_pending
        assert row.benchmark_return_pct is None
    cached, from_cache = loader.get_daily_history(
        spec.instrument, dt.date(2026, 1, 5), dt.date(2026, 1, 7), preserve_malformed_rows=True,
    )
    assert from_cache
    leg = compute_benchmark_leg(cached, entry_date=dt.date(2026, 1, 6),
                                exit_date=dt.date(2026, 1, 7), benchmark_key="index")
    assert leg.return_pct is None


def _poisoned_upsert(monkeypatch, poison_id: int) -> None:
    """Make every stock write for one signal fail deterministically."""
    from backend.validation import service

    original = service.upsert_forward_return

    def fail_poison(session, **kwargs):
        if kwargs["result_id"] == poison_id:
            raise RuntimeError("deterministic write failure")
        return original(session, **kwargs)

    monkeypatch.setattr(service, "upsert_forward_return", fail_poison)


def test_one_failing_signal_does_not_stop_the_rest_of_the_batch(session_factory, monkeypatch):
    """A single poison signal used to abort every later signal in the batch."""
    from backend.validation import service

    poison = _seed_signal(session_factory)
    healthy = _seed_signal(session_factory)
    _poisoned_upsert(monkeypatch, poison)

    with pytest.raises(service.ForwardReturnBatchError) as caught:
        compute_pending_forward_returns(
            session_factory, _FakeDailyLoader({}), horizons=(1,),
            universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
            benchmark_resolver=lambda _: None,
        )
    assert caught.value.summary.total_signals == 1
    with session_factory() as session:
        assert healthy in set(session.scalars(select(SignalForwardReturn.result_id)))


def test_failing_signal_is_recorded_as_attempted_so_the_queue_moves_on(session_factory, monkeypatch):
    """Oldest-attempt-first selection must not pick the same poison forever.

    Beginner note:
        The failed signal's transaction rolls back, so without a separately
        committed attempt time it stayed the oldest work item and a limited
        batch re-selected it (and failed) on every run, starving the queue.
    """
    from backend.validation import service

    poison = _seed_signal(session_factory)
    healthy = _seed_signal(session_factory)
    _poisoned_upsert(monkeypatch, poison)
    options = dict(horizons=(1,), limit=1, benchmark_resolver=lambda _: None,
                   universe_loader=lambda _: _universe([("RELIANCE", "500325")]))

    with pytest.raises(service.ForwardReturnBatchError):
        compute_pending_forward_returns(session_factory, _FakeDailyLoader({}), **options)
    summary = compute_pending_forward_returns(session_factory, _FakeDailyLoader({}), **options)

    assert summary.total_signals == 1
    with session_factory() as session:
        stored = {row.result_id: row for row in session.scalars(select(SignalForwardReturn))}
        assert healthy in stored
        assert stored[poison].status is ForwardReturnStatus.PENDING
        assert stored[poison].last_attempted_at is not None


def test_benchmark_retry_finalizes_after_grace_period(session_factory):
    """A configured index that never yields a leg cannot stay queued forever."""
    from backend.storage.repository import upsert_forward_return
    from backend.validation.forward_return import ForwardReturnPoint

    result_id = _seed_signal(session_factory)
    with session_factory() as session:
        upsert_forward_return(session, result_id=result_id, point=ForwardReturnPoint(
            horizon_days=1, status=ForwardReturnStatus.COMPUTED,
            entry_date=dt.date(2026, 1, 6), exit_date=dt.date(2026, 1, 6),
            forward_return_pct=Decimal("4"),
        ), benchmark_retry_pending=True, attempted_at=dt.datetime(2026, 1, 7, tzinfo=dt.UTC))
    summary = compute_pending_forward_returns(
        session_factory, _FakeDailyLoader({}), horizons=(1,), as_of=dt.date(2026, 2, 1),
        universe_loader=lambda _: pytest.fail("stock fetched"),
        benchmark_resolver=lambda _: BenchmarkSpec(key="index", symbol="INDEX", security_id="13"),
    )
    assert summary.benchmark_missing == 1
    with session_factory() as session:
        row = session.scalars(select(SignalForwardReturn)).one()
        assert row.benchmark_retry_pending is False
        assert row.benchmark_return_pct is None
        assert row.forward_return_pct == Decimal("4")


def test_service_validates_each_loaded_frame_once(session_factory, monkeypatch):
    """The loader boundary validates; the calculator must not repeat it per horizon."""
    from backend.validation import forward_return, service

    _seed_signal(session_factory)
    calls: list[str] = []
    for module in (service, forward_return):
        original = module.validate_candles

        def counting(*args, _original=original, _name=module.__name__, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)

        monkeypatch.setattr(module, "validate_candles", counting)
    frame = _candles([("2026-01-05", "90", "95", "88", "92"), ("2026-01-06", "100", "106", "98", "104"),
                      ("2026-01-07", "100", "110", "98", "107"), ("2026-01-08", "100", "110", "98", "108")])
    compute_pending_forward_returns(
        session_factory, _FakeDailyLoader({"RELIANCE": frame}), horizons=(1, 2, 3),
        as_of=dt.date(2026, 1, 8), benchmark_resolver=lambda _: None,
        universe_loader=lambda _: _universe([("RELIANCE", "500325")]),
    )
    assert len(calls) == 1

def test_loader_strips_malformed_rows_unless_validation_opts_in(tmp_path):
    """Owner decision: only forward-return validation sees raw malformed rows.

    Beginner note:
        The cache keeps raw vendor rows so validation can refuse to shift an
        entry day. Scans, charts and ranking read the same cache, and a single
        bad row there used to quarantine the symbol from every screener, so
        their default view strips malformed rows exactly as before VALID-005.
    """
    from types import SimpleNamespace

    from backend.daily_data_loader import DailyDataLoader
    from backend.dhan_client import DhanDataClient

    loader = DailyDataLoader(
        DhanDataClient(raw_client=SimpleNamespace(historical_daily_data=lambda **_: pytest.fail("no network"))),
        cache_dir=tmp_path, request_delay_seconds=0, fetch_workers=1,
    )
    raw = _candles([
        ("2026-01-05", "90", "95", "88", "92"),
        ("2026-01-06", "100", "106", "98", "104"),
        ("2026-01-06", "100", "106", "98", "104"),
        ("2026-01-07", "100", "110", "98", "107"),
    ])
    raw.loc[1, "open"] = None
    raw.loc[2, "timestamp"] = "bad-date"
    raw.to_parquet(loader.cache_path("RELIANCE", "500325"), index=False)
    instrument = {"symbol": "RELIANCE", "security_id": "500325"}

    clean, hit = loader.get_daily_history(instrument, dt.date(2026, 1, 5), dt.date(2026, 1, 7))
    raw_view, _ = loader.get_daily_history(
        instrument, dt.date(2026, 1, 5), dt.date(2026, 1, 7), preserve_malformed_rows=True,
    )

    assert hit and len(clean) == 2
    assert len(raw_view) == 4
    assert len(loader.read_cached_history("RELIANCE", "500325")) == 2
    assert len(loader.read_cached_history("RELIANCE", "500325", preserve_malformed_rows=True)) == 4
