"""Tests for the daily candle cache/failure layer."""

from __future__ import annotations

import concurrent.futures
import logging
import os
import threading
import time
from datetime import date, datetime

import pandas as pd
import pytest

from backend.daily_data_loader import (
    DEFAULT_HISTORY_YEARS_BACK,
    DailyDataLoader,
    _RequestPacer,
    history_start_date,
)
from backend.dhan_client import DhanRateLimitError
from backend.observability import (
    EVENT_CANDLE_DATA_QUALITY_FAILED,
    EVENT_CANDLE_DATA_QUALITY_WARNING,
    EVENT_EXTERNAL_API_FAILED,
)


def test_history_start_date_subtracts_years_and_handles_leap_day():
    """The shared helper underpins both Streamlit scans and the headless job."""
    # Normal date: subtract exactly ten calendar years.
    assert history_start_date(DEFAULT_HISTORY_YEARS_BACK, date(2026, 6, 5)) == date(
        2016, 6, 5
    )
    # Feb 29 minus ten years lands on a non-leap year, so fall back to Feb 28.
    assert history_start_date(10, date(2024, 2, 29)) == date(2014, 2, 28)
    # A custom window still works.
    assert history_start_date(1, date(2020, 1, 15)) == date(2019, 1, 15)


class FakeDhanClient:
    """Minimal fake client that behaves like DhanDataClient without the network."""

    def __init__(self):
        self.calls = 0

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        # Counting calls lets the test prove the second request came from cache.
        self.calls += 1
        return pd.DataFrame(
            {
                "timestamp": pd.to_datetime(["2026-05-01", "2026-05-10", "2026-05-11"]),
                "open": [99.0, 100.0, 101.0],
                "high": [109.0, 110.0, 111.0],
                "low": [94.0, 95.0, 96.0],
                "close": [107.0, 108.0, 109.0],
                "volume": [900.0, 1000.0, 1200.0],
            }
        )


def candle_frame() -> pd.DataFrame:
    """Return a tiny successful daily candle frame for fake clients."""
    return pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-10", "2026-05-11"]),
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [95.0, 96.0],
            "close": [108.0, 109.0],
            "volume": [1000.0, 1200.0],
        }
    )


def mapped_universe() -> pd.DataFrame:
    """Return one mapped universe row for loader tests."""
    return pd.DataFrame(
        [
            {
                "symbol": "RELIANCE",
                "security_id": "2885",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "mapping_status": "mapped",
            }
        ]
    )


def mapped_universe_many(symbols: list[str]) -> pd.DataFrame:
    """Return mapped universe rows with stable fake security IDs."""
    return pd.DataFrame(
        [
            {
                "symbol": symbol,
                "security_id": str(index),
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "mapping_status": "mapped",
            }
            for index, symbol in enumerate(symbols, start=1)
        ]
    )


class SequenceDhanClient:
    """Fake Dhan client that returns or raises outcomes in order."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = 0

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if outcome == "rate_limit":
            raise DhanRateLimitError("DH-904 Rate_Limit")
        return candle_frame()


class SingleFrameClient:
    """Fake Dhan client that returns the same prepared frame for each request."""

    def __init__(self, frame: pd.DataFrame):
        self.frame = frame

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        return self.frame.copy(deep=True)


def test_daily_data_loader_uses_cache_on_second_fetch(tmp_path):
    # tmp_path gives this test its own throwaway cache directory.
    client = FakeDhanClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }

    first, first_from_cache = loader.get_daily_history(
        instrument, date(2026, 5, 1), date(2026, 5, 11)
    )
    second, second_from_cache = loader.get_daily_history(
        instrument, date(2026, 5, 1), date(2026, 5, 11)
    )

    # First call fetches and writes cache; second call reads that cache.
    assert not first_from_cache
    assert second_from_cache
    assert client.calls == 1
    assert first.equals(second)


def test_cache_hit_does_not_sleep_or_call_dhan_again(tmp_path):
    sleeps = []
    client = FakeDhanClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.5,
        sleep_func=sleeps.append,
    )
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }

    loader.get_daily_history(instrument, date(2026, 5, 1), date(2026, 5, 11))
    sleeps.clear()
    loader.get_daily_history(instrument, date(2026, 5, 1), date(2026, 5, 11))

    assert sleeps == []
    assert client.calls == 1


def test_cache_hit_refetches_when_requested_range_starts_before_cached_data(tmp_path):
    """A parquet must cover both ends of the requested range to count as a hit.

    Long-lookback screeners can ask for years of data. If an existing cache only
    contains recent candles, returning a sliced recent frame silently weakens
    the strategy. The loader should fetch the requested range instead.
    """

    class FullRangeClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fetch_daily_candles(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(
                        ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"]
                    ),
                    "open": [100.0, 101.0, 102.0, 103.0],
                    "high": [110.0, 111.0, 112.0, 113.0],
                    "low": [95.0, 96.0, 97.0, 98.0],
                    "close": [108.0, 109.0, 110.0, 111.0],
                    "volume": [1000.0, 1200.0, 1300.0, 1400.0],
                }
            )

    client = FullRangeClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = mapped_universe().iloc[0].to_dict()
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-05-03", "2026-05-04"]),
            "open": [102.0, 103.0],
            "high": [112.0, 113.0],
            "low": [97.0, 98.0],
            "close": [110.0, 111.0],
            "volume": [1300.0, 1400.0],
        }
    ).to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    frame, from_cache = loader.get_daily_history(
        instrument,
        date(2026, 5, 1),
        date(2026, 5, 4),
    )

    assert not from_cache
    assert len(client.calls) == 1
    assert client.calls[0]["from_date"] == date(2026, 5, 1)
    assert frame["timestamp"].dt.date.min() == date(2026, 5, 1)


def test_cache_miss_sleeps_before_api_call(tmp_path):
    sleeps = []
    client = FakeDhanClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.5,
        sleep_func=sleeps.append,
    )

    result = loader.load_universe_history(mapped_universe(), date(2026, 5, 1), date(2026, 5, 11))

    assert "RELIANCE" in result.frames
    assert sleeps == [0.5]
    assert result.api_attempts == 1
    assert result.rate_limit_retries == 0


def test_rate_limit_failure_retries_after_backoff_and_succeeds(tmp_path):
    sleeps = []
    client = SequenceDhanClient(["rate_limit", "success"])
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.5,
        rate_limit_retry_delays=[2.0, 5.0, 10.0],
        sleep_func=sleeps.append,
    )

    result = loader.load_universe_history(mapped_universe(), date(2026, 5, 1), date(2026, 5, 11))

    assert "RELIANCE" in result.frames
    assert result.failures == []
    assert sleeps == [0.5, 2.0]
    assert client.calls == 2
    assert result.api_attempts == 2
    assert result.rate_limit_retries == 1
    assert loader.last_api_attempts == 2
    assert loader.last_rate_limit_retries == 1


def test_repeated_rate_limit_failures_are_recorded_without_crashing(tmp_path):
    sleeps = []
    client = SequenceDhanClient(["rate_limit", "rate_limit", "rate_limit", "rate_limit"])
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.5,
        rate_limit_retry_delays=[2.0, 5.0, 10.0],
        sleep_func=sleeps.append,
    )

    result = loader.load_universe_history(mapped_universe(), date(2026, 5, 1), date(2026, 5, 11))

    assert result.frames == {}
    assert result.failures[0]["symbol"] == "RELIANCE"
    assert "DH-904" in result.failures[0]["message"]
    assert sleeps == [0.5, 2.0, 5.0, 10.0]
    assert client.calls == 4
    assert result.api_attempts == 4
    assert result.rate_limit_retries == 3


def test_load_universe_history_records_failures_without_crashing(tmp_path):
    class FailingClient:
        """Fake client that simulates one symbol failing during a batch run."""

        def fetch_daily_candles(self, *args, **kwargs):
            raise RuntimeError("boom")

    loader = DailyDataLoader(FailingClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    universe = mapped_universe()

    result = loader.load_universe_history(universe, date(2026, 5, 1), date(2026, 5, 11))

    # A failed symbol should be reported in failures, not raised as an exception
    # that stops every other symbol from being scanned.
    assert result.frames == {}
    assert result.failures[0]["symbol"] == "RELIANCE"
    assert "boom" in result.failures[0]["message"]


def test_load_universe_history_redacts_failure_messages(tmp_path):
    """Loader failure details are later shown in Streamlit run details."""

    class SecretFailingClient:
        """Fake client that simulates a broker exception echoing a token."""

        def fetch_daily_candles(self, *args, **kwargs):
            raise RuntimeError("Dhan failed with access_token=broker-token-secret")

    loader = DailyDataLoader(
        SecretFailingClient(),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )

    result = loader.load_universe_history(
        mapped_universe(),
        date(2026, 5, 1),
        date(2026, 5, 11),
    )

    assert result.frames == {}
    assert "broker-token-secret" not in result.failures[0]["message"]
    assert "***REDACTED***" in result.failures[0]["message"]


def test_load_universe_history_emits_external_api_failed_event(tmp_path, caplog):
    """OBS-001: a failed Dhan fetch emits external_api_failed tagged with the symbol."""

    class FailingClient:
        def fetch_daily_candles(self, *args, **kwargs):
            raise RuntimeError("boom")

    loader = DailyDataLoader(FailingClient(), cache_dir=tmp_path, request_delay_seconds=0.0)

    with caplog.at_level(logging.WARNING):
        loader.load_universe_history(mapped_universe(), date(2026, 5, 1), date(2026, 5, 11))

    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_EXTERNAL_API_FAILED
    ]
    assert len(events) == 1
    assert events[0]["symbol"] == "RELIANCE"


def test_iter_universe_history_blocks_fatal_candle_quality_before_scan(tmp_path, caplog):
    """Fatal candle defects should become loader failures before scanner code runs."""
    frame = candle_frame()
    frame.loc[0, "high"] = frame.loc[0, "low"] - 1.0
    loader = DailyDataLoader(SingleFrameClient(frame), cache_dir=tmp_path, request_delay_seconds=0.0)

    with caplog.at_level(logging.WARNING):
        items = list(
            loader.iter_universe_history(
                mapped_universe(),
                date(2026, 5, 10),
                date(2026, 5, 11),
            )
        )

    assert len(items) == 1
    assert items[0].failure is not None
    assert items[0].failure["phase"] == "data_quality"
    assert items[0].failure["quality_codes"] == ["HIGH_BELOW_LOW"]
    assert "95.0" not in str(items[0].failure)
    assert loader.last_failures == [items[0].failure]
    assert len(loader.last_data_quality_reports) == 1
    assert loader.last_data_quality_reports[0].findings[0].code == "HIGH_BELOW_LOW"
    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_CANDLE_DATA_QUALITY_FAILED
    ]
    assert len(events) == 1
    assert events[0]["symbol"] == "RELIANCE"
    assert events[0]["finding_codes"] == ["HIGH_BELOW_LOW"]


def test_warning_only_candle_quality_passes_through_and_logs(tmp_path, caplog):
    """Stale-but-usable data should be recorded without blocking the frame."""
    loader = DailyDataLoader(
        SingleFrameClient(candle_frame()),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )

    with caplog.at_level(logging.WARNING):
        result = loader.load_universe_history(
            mapped_universe(),
            date(2026, 5, 10),
            date(2026, 5, 13),
        )

    assert list(result.frames) == ["RELIANCE"]
    assert result.failures == []
    assert len(loader.last_data_quality_reports) == 1
    report = loader.last_data_quality_reports[0]
    assert [finding.code for finding in report.findings] == ["STALE_LATEST_CANDLE"]
    assert report.is_usable
    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_CANDLE_DATA_QUALITY_WARNING
    ]
    assert len(events) == 1
    assert events[0]["symbol"] == "RELIANCE"
    assert events[0]["finding_codes"] == ["STALE_LATEST_CANDLE"]


def test_circuit_breaker_skips_remaining_symbols_after_failure_limit(tmp_path):
    """Repeated Dhan failures should stop the batch from hammering the API."""

    class AlwaysFailingClient:
        def __init__(self):
            self.calls = 0

        def fetch_daily_candles(self, *args, **kwargs):
            self.calls += 1
            raise RuntimeError("broker down")

    client = AlwaysFailingClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        max_consecutive_failures=1,
    )

    result = loader.load_universe_history(
        mapped_universe_many(["AAA", "BBB", "CCC"]),
        date(2026, 5, 1),
        date(2026, 5, 11),
    )

    assert client.calls == 1
    assert [failure["symbol"] for failure in result.failures] == ["AAA", "BBB", "CCC"]
    assert any("circuit breaker" in str(failure["message"]).lower() for failure in result.failures[1:])


def test_fetch_timeout_records_failure_without_waiting_for_slow_client(tmp_path):
    """A stuck Dhan call should fail the symbol promptly instead of blocking."""

    class SlowClient:
        def __init__(self):
            self.calls = 0

        def fetch_daily_candles(self, *args, **kwargs):
            self.calls += 1
            time.sleep(0.25)
            return candle_frame()

    loader = DailyDataLoader(
        SlowClient(),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        fetch_timeout_seconds=0.01,
    )
    started = time.monotonic()

    result = loader.load_universe_history(
        mapped_universe(),
        date(2026, 5, 1),
        date(2026, 5, 11),
    )

    assert time.monotonic() - started < 0.20
    assert result.frames == {}
    assert "timed out" in str(result.failures[0]["message"]).lower()


def test_cache_path_is_date_independent(tmp_path):
    """The new cache layout writes one parquet file per (symbol, security_id)."""
    loader = DailyDataLoader(FakeDhanClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    assert loader.cache_path("RELIANCE", "2885").name == "RELIANCE_2885.parquet"


def test_ensure_daily_history_fresh_download(tmp_path):
    """First-ever call has no cache, so it should download the full window."""

    class FakeClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fetch_daily_candles(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
                    "open": [100.0, 101.0],
                    "high": [110.0, 111.0],
                    "low": [95.0, 96.0],
                    "close": [108.0, 109.0],
                    "volume": [1000.0, 1200.0],
                }
            )

    client = FakeClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }

    frame, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 3))

    assert status == "fresh_download"
    assert len(client.calls) == 1
    # 10y back from 2024-01-03 is 2014-01-03; the loader should ask for that range.
    assert client.calls[0]["from_date"] == date(2014, 1, 3)
    assert client.calls[0]["to_date"] == date(2024, 1, 3)
    # The parquet file should now exist under the stable name.
    assert (tmp_path / "RELIANCE_2885.parquet").exists()
    assert len(frame) == 2


def test_cache_only_loader_fails_clearly_when_history_needs_fetch(tmp_path):
    """A cache-only loader should explain why it cannot fill a missing cache."""
    loader = DailyDataLoader(
        None,
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }

    with pytest.raises(
        RuntimeError,
        match=r"built without a Dhan client.*cache-only mode",
    ):
        loader.ensure_daily_history(
            instrument,
            years_back=10,
            today=date(2024, 1, 3),
        )


def test_ensure_daily_history_fresh_skips_api_when_cache_is_current(tmp_path):
    """If cache already covers today, no API call should happen."""

    class CountingClient:
        def __init__(self):
            self.calls = 0

        def fetch_daily_candles(self, **kwargs):
            self.calls += 1
            return pd.DataFrame()

    loader = DailyDataLoader(CountingClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }
    # Pre-seed a parquet so `today` is already covered.
    seeded = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2014-01-03", "2024-01-02", "2024-01-03"]),
            "open": [50.0, 100.0, 101.0],
            "high": [55.0, 110.0, 111.0],
            "low": [45.0, 95.0, 96.0],
            "close": [52.0, 108.0, 109.0],
            "volume": [500.0, 1000.0, 1200.0],
        }
    )
    seeded.to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    _, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 3))
    assert status == "fresh"
    assert loader.client.calls == 0


def test_ensure_daily_history_backfills_when_cache_starts_after_requested_start(tmp_path):
    """A current last candle is not enough when the historical front is missing."""

    class BackfillClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fetch_daily_candles(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2014-01-03", "2024-01-03"]),
                    "open": [50.0, 100.0],
                    "high": [55.0, 110.0],
                    "low": [45.0, 95.0],
                    "close": [52.0, 108.0],
                    "volume": [500.0, 1000.0],
                }
            )

    client = BackfillClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2014-01-05", "2024-01-02", "2024-01-03"]),
            "open": [50.0, 100.0, 101.0],
            "high": [55.0, 110.0, 111.0],
            "low": [45.0, 95.0, 96.0],
            "close": [52.0, 108.0, 109.0],
            "volume": [500.0, 1000.0, 1200.0],
        }
    ).to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    frame, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 3))

    assert status == "backfilled"
    assert len(client.calls) == 1
    assert client.calls[0]["from_date"] == date(2014, 1, 3)
    assert client.calls[0]["to_date"] == date(2024, 1, 3)
    assert frame["timestamp"].dt.date.min() == date(2014, 1, 3)


def test_ensure_daily_history_appends_incrementally(tmp_path):
    """When the cache is behind today, only the missing days should be fetched."""

    class IncrementalClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fetch_daily_candles(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame(
                {
                    "timestamp": pd.to_datetime(["2024-01-04", "2024-01-05"]),
                    "open": [102.0, 103.0],
                    "high": [112.0, 113.0],
                    "low": [97.0, 98.0],
                    "close": [110.0, 112.0],
                    "volume": [1300.0, 1400.0],
                }
            )

    client = IncrementalClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }
    # Cache ends on 2024-01-03; today is 2024-01-05.
    seeded = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2014-01-05", "2024-01-02", "2024-01-03"]),
            "open": [50.0, 100.0, 101.0],
            "high": [55.0, 110.0, 111.0],
            "low": [45.0, 95.0, 96.0],
            "close": [52.0, 108.0, 109.0],
            "volume": [500.0, 1000.0, 1200.0],
        }
    )
    seeded.to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    frame, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 5))

    assert status == "incremental"
    assert len(client.calls) == 1
    # Incremental fetch starts the day after the last cached timestamp.
    assert client.calls[0]["from_date"] == date(2024, 1, 4)
    assert client.calls[0]["to_date"] == date(2024, 1, 5)
    # Merged frame should contain cached history plus the two new candle days.
    assert len(frame) == 5


def test_empty_incremental_fetch_writes_checked_marker_to_avoid_retries(tmp_path):
    """Weekends/holidays should not trigger the same empty API fetch repeatedly."""

    class EmptyIncrementalClient:
        def __init__(self):
            self.calls: list[dict] = []

        def fetch_daily_candles(self, **kwargs):
            self.calls.append(kwargs)
            return pd.DataFrame()

    client = EmptyIncrementalClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    instrument = {
        "symbol": "RELIANCE",
        "security_id": "2885",
        "exchange_segment": "NSE_EQ",
        "instrument_type": "EQUITY",
    }
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2014-01-05", "2024-01-03"]),
            "open": [50.0, 101.0],
            "high": [55.0, 111.0],
            "low": [45.0, 96.0],
            "close": [52.0, 109.0],
            "volume": [500.0, 1200.0],
        }
    ).to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    _, first_status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 5))
    _, second_status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 5))

    assert first_status == "fresh"
    assert second_status == "fresh"
    assert len(client.calls) == 1


def test_read_cached_history_returns_empty_when_missing(tmp_path):
    loader = DailyDataLoader(FakeDhanClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    assert loader.read_cached_history("UNKNOWN", "9999").empty


def test_cleanup_legacy_cache_files_removes_only_date_suffixed_files(tmp_path):
    """Legacy cache filenames have two trailing 8-digit date stamps."""
    (tmp_path / "RELIANCE_2885_20150101_20250101.parquet").write_bytes(b"x")
    # The non-legacy file should be kept.
    (tmp_path / "RELIANCE_2885.parquet").write_bytes(b"y")
    # A file that incidentally has digits in the symbol stays untouched too.
    (tmp_path / "20MICRONS_12345.parquet").write_bytes(b"z")
    # A malformed one-part filename cannot contain the four legacy components.
    # Cleanup must leave it alone instead of indexing past its split parts.
    (tmp_path / "orphan.parquet").write_bytes(b"o")

    loader = DailyDataLoader(FakeDhanClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    removed = loader.cleanup_legacy_cache_files()

    assert removed == 1
    assert not (tmp_path / "RELIANCE_2885_20150101_20250101.parquet").exists()
    assert (tmp_path / "RELIANCE_2885.parquet").exists()
    assert (tmp_path / "20MICRONS_12345.parquet").exists()
    assert (tmp_path / "orphan.parquet").exists()


def test_cleanup_stale_cache_files_removes_old_parquets_and_orphan_markers(tmp_path):
    """Cache cleanup should remove old files and checked markers without owners."""
    old_parquet = tmp_path / "OLD_1.parquet"
    old_checked = tmp_path / "OLD_1.checked"
    recent_parquet = tmp_path / "RECENT_2.parquet"
    orphan_checked = tmp_path / "ORPHAN_3.checked"
    for path in (old_parquet, old_checked, recent_parquet, orphan_checked):
        path.write_bytes(b"x")

    old_time = datetime(2026, 1, 1).timestamp()
    recent_time = datetime(2026, 6, 1).timestamp()
    os.utime(old_parquet, (old_time, old_time))
    os.utime(old_checked, (old_time, old_time))
    os.utime(recent_parquet, (recent_time, recent_time))
    os.utime(orphan_checked, (recent_time, recent_time))

    loader = DailyDataLoader(FakeDhanClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    removed = loader.cleanup_stale_cache_files(
        max_age_days=30,
        now=datetime(2026, 6, 2),
    )

    assert removed == 3
    assert not old_parquet.exists()
    assert not old_checked.exists()
    assert not orphan_checked.exists()
    assert recent_parquet.exists()


# ---------------------------------------------------------------------------
# PERF-001: parallel fetching, the shared request pacer, and prefetch streaming
# ---------------------------------------------------------------------------


class ThreadSafeDhanClient:
    """Fake client safe to call from worker threads, with per-id failures."""

    def __init__(self, fail_ids=()):
        self._lock = threading.Lock()
        self.calls = 0
        self.fail_ids = set(fail_ids)

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        with self._lock:
            self.calls += 1
        if security_id in self.fail_ids:
            raise RuntimeError(f"boom for {security_id}")
        return candle_frame()


class AlwaysFailDhanClient:
    """Fake client whose every call fails, for circuit-breaker tests."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        with self._lock:
            self.calls += 1
        raise RuntimeError("persistent broker failure")


class RateLimitOncePerSecurityClient:
    """Rate-limit each security once, then return candles on its retry."""

    def __init__(self):
        self._lock = threading.Lock()
        self.attempts: dict[str, int] = {}

    def fetch_daily_candles(
        self, security_id, exchange_segment, instrument_type, from_date, to_date
    ):
        with self._lock:
            self.attempts[security_id] = self.attempts.get(security_id, 0) + 1
            attempt = self.attempts[security_id]
        if attempt == 1:
            raise DhanRateLimitError("DH-904 Rate_Limit")
        return candle_frame()


class CountingPacer:
    """Thread-safe pacer probe that records every reserved request slot."""

    def __init__(self):
        self._lock = threading.Lock()
        self.calls = 0

    def wait(self) -> None:
        with self._lock:
            self.calls += 1


class FailFailThenSuccessClient:
    """Force two failures and an already-running success to finish together."""

    def __init__(self):
        self._first_wave = threading.Barrier(3)

    def fetch_daily_candles(
        self, security_id, exchange_segment, instrument_type, from_date, to_date
    ):
        if security_id in {"1", "2", "3"}:
            self._first_wave.wait(timeout=5)
        if security_id in {"1", "2"}:
            raise RuntimeError(f"boom for {security_id}")
        return candle_frame()


def test_request_pacer_enforces_global_spacing():
    """Each wait() reserves the next slot; only the first request is unpaced."""
    clock = {"now": 100.0}
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(round(seconds, 6))
        clock["now"] += seconds

    pacer = _RequestPacer(0.5, fake_sleep, time_func=lambda: clock["now"])
    pacer.wait()
    pacer.wait()
    pacer.wait()

    assert sleeps == [0.5, 0.5]


def test_request_pacer_zero_delay_never_sleeps():
    sleeps: list[float] = []
    pacer = _RequestPacer(0.0, sleeps.append)
    pacer.wait()
    pacer.wait()
    assert sleeps == []


def test_parallel_rate_limit_retries_reserve_a_pacer_slot_for_every_attempt(tmp_path):
    """Concurrent DH-904 retries must pass through the shared pacer again."""
    client = RateLimitOncePerSecurityClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        rate_limit_retry_delays=[0.0],
        fetch_workers=2,
    )
    pacer = CountingPacer()
    loader._pacer = pacer

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                loader._fetch_with_rate_limit_retries,
                security_id,
                "NSE_EQ",
                "EQUITY",
                "2026-05-10",
                "2026-05-11",
            )
            for security_id in ("1", "2")
        ]
        frames = [future.result() for future in futures]

    assert all(not frame.empty for frame in frames)
    assert client.attempts == {"1": 2, "2": 2}
    assert pacer.calls == sum(client.attempts.values())


def test_parallel_iter_matches_sequential_output(tmp_path):
    """workers=4 must yield the same items, order, and stats as workers=1."""
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE", "FFF"]
    universe = mapped_universe_many(symbols)

    sequential = DailyDataLoader(
        ThreadSafeDhanClient(),
        cache_dir=tmp_path / "seq",
        request_delay_seconds=0.0,
        fetch_workers=1,
    )
    parallel = DailyDataLoader(
        ThreadSafeDhanClient(),
        cache_dir=tmp_path / "par",
        request_delay_seconds=0.0,
        fetch_workers=4,
    )

    seq_items = list(sequential.iter_universe_history(universe, "2026-05-10", "2026-05-11"))
    par_items = list(parallel.iter_universe_history(universe, "2026-05-10", "2026-05-11"))

    assert [item.symbol for item in par_items] == [item.symbol for item in seq_items] == symbols
    assert all(item.failure is None for item in par_items)
    assert parallel.last_cache_misses == sequential.last_cache_misses == len(symbols)
    assert parallel.last_api_attempts == sequential.last_api_attempts == len(symbols)
    for seq_item, par_item in zip(seq_items, par_items, strict=True):
        pd.testing.assert_frame_equal(seq_item.candles, par_item.candles)


def test_parallel_iter_progress_indices_strictly_increase(tmp_path):
    """The progress callback contract survives parallel fetching unchanged."""
    symbols = ["AAA", "BBB", "CCC", "DDD", "EEE"]
    loader = DailyDataLoader(
        ThreadSafeDhanClient(),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        fetch_workers=3,
    )
    seen: list[tuple[int, int, str]] = []

    list(
        loader.iter_universe_history(
            mapped_universe_many(symbols),
            "2026-05-10",
            "2026-05-11",
            progress_callback=lambda done, total, symbol: seen.append((done, total, symbol)),
        )
    )

    assert [entry[0] for entry in seen] == list(range(1, len(symbols) + 1))
    assert all(entry[1] == len(symbols) for entry in seen)
    assert [entry[2] for entry in seen] == symbols


def test_parallel_circuit_breaker_stops_new_submissions(tmp_path):
    """Once the breaker trips, no new fetches are submitted; bookkeeping stays exact."""
    symbols = [f"SYM{i:02d}" for i in range(12)]
    client = AlwaysFailDhanClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        max_consecutive_failures=2,
        fetch_workers=2,
    )

    items = list(loader.iter_universe_history(mapped_universe_many(symbols), "2026-05-10", "2026-05-11"))

    assert len(items) == len(symbols)
    assert all(item.failure is not None for item in items)
    breaker_items = [
        item for item in items if "circuit breaker" in str(item.failure.get("message", ""))
    ]
    # Every consumed real fetch corresponds to one client call; everything else
    # must be a breaker skip, and the breaker must have prevented most calls.
    assert client.calls + len(breaker_items) == len(symbols)
    assert client.calls < len(symbols)
    assert breaker_items, "breaker should have skipped at least the unsubmitted rows"
    assert all(
        "after 2 consecutive failure(s)" in str(item.failure["message"])
        for item in breaker_items
    )


def test_parallel_breaker_messages_keep_the_failure_count_that_tripped_it(tmp_path):
    """An in-flight success must not rewrite the breaker's recorded threshold."""
    symbols = [f"SYM{i:02d}" for i in range(1, 9)]
    loader = DailyDataLoader(
        FailFailThenSuccessClient(),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        max_consecutive_failures=2,
        fetch_workers=3,
    )

    items = list(
        loader.iter_universe_history(
            mapped_universe_many(symbols),
            "2026-05-10",
            "2026-05-11",
        )
    )

    breaker_messages = [
        str(item.failure["message"])
        for item in items
        if item.failure is not None and "circuit breaker" in str(item.failure["message"])
    ]
    assert breaker_messages
    assert all("after 2 consecutive failure(s)" in message for message in breaker_messages)


def test_iter_ensure_universe_history_parallel_keeps_order_and_redacts(tmp_path, monkeypatch):
    """Prefetch streaming preserves input order and redacts failure text."""
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "prefetch-secret")
    symbols = ["AAA", "BBB", "CCC", "DDD"]
    rows = mapped_universe_many(symbols).to_dict("records")
    # security_id "2" belongs to BBB; make it fail with secret-bearing text.
    client = ThreadSafeDhanClient(fail_ids={"2"})

    class SecretFailClient(ThreadSafeDhanClient):
        def fetch_daily_candles(self, security_id, *args, **kwargs):
            if security_id == "2":
                with self._lock:
                    self.calls += 1
                raise RuntimeError("token=prefetch-secret rejected")
            return super().fetch_daily_candles(security_id, *args, **kwargs)

    client = SecretFailClient()
    loader = DailyDataLoader(
        client,
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
        fetch_workers=4,
    )

    outcomes = list(loader.iter_ensure_universe_history(rows, years_back=1))

    assert [outcome.symbol for outcome in outcomes] == symbols
    assert outcomes[0].status == "fresh_download"
    assert outcomes[1].status == "failed"
    assert outcomes[1].message is not None
    assert "prefetch-secret" not in outcomes[1].message
    assert outcomes[2].status == "fresh_download"


def test_fetch_workers_setting_clamps_and_defaults(monkeypatch, tmp_path):
    """SCANNER_DHAN_FETCH_WORKERS parses defensively and clamps to 1..8."""
    from backend.config import dhan_fetch_workers

    monkeypatch.delenv("SCANNER_DHAN_FETCH_WORKERS", raising=False)
    assert dhan_fetch_workers() == 1
    monkeypatch.setenv("SCANNER_DHAN_FETCH_WORKERS", "4")
    assert dhan_fetch_workers() == 4
    monkeypatch.setenv("SCANNER_DHAN_FETCH_WORKERS", "80")
    assert dhan_fetch_workers() == 8
    monkeypatch.setenv("SCANNER_DHAN_FETCH_WORKERS", "0")
    assert dhan_fetch_workers() == 1
    monkeypatch.setenv("SCANNER_DHAN_FETCH_WORKERS", "not-a-number")
    assert dhan_fetch_workers() == 1

    # The loader clamps explicit constructor values the same way.
    loader = DailyDataLoader(None, cache_dir=tmp_path, fetch_workers=99)
    assert loader.fetch_workers == 8
