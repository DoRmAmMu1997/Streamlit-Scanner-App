"""Tests for reusable candle-data quality validation (DATA-001A)."""

from __future__ import annotations

from datetime import date

import pandas as pd

from backend.data_quality.candles import validate_candles


def _valid_candles() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-03"]),
            "open": [100.0, 102.0, 103.0],
            "high": [105.0, 106.0, 107.0],
            "low": [99.0, 101.0, 102.0],
            "close": [104.0, 103.5, 106.0],
            "volume": [1_000.0, 1_100.0, 1_200.0],
        }
    )


def _finding_codes(report) -> list[str]:
    return [finding.code for finding in report.findings]


def test_valid_candles_return_usable_report_without_findings():
    report = validate_candles(_valid_candles(), symbol="RELIANCE")

    assert report.symbol == "RELIANCE"
    assert report.row_count == 3
    assert report.latest_date == date(2026, 6, 3)
    assert report.findings == ()
    assert report.is_usable
    assert not report.has_fatal_findings


def test_validator_accepts_date_column_and_datetime_index():
    with_date_column = _valid_candles().rename(columns={"timestamp": "date"})
    indexed = _valid_candles().set_index("timestamp")

    assert validate_candles(with_date_column, symbol="DATE").latest_date == date(2026, 6, 3)
    assert validate_candles(indexed, symbol="INDEX").latest_date == date(2026, 6, 3)


def test_empty_dataframe_is_fatal():
    report = validate_candles(pd.DataFrame(), symbol="EMPTY")

    assert report.row_count == 0
    assert _finding_codes(report) == ["EMPTY_FRAME"]
    assert report.findings[0].severity == "fatal"
    assert not report.is_usable


def test_missing_required_columns_are_fatal():
    frame = _valid_candles().drop(columns=["close", "volume"])

    report = validate_candles(frame, symbol="MISSING")

    assert _finding_codes(report) == ["MISSING_REQUIRED_COLUMNS"]
    assert report.findings[0].severity == "fatal"
    assert "close" in report.findings[0].message
    assert "volume" in report.findings[0].message


def test_missing_date_axis_is_fatal():
    frame = _valid_candles().drop(columns=["timestamp"])

    report = validate_candles(frame, symbol="NO_DATE")

    assert _finding_codes(report) == ["MISSING_DATE_AXIS"]
    assert report.findings[0].severity == "fatal"


def test_invalid_dates_are_fatal():
    frame = _valid_candles()
    frame["timestamp"] = frame["timestamp"].astype(object)
    frame.loc[1, "timestamp"] = "not-a-date"

    report = validate_candles(frame, symbol="BAD_DATE")

    assert _finding_codes(report) == ["INVALID_DATE"]
    assert report.findings[0].severity == "fatal"
    assert report.findings[0].affected_rows == 1


def test_duplicate_dates_are_fatal():
    frame = _valid_candles()
    frame.loc[2, "timestamp"] = frame.loc[1, "timestamp"]

    report = validate_candles(frame, symbol="DUPLICATE")

    assert _finding_codes(report) == ["DUPLICATE_DATE"]
    assert report.findings[0].severity == "fatal"
    assert report.findings[0].affected_rows == 2


def test_invalid_numeric_values_are_fatal():
    frame = _valid_candles()
    frame["open"] = frame["open"].astype(object)
    frame.loc[0, "open"] = "not-a-number"
    frame.loc[1, "high"] = float("nan")
    frame.loc[2, "volume"] = float("inf")

    report = validate_candles(frame, symbol="BAD_NUMBERS")

    assert _finding_codes(report) == ["INVALID_NUMERIC_VALUE"]
    assert report.findings[0].severity == "fatal"
    assert report.findings[0].affected_rows == 3


def test_high_lower_than_low_is_fatal():
    frame = _valid_candles()
    frame.loc[1, "high"] = 100.0

    report = validate_candles(frame, symbol="BAD_RANGE")

    assert _finding_codes(report) == ["HIGH_BELOW_LOW"]
    assert report.findings[0].severity == "fatal"
    assert report.findings[0].affected_rows == 1


def test_open_outside_low_high_range_is_fatal():
    above = _valid_candles()
    above.loc[0, "open"] = 106.0
    below = _valid_candles()
    below.loc[1, "open"] = 100.0

    assert _finding_codes(validate_candles(above, symbol="OPEN_ABOVE")) == ["OPEN_OUTSIDE_RANGE"]
    assert _finding_codes(validate_candles(below, symbol="OPEN_BELOW")) == ["OPEN_OUTSIDE_RANGE"]


def test_close_outside_low_high_range_is_fatal():
    above = _valid_candles()
    above.loc[0, "close"] = 106.0
    below = _valid_candles()
    below.loc[1, "close"] = 100.0

    assert _finding_codes(validate_candles(above, symbol="CLOSE_ABOVE")) == ["CLOSE_OUTSIDE_RANGE"]
    assert _finding_codes(validate_candles(below, symbol="CLOSE_BELOW")) == ["CLOSE_OUTSIDE_RANGE"]


def test_negative_volume_is_fatal():
    frame = _valid_candles()
    frame.loc[1, "volume"] = -1.0

    report = validate_candles(frame, symbol="NEGATIVE_VOLUME")

    assert _finding_codes(report) == ["NEGATIVE_VOLUME"]
    assert report.findings[0].severity == "fatal"
    assert report.findings[0].affected_rows == 1


def test_stale_latest_date_beyond_tolerance_is_warning():
    # Latest candle is 2026-06-03; expected 2026-06-10 is a 7-day gap (> 4).
    report = validate_candles(
        _valid_candles(),
        symbol="STALE",
        expected_latest_date=date(2026, 6, 10),
    )

    assert _finding_codes(report) == ["STALE_LATEST_CANDLE"]
    assert report.findings[0].severity == "warning"
    assert report.is_usable


def test_stale_within_tolerance_does_not_warn():
    # A normal long-weekend gap (Fri 06-03 data, Tue 06-07 run = 4 days) is fine.
    report = validate_candles(
        _valid_candles(),
        symbol="FRESH_ENOUGH",
        expected_latest_date=date(2026, 6, 7),
    )

    assert report.findings == ()


def test_stale_tolerance_zero_compares_exact_date():
    report = validate_candles(
        _valid_candles(),
        symbol="EXACT",
        expected_latest_date=date(2026, 6, 5),
        stale_tolerance_days=0,
    )

    assert _finding_codes(report) == ["STALE_LATEST_CANDLE"]


def test_calendar_gaps_over_seven_days_are_warnings():
    frame = _valid_candles()
    frame["timestamp"] = pd.to_datetime(["2026-06-01", "2026-06-02", "2026-06-12"])

    report = validate_candles(frame, symbol="GAP")

    assert _finding_codes(report) == ["CALENDAR_DATE_GAP"]
    assert report.findings[0].severity == "warning"
    assert report.findings[0].affected_rows == 1


def test_suspicious_overnight_price_gaps_over_fifty_percent_are_warnings():
    frame = _valid_candles()
    frame.loc[0, "close"] = 100.0
    frame.loc[1, "open"] = 151.0
    frame.loc[1, "high"] = 155.0
    frame.loc[1, "low"] = 150.0
    frame.loc[1, "close"] = 152.0

    report = validate_candles(frame, symbol="PRICE_GAP")

    assert _finding_codes(report) == ["SUSPICIOUS_OVERNIGHT_PRICE_GAP"]
    assert report.findings[0].severity == "warning"
    assert report.findings[0].affected_rows == 1


def test_gap_threshold_boundaries_do_not_warn():
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-01", "2026-06-08"]),
            "open": [100.0, 150.0],
            "high": [105.0, 151.0],
            "low": [99.0, 149.0],
            "close": [100.0, 150.0],
            "volume": [1_000.0, 1_100.0],
        }
    )

    report = validate_candles(frame, symbol="BOUNDARY")

    assert report.findings == ()


def test_input_dataframe_is_not_mutated():
    frame = _valid_candles()
    original = frame.copy(deep=True)

    validate_candles(frame, symbol="IMMUTABLE", expected_latest_date=date(2026, 6, 5))

    pd.testing.assert_frame_equal(frame, original)
