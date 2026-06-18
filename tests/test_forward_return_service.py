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
