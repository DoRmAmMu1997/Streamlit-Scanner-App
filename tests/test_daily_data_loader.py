"""Tests for the daily candle cache/failure layer."""

from __future__ import annotations

import os
import time
from datetime import date, datetime

import pandas as pd

from backend.daily_data_loader import (
    DEFAULT_HISTORY_YEARS_BACK,
    DailyDataLoader,
    history_start_date,
)
from backend.dhan_client import DhanRateLimitError


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

    loader = DailyDataLoader(FakeDhanClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    removed = loader.cleanup_legacy_cache_files()

    assert removed == 1
    assert not (tmp_path / "RELIANCE_2885_20150101_20250101.parquet").exists()
    assert (tmp_path / "RELIANCE_2885.parquet").exists()
    assert (tmp_path / "20MICRONS_12345.parquet").exists()


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
