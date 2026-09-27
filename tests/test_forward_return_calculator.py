"""VALID-002 forward-return calculator tests.

These tests are deliberately pure: no database, no network, no Streamlit. They
lock the trading-day math and benchmark alignment before the service wiring is
allowed to exist.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pandas as pd
import pytest

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


@pytest.mark.parametrize(
    ("mutate", "description"),
    [
        (lambda frame: frame.assign(timestamp=["2026-01-05", "not-a-date"]), "invalid timestamp"),
        (lambda frame: frame.assign(open=["90", "NaN"]), "non-finite open"),
        (lambda frame: frame.assign(high=["95", "80"]), "high below low"),
        (lambda frame: frame.assign(close=["92", "120"]), "close outside range"),
    ],
)
def test_compute_forward_return_rejects_malformed_raw_rows_before_preparation(mutate, description):
    """Malformed raw bars cannot disappear or shift the measured holding window.

    Beginner note:
    The old preparation helper silently dropped a bad timestamp and coerced bad
    prices. That could move the "next" row from January 6 to January 7 and still
    produce a confident-looking return. This test requires a terminal unavailable
    result before sorting, dropping, or deduplicating can hide the bad source row.
    """
    frame = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-06", "100", "106", "98", "104"),
        ]
    )

    point = compute_forward_return(
        mutate(frame),
        dt.date(2026, 1, 5),
        1,
        as_of=dt.date(2026, 1, 8),
    )

    assert point.status is ForwardReturnStatus.INSUFFICIENT_DATA, description


def test_compute_forward_return_rejects_conflicting_daily_duplicates():
    """Reject competing OHLC facts before preparation chooses a duplicate.

    Beginner note:
        If deduplication runs first, a conflicting entry price disappears and a fabricated return becomes
        COMPUTED.
    """
    frame = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-06", "100", "106", "98", "104"),
            ("2026-01-06", "101", "107", "99", "105"),
        ]
    )

    point = compute_forward_return(frame, dt.date(2026, 1, 5), 1, as_of=dt.date(2026, 1, 8))

    assert point.status is ForwardReturnStatus.INSUFFICIENT_DATA


def test_compute_forward_return_accepts_ohlc_without_volume_and_holiday_gaps():
    """Accept price-only evidence and count actual trading bars across holidays.

    Beginner note:
        Requiring volume or filling missing calendar dates would incorrectly reject valid OHLC or change the
        requested holding period.
    """
    frame = _candles(
        [
            ("2026-01-05", "90", "95", "88", "92"),
            ("2026-01-09", "100", "106", "98", "104"),
        ]
    ).drop(columns="volume")

    point = compute_forward_return(frame, dt.date(2026, 1, 5), 1, as_of=dt.date(2026, 1, 10))

    assert point.status is ForwardReturnStatus.COMPUTED
    assert point.entry_date == dt.date(2026, 1, 9)


@pytest.mark.parametrize("horizon", [True, False, 0, -1, 1.5, Decimal("2.0")])
def test_compute_forward_return_rejects_non_positive_non_integral_horizons(horizon):
    """Reject mistaken horizon values without silent integer coercion.

    Beginner note:
        Bool is an int subclass and int(1.5) truncates; either acceptance would measure a different period
        than the caller requested.
    """
    with pytest.raises(ValueError, match="positive integer"):
        compute_forward_return(
            _candles([("2026-01-05", "90", "95", "88", "92")]),
            dt.date(2026, 1, 5),
            horizon,
        )


def test_identical_daily_rows_with_distinct_times_count_as_one_trading_bar():
    """Count equivalent intraday timestamps as one daily trading observation.

    Beginner note:
        Timestamp-level deduplication alone used to count January 6 twice and
        move the two-bar exit from January 7 back to January 6. This assertion
        protects the calendar-date canonicalization after raw validation.
    """
    frame = pd.DataFrame([
        {"timestamp": "2026-01-05 00:00", "open": 100, "high": 110, "low": 90, "close": 104},
        {"timestamp": "2026-01-06 00:00", "open": 100, "high": 110, "low": 90, "close": 104},
        {"timestamp": "2026-01-06 09:00", "open": 100, "high": 110, "low": 90, "close": 104},
        {"timestamp": "2026-01-07 00:00", "open": 100, "high": 120, "low": 90, "close": 115},
    ])
    point = compute_forward_return(frame, dt.date(2026, 1, 5), 2, as_of=dt.date(2026, 1, 8))
    assert point.exit_date == dt.date(2026, 1, 7)
    assert point.forward_return_pct == Decimal("15")
