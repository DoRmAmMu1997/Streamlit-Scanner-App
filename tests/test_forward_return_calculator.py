"""VALID-002 forward-return calculator tests.

These tests are deliberately pure: no database, no network, no Streamlit. They
lock the trading-day math and benchmark alignment before the service wiring is
allowed to exist.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd

from backend.storage.models import ForwardReturnStatus
from backend.validation.benchmarks import compute_benchmark_leg
from backend.validation.forward_return import compute_forward_return


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


def test_compute_forward_return_uses_next_open_nth_close_and_path_metrics():
    frame = _candles(
        [
            ("2026-01-05", "90.00", "95.00", "88.00", "92.00"),
            ("2026-01-06", "100.00", "106.00", "98.00", "104.00"),
            ("2026-01-07", "105.00", "120.00", "95.00", "110.00"),
            # Deliberate calendar gap: the horizon is counted by bar position,
            # not by calendar days.
            ("2026-01-09", "111.00", "118.00", "99.00", "115.00"),
        ]
    )

    point = compute_forward_return(
        frame,
        dt.date(2026, 1, 5),
        3,
        as_of=dt.date(2026, 1, 10),
    )

    assert point.status is ForwardReturnStatus.COMPUTED
    assert point.entry_date == dt.date(2026, 1, 6)
    assert point.exit_date == dt.date(2026, 1, 9)
    assert point.entry_price == Decimal("100.0000")
    assert point.exit_price == Decimal("115.0000")
    assert point.forward_return_pct == Decimal("15.0000")
    assert point.max_adverse_excursion_pct == Decimal("-5.0000")
    assert point.max_favorable_excursion_pct == Decimal("20.0000")


def test_compute_forward_return_stays_pending_until_as_of_reaches_exit_date():
    frame = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-06", "100", "106", "98", "104"),
            ("2026-01-07", "105", "120", "95", "110"),
            ("2026-01-09", "111", "118", "99", "115"),
        ]
    )

    point = compute_forward_return(
        frame,
        dt.date(2026, 1, 5),
        3,
        as_of=dt.date(2026, 1, 8),
    )

    assert point.status is ForwardReturnStatus.PENDING
    assert point.forward_return_pct is None
    assert point.entry_price is None
    assert point.exit_price is None


def test_compute_forward_return_distinguishes_recent_and_stale_missing_future_data():
    frame = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-06", "100", "106", "98", "104"),
        ]
    )

    recent = compute_forward_return(
        frame,
        dt.date(2026, 1, 5),
        3,
        as_of=dt.date(2026, 1, 8),
    )
    stale = compute_forward_return(
        frame,
        dt.date(2026, 1, 5),
        3,
        as_of=dt.date(2026, 2, 1),
    )

    assert recent.status is ForwardReturnStatus.PENDING
    assert stale.status is ForwardReturnStatus.INSUFFICIENT_DATA


def test_compute_forward_return_marks_absent_signal_date_insufficient():
    point = compute_forward_return(
        _candles(
            [
                ("2026-01-06", "100", "106", "98", "104"),
                ("2026-01-07", "105", "120", "95", "110"),
            ]
        ),
        dt.date(2026, 1, 5),
        1,
        as_of=dt.date(2026, 1, 8),
    )

    assert point.status is ForwardReturnStatus.INSUFFICIENT_DATA


def test_compute_benchmark_leg_aligns_by_entry_and_exit_dates():
    leg = compute_benchmark_leg(
        _candles(
            [
                ("2026-01-05", "190", "195", "188", "192"),
                ("2026-01-06", "200", "206", "198", "204"),
                ("2026-01-09", "210", "222", "205", "220"),
            ]
        ),
        entry_date=dt.date(2026, 1, 6),
        exit_date=dt.date(2026, 1, 9),
        benchmark_key="nifty_test",
    )

    assert leg.benchmark_key == "nifty_test"
    assert leg.entry_price == Decimal("200.0000")
    assert leg.exit_price == Decimal("220.0000")
    assert leg.return_pct == Decimal("10.0000")


def test_compute_benchmark_leg_keeps_key_and_nulls_prices_when_dates_are_missing():
    leg = compute_benchmark_leg(
        _candles([("2026-01-06", "200", "206", "198", "204")]),
        entry_date=dt.date(2026, 1, 6),
        exit_date=dt.date(2026, 1, 9),
        benchmark_key="nifty_test",
    )

    assert leg.benchmark_key == "nifty_test"
    assert leg.entry_price is None
    assert leg.exit_price is None
    assert leg.return_pct is None
