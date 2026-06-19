"""VALID-002 service tests for filling signal_forward_returns."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal
from typing import Any

import pandas as pd
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
    ) -> tuple[pd.DataFrame, bool]:
        del force_refresh
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


def _seed_signal(db_session, *, universe_key: str = "nifty_500"):
    run = create_scan_run(
        db_session,
        screener_key="envelope_knoxville_buy",
        universe_key=universe_key,
        data_snapshot_date=dt.date(2026, 1, 10),
    )
    [result] = save_scan_results(
        db_session,
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
    db_session.commit()
    return result


def test_service_upserts_forward_return_and_benchmark_without_duplicates(db_session):
    result = _seed_signal(db_session)
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
        db_session,
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
    db_session.commit()

    assert summary.computed == 1
    assert summary.pending == 0
    assert summary.insufficient == 0
    row = db_session.scalars(select(SignalForwardReturn)).one()
    assert row.result_id == result.id
    assert row.status is ForwardReturnStatus.COMPUTED
    assert row.forward_return_pct == Decimal("15.0000")
    assert row.benchmark_key == "nifty_test"
    assert row.benchmark_return_pct == Decimal("10.0000")
    assert row.excess_return_pct == Decimal("5.0000")

    second = compute_pending_forward_returns(
        db_session,
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
    db_session.commit()

    assert second.total_signals == 0
    assert db_session.scalar(select(func.count()).select_from(SignalForwardReturn)) == 1


def test_service_computes_stock_return_when_benchmark_is_unresolved(db_session):
    _seed_signal(db_session, universe_key="fno")
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
        db_session,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )
    db_session.commit()

    assert summary.computed == 1
    row = db_session.scalars(select(SignalForwardReturn)).one()
    assert row.forward_return_pct == Decimal("15.0000")
    assert row.benchmark_key is None
    assert row.benchmark_return_pct is None
    assert row.excess_return_pct is None


def test_service_marks_missing_symbol_mapping_insufficient_without_loading_data(db_session):
    _seed_signal(db_session)
    loader = _FakeDailyLoader({})

    summary = compute_pending_forward_returns(
        db_session,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("TCS", "532540")]),
    )
    db_session.commit()

    assert summary.insufficient == 1
    assert loader.calls == []
    row = db_session.scalars(select(SignalForwardReturn)).one()
    assert row.status is ForwardReturnStatus.INSUFFICIENT_DATA
    assert row.forward_return_pct is None


def test_service_recomputes_pending_signal_to_computed_on_later_run(db_session):
    """A window that has not elapsed yet stays PENDING, then upserts to COMPUTED.

    This is the retryability contract end-to-end: the first pass cannot see the
    exit bar's date as having passed (no lookahead), so it records PENDING; a later
    pass — once ``as_of`` reaches the exit date — re-selects that same pending row
    (terminal rows would be skipped) and updates it in place, never duplicating.
    """
    _seed_signal(db_session)
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
        db_session,
        loader,
        as_of=dt.date(2026, 1, 7),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )
    db_session.commit()

    assert first.pending == 1
    assert first.computed == 0
    pending_row = db_session.scalars(select(SignalForwardReturn)).one()
    assert pending_row.status is ForwardReturnStatus.PENDING
    assert pending_row.forward_return_pct is None
    assert pending_row.computed_at is None

    # Second pass: as_of now past the exit date — the same row flips to COMPUTED.
    second = compute_pending_forward_returns(
        db_session,
        loader,
        as_of=dt.date(2026, 1, 10),
        horizons=(3,),
        universe_loader=lambda _key: _universe([("RELIANCE", "500325")]),
    )
    db_session.commit()

    assert second.total_signals == 1  # the pending row was retryable, so re-selected
    assert second.computed == 1
    assert db_session.scalar(select(func.count()).select_from(SignalForwardReturn)) == 1
    computed_row = db_session.scalars(select(SignalForwardReturn)).one()
    assert computed_row.id == pending_row.id  # updated in place, not a new row
    assert computed_row.status is ForwardReturnStatus.COMPUTED
    assert computed_row.forward_return_pct == Decimal("15.0000")
    assert computed_row.computed_at is not None


def test_service_keeps_signal_pending_and_retryable_when_universe_cannot_load(db_session):
    """A missing/corrupt universe is an environment fault → PENDING, not terminal.

    Contrast with the symbol-missing case above (INSUFFICIENT_DATA, terminal): there
    the universe loaded and simply had no row for the symbol. Here the universe load
    itself fails, which must not permanently brand every signal of that universe as
    un-measurable — it stays PENDING so a later run can still compute it. No candle
    fetch is attempted because there is no instrument to fetch.
    """
    _seed_signal(db_session)
    loader = _FakeDailyLoader({})

    def _broken_universe(_key: str) -> pd.DataFrame:
        raise FileNotFoundError("universe CSV not generated yet")

    summary = compute_pending_forward_returns(
        db_session,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(20, 60),
        universe_loader=_broken_universe,
    )
    db_session.commit()

    assert summary.pending == 2  # one row per horizon
    assert summary.insufficient == 0
    assert loader.calls == []  # nothing to fetch without an instrument
    statuses = db_session.scalars(select(SignalForwardReturn.status)).all()
    assert set(statuses) == {ForwardReturnStatus.PENDING}

    # Retryable: the pending rows are still selected on a later pass.
    retry = compute_pending_forward_returns(
        db_session,
        loader,
        as_of=dt.date(2026, 2, 1),
        horizons=(20, 60),
        universe_loader=_broken_universe,
    )
    db_session.commit()
    assert retry.total_signals == 1
