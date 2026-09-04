"""Tests for translating Dhan responses into app-friendly candle tables."""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import sys
import threading
import time
from types import SimpleNamespace

import pandas as pd
import pytest

from backend.config import DhanCredentials
from backend.dhan_client import (
    DhanDataClient,
    DhanRateLimitError,
    normalize_daily_payload,
    normalize_daily_response,
)


def _successful_daily_response():
    return {
        "status": "success",
        "data": {
            "start_Time": ["2026-05-10"],
            "open": [100.0],
            "high": [110.0],
            "low": [95.0],
            "close": [108.0],
            "volume": [12345.0],
        },
    }


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


def test_production_client_uses_one_sdk_transport_per_worker_thread(monkeypatch):
    """Each concurrent worker should own its own Dhan SDK HTTP transport."""
    first_wave = threading.Barrier(3)
    created_clients = []

    class FakeDhanContext:
        def __init__(self, client_code, access_token):
            self.client_code = client_code
            self.access_token = access_token

    class FakeSdkClient:
        def __init__(self, context):
            self.context = context
            self.thread_ids: set[int] = set()
            created_clients.append(self)

        def historical_daily_data(self, **kwargs):
            self.thread_ids.add(threading.get_ident())
            first_wave.wait(timeout=5)
            return _successful_daily_response()

    monkeypatch.setitem(
        sys.modules,
        "dhanhq",
        SimpleNamespace(DhanContext=FakeDhanContext, dhanhq=FakeSdkClient),
    )
    client = DhanDataClient(DhanCredentials("client-code", "access-token"))

    with concurrent.futures.ThreadPoolExecutor(max_workers=3) as executor:
        frames = list(
            executor.map(
                lambda security_id: client.fetch_daily_candles(
                    security_id,
                    "NSE_EQ",
                    "EQUITY",
                    "2026-05-10",
                    "2026-05-11",
                ),
                ("1", "2", "3"),
            )
        )

    used_clients = [sdk_client for sdk_client in created_clients if sdk_client.thread_ids]
    assert all(not frame.empty for frame in frames)
    assert client.dhan is created_clients[0]
    assert len(used_clients) == 3
    assert all(len(sdk_client.thread_ids) == 1 for sdk_client in used_clients)


def test_injected_raw_client_calls_are_serialized():
    """The compatibility raw-client seam must not be called concurrently."""

    class ConcurrencyProbeClient:
        def __init__(self):
            self._state_lock = threading.Lock()
            self.active_calls = 0
            self.max_active_calls = 0

        def historical_daily_data(self, **kwargs):
            with self._state_lock:
                self.active_calls += 1
                self.max_active_calls = max(self.max_active_calls, self.active_calls)
            time.sleep(0.02)
            with self._state_lock:
                self.active_calls -= 1
            return _successful_daily_response()

    raw_client = ConcurrencyProbeClient()
    client = DhanDataClient(raw_client=raw_client)

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
        frames = list(
            executor.map(
                lambda security_id: client.fetch_daily_candles(
                    security_id,
                    "NSE_EQ",
                    "EQUITY",
                    "2026-05-10",
                    "2026-05-11",
                ),
                ("1", "2", "3", "4"),
            )
        )

    assert client.dhan is raw_client
    assert all(not frame.empty for frame in frames)
    assert raw_client.max_active_calls == 1


# ---------------------------------------------------------------------------
# DATA-003: vendor duplicates must not reach the cache
# ---------------------------------------------------------------------------


def test_exact_duplicate_bars_are_collapsed():
    """DhanHQ sometimes repeats a bar verbatim; one copy carries all the info.

    Observed live for AEGISLOG on 2024-06-05 — two byte-identical rows. Persisting
    both makes the symbol fail DATA-001's DUPLICATE_DATE check and drops it from
    every scan, so the redundant copy is removed at the vendor boundary.
    """
    payload = [
        {"timestamp": "2024-06-04", "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5, "volume": 10},
        {"timestamp": "2024-06-05", "open": 700.0, "high": 730.1, "low": 664.75, "close": 705.45, "volume": 1122766},
        {"timestamp": "2024-06-05", "open": 700.0, "high": 730.1, "low": 664.75, "close": 705.45, "volume": 1122766},
    ]

    frame = normalize_daily_payload(payload)

    assert len(frame.index) == 2
    # No trading day may be lost to the dedupe.
    assert list(pd.to_datetime(frame["timestamp"]).dt.date) == [
        dt.date(2024, 6, 4),
        dt.date(2024, 6, 5),
    ]


def test_conflicting_bars_for_one_date_are_preserved():
    """Two *different* bars for one day must survive to be reported, not guessed at.

    Collapsing these would stitch together a price series that never existed. They
    stay so DATA-001 quarantines the symbol and the DATA-002 repair resolves them
    against the vendor instead.
    """
    payload = [
        {"timestamp": "2024-06-05", "open": 700.0, "high": 730.0, "low": 664.0, "close": 705.0, "volume": 1_000},
        {"timestamp": "2024-06-05", "open": 12.0, "high": 13.0, "low": 11.0, "close": 12.5, "volume": 2_000},
    ]

    frame = normalize_daily_payload(payload)

    assert len(frame.index) == 2


def test_duplicate_bars_differing_only_in_volume_are_preserved():
    """Same OHLC, different volume is a partial-vs-final bar, not a redundant copy.

    Only the DATA-002 repair may resolve that (highest volume wins); the vendor
    boundary must not silently pick one.
    """
    payload = [
        {"timestamp": "2024-06-05", "open": 700.0, "high": 730.0, "low": 664.0, "close": 705.0, "volume": 1_122_766},
        {"timestamp": "2024-06-05", "open": 700.0, "high": 730.0, "low": 664.0, "close": 705.0, "volume": 2_245},
    ]

    frame = normalize_daily_payload(payload)

    assert len(frame.index) == 2


def test_deduped_payload_passes_the_data_quality_gate():
    """The end-to-end point: an exact-duplicate payload no longer quarantines."""
    from backend.data_quality.candles import validate_candles

    payload = [
        {"timestamp": "2024-06-04", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 10},
        {"timestamp": "2024-06-05", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 20},
        {"timestamp": "2024-06-05", "open": 100.0, "high": 105.0, "low": 99.0, "close": 104.0, "volume": 20},
    ]

    report = validate_candles(normalize_daily_payload(payload), symbol="AEGISLOG")

    assert "DUPLICATE_DATE" not in {finding.code for finding in report.findings}
