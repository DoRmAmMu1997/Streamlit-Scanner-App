"""Tests for translating Dhan responses into app-friendly candle tables."""

from __future__ import annotations

import pandas as pd
import pytest

from backend.dhan_client import DhanRateLimitError, normalize_daily_payload, normalize_daily_response


def test_normalize_daily_response_accepts_dict_of_arrays():
    # Dhan commonly returns a dictionary where each key is a candle field and
    # each value is a list of values for that field.
    response = {
        "status": "success",
        "data": {
            "start_Time": ["2026-05-10", "2026-05-11"],
            "open": ["100"],
            "high": ["110"],
            "low": ["95"],
            "close": ["108"],
            "volume": ["12345"],
        },
    }

    # Dhan usually returns equal-length arrays. This test intentionally uses a
    # smaller payload below so pandas catches shape problems if we break setup.
    response["data"]["open"].append("108")
    response["data"]["high"].append("112")
    response["data"]["low"].append("104")
    response["data"]["close"].append("111")
    response["data"]["volume"].append("13000")

    df = normalize_daily_response(response)

    # The rest of the app depends on this exact standard column order.
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]
    assert len(df) == 2
    assert df["close"].tolist() == [108, 111]
    assert pd.api.types.is_datetime64_any_dtype(df["timestamp"])


def test_normalize_daily_payload_accepts_list_of_rows():
    # Some clients/APIs return rows instead of columns. The normalizer supports
    # both so the rest of the code does not care which shape Dhan gives us.
    payload = [
        {"date": "2026-05-10", "open": 10, "high": 12, "low": 9, "close": 11, "volume": 1000},
        {"date": "2026-05-11", "open": 11, "high": 13, "low": 10, "close": 12, "volume": 1200},
    ]

    df = normalize_daily_payload(payload)

    assert len(df) == 2
    assert df.iloc[-1]["close"] == 12


def test_normalize_daily_response_returns_empty_for_no_data():
    # No-data responses are normal for some date ranges. They should not crash
    # the whole scanner run.
    response = {"status": "failure", "remarks": "No data found"}

    df = normalize_daily_response(response)

    assert df.empty
    assert list(df.columns) == ["timestamp", "open", "high", "low", "close", "volume"]


def test_normalize_daily_response_raises_rate_limit_for_dh904():
    # Dhan can send the rate-limit details inside `remarks`. The scanner should
    # identify this as a retryable rate-limit problem instead of a generic error.
    response = {
        "status": "failure",
        "remarks": {
            "error_code": "DH-904",
            "error_type": "Rate_Limit",
            "error_message": "Too many requests on server from single user.",
        },
    }

    with pytest.raises(DhanRateLimitError):
        normalize_daily_response(response)


def test_normalize_daily_response_raises_rate_limit_for_error_type_text():
    # Keep the detector tolerant because SDK versions may move the error text
    # between top-level fields, remarks, message, or data.
    response = {
        "status": "failure",
        "message": "Rate_Limit: Try throttling API calls.",
    }

    with pytest.raises(DhanRateLimitError):
        normalize_daily_response(response)
