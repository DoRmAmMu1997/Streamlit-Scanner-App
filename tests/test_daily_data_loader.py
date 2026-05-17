from __future__ import annotations

"""Tests for the daily candle cache/failure layer."""

from datetime import date

import pandas as pd

from backend.daily_data_loader import DailyDataLoader
from backend.dhan_client import DhanRateLimitError


class FakeDhanClient:
    """Minimal fake client that behaves like DhanDataClient without the network."""

    def __init__(self):
        self.calls = 0

    def fetch_daily_candles(self, security_id, exchange_segment, instrument_type, from_date, to_date):
        # Counting calls lets the test prove the second request came from cache.
        self.calls += 1
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
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [95.0, 96.0],
            "close": [108.0, 109.0],
            "volume": [1000.0, 1200.0],
        }
    )
    seeded.to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    _, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 3))
    assert status == "fresh"
    assert loader.client.calls == 0


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
            "timestamp": pd.to_datetime(["2024-01-02", "2024-01-03"]),
            "open": [100.0, 101.0],
            "high": [110.0, 111.0],
            "low": [95.0, 96.0],
            "close": [108.0, 109.0],
            "volume": [1000.0, 1200.0],
        }
    )
    seeded.to_parquet(tmp_path / "RELIANCE_2885.parquet", index=False)

    frame, status = loader.ensure_daily_history(instrument, years_back=10, today=date(2024, 1, 5))

    assert status == "incremental"
    assert len(client.calls) == 1
    # Incremental fetch starts the day after the last cached timestamp.
    assert client.calls[0]["from_date"] == date(2024, 1, 4)
    assert client.calls[0]["to_date"] == date(2024, 1, 5)
    # Merged frame should contain all four candle days, deduped.
    assert len(frame) == 4


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
