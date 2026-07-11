"""Loader integration tests for the PERF-002 footer-stats fast paths.

Two behaviors changed and two must NOT have changed:

1. NEW: ``get_daily_history`` can use the footer to skip a frame that would be
   discarded on a definite cache miss. Prefetch still reads the frame before
   declaring it fresh because footer metadata cannot prove data pages remain
   readable.
2. UNCHANGED: any file the footer cannot vouch for (``write_statistics=False``
   is the canonical case) behaves exactly as before via the full-read
   fallback — a covered range is still a cache hit, a fresh cache is still
   "fresh". The pre-existing suite (test_daily_data_loader.py) runs
   unmodified as the broader behavior lock.
"""

from __future__ import annotations

from datetime import date, timedelta

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from backend import daily_data_loader
from backend.daily_data_loader import DailyDataLoader, history_start_date

TODAY = date(2026, 7, 10)
YEARS_BACK = 10


def _instrument() -> dict:
    return {"symbol": "DEMO", "security_id": "1"}


def _covering_frame() -> pd.DataFrame:
    """Business-day candles spanning the full prefetch window ending today."""
    start = history_start_date(YEARS_BACK, TODAY) - timedelta(days=5)
    stamps = pd.date_range(start, TODAY, freq="B")
    if stamps[-1].date() != TODAY:
        stamps = stamps.append(pd.DatetimeIndex([pd.Timestamp(TODAY)]))
    return pd.DataFrame(
        {
            "timestamp": stamps,
            "open": 100.0,
            "high": 110.0,
            "low": 95.0,
            "close": 105.0,
            "volume": 1000.0,
        }
    )


def _write_cache(loader: DailyDataLoader, frame: pd.DataFrame, *, statistics: bool) -> None:
    """Write DEMO's cache file with or without footer statistics."""
    path = loader.cache_path("DEMO", "1")
    path.parent.mkdir(parents=True, exist_ok=True)
    if statistics:
        frame.to_parquet(path, index=False)
    else:
        pq.write_table(pa.Table.from_pandas(frame), path, write_statistics=False)


def _forbid_full_reads(monkeypatch) -> None:
    """Make any pandas parquet read fail loudly inside the loader module."""

    def _explode(*_args, **_kwargs):
        raise AssertionError("this path must answer from footer statistics alone")

    monkeypatch.setattr(daily_data_loader.pd, "read_parquet", _explode)


def test_prefetch_validates_frame_before_fresh_verdict(monkeypatch, tmp_path):
    """A fresh prefetch verdict must validate the actual Parquet data pages.

    Beginner note: footer statistics are a quick index, not an integrity
    check. A file may keep a readable footer even when a data page is damaged,
    so this path deliberately pays for one full read before saying "fresh".
    """
    loader = DailyDataLoader(client=None, cache_dir=tmp_path, request_delay_seconds=0.0)
    _write_cache(loader, _covering_frame(), statistics=True)
    real_read_parquet = pd.read_parquet
    read_paths = []

    def _record_read(path, *args, **kwargs):
        read_paths.append(path)
        return real_read_parquet(path, *args, **kwargs)

    monkeypatch.setattr(daily_data_loader.pd, "read_parquet", _record_read)

    outcomes = list(
        loader.iter_ensure_universe_history(
            [_instrument()], years_back=YEARS_BACK, today=TODAY
        )
    )

    assert [(outcome.symbol, outcome.status) for outcome in outcomes] == [("DEMO", "fresh")]
    assert read_paths == [loader.cache_path("DEMO", "1")]


def test_prefetch_does_not_call_unreadable_data_pages_fresh(monkeypatch, tmp_path):
    """A footer-only success must not hide a corrupt Parquet data page."""
    loader = DailyDataLoader(client=None, cache_dir=tmp_path, request_delay_seconds=0.0)
    _write_cache(loader, _covering_frame(), statistics=True)
    monkeypatch.setattr(
        daily_data_loader,
        "timestamp_bounds",
        lambda _path: (history_start_date(YEARS_BACK, TODAY), TODAY),
    )

    def _unreadable(*_args, **_kwargs):
        raise OSError("parquet data page is corrupt")

    monkeypatch.setattr(daily_data_loader.pd, "read_parquet", _unreadable)

    outcomes = list(
        loader.iter_ensure_universe_history(
            [_instrument()], years_back=YEARS_BACK, today=TODAY
        )
    )

    assert outcomes[0].status == "failed"
    assert "parquet data page is corrupt" in (outcomes[0].message or "")


def test_prefetch_falls_back_to_full_read_without_footer_statistics(tmp_path):
    """A statless covering cache must still be 'fresh' via the slow path."""
    loader = DailyDataLoader(client=None, cache_dir=tmp_path, request_delay_seconds=0.0)
    _write_cache(loader, _covering_frame(), statistics=False)

    outcomes = list(
        loader.iter_ensure_universe_history(
            [_instrument()], years_back=YEARS_BACK, today=TODAY
        )
    )

    assert [(outcome.symbol, outcome.status) for outcome in outcomes] == [("DEMO", "fresh")]


def test_stale_cache_still_reaches_the_slow_path(tmp_path):
    """The footer shortcut must not swallow the incremental top-up.

    With a cache ending before today, the fast path declines and
    ensure_daily_history runs; a cache-only loader then raises its documented
    RuntimeError, which the prefetch surfaces as a redacted failure.
    """
    loader = DailyDataLoader(client=None, cache_dir=tmp_path, request_delay_seconds=0.0)
    stale = _covering_frame()
    stale = stale.loc[stale["timestamp"] < pd.Timestamp(TODAY) - pd.Timedelta(days=30)]
    _write_cache(loader, stale, statistics=True)

    outcomes = list(
        loader.iter_ensure_universe_history(
            [_instrument()], years_back=YEARS_BACK, today=TODAY
        )
    )

    assert outcomes[0].status == "failed"
    assert "cache-only mode" in (outcomes[0].message or "")


def test_get_daily_history_miss_decision_reads_no_frame(monkeypatch, tmp_path):
    """An insufficient cache goes straight to the fetch, frame unread.

    Before PERF-002 the loader decompressed the whole cached frame, computed
    its bounds, discarded it, and fetched from Dhan. Now the footer answers
    the coverage question, so the only pandas work is the fetched result.
    """
    fetched = pd.DataFrame(
        {
            "timestamp": pd.date_range(date(2026, 6, 1), date(2026, 7, 10), freq="B"),
            "open": 100.0,
            "high": 110.0,
            "low": 95.0,
            "close": 105.0,
            "volume": 1000.0,
        }
    )

    class OneShotClient:
        """Serve one canned frame and count how often Dhan is called."""

        def __init__(self) -> None:
            self.calls = 0

        def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
            self.calls += 1
            return fetched.copy(deep=True)

    client = OneShotClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    short = fetched.loc[fetched["timestamp"] >= pd.Timestamp(date(2026, 7, 1))]
    _write_cache(loader, short, statistics=True)
    # Forbid reads AFTER writing the cache; to_parquet is unaffected.
    _forbid_full_reads(monkeypatch)

    frame, from_cache = loader.get_daily_history(
        _instrument(), date(2026, 6, 1), date(2026, 7, 10)
    )

    assert from_cache is False
    assert client.calls == 1
    assert not frame.empty


def test_get_daily_history_covered_range_without_statistics_is_still_a_hit(tmp_path):
    """PERF-002 must not turn any old cache hit into a Dhan fetch."""

    class ForbiddenClient:
        """Fail the test if the loader asks Dhan for anything."""

        def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
            raise AssertionError("a covered cache must not reach Dhan")

    loader = DailyDataLoader(ForbiddenClient(), cache_dir=tmp_path, request_delay_seconds=0.0)
    _write_cache(loader, _covering_frame(), statistics=False)

    frame, from_cache = loader.get_daily_history(
        _instrument(), TODAY - timedelta(days=30), TODAY
    )

    assert from_cache is True
    assert not frame.empty
    assert frame["timestamp"].min() >= pd.Timestamp(TODAY - timedelta(days=30))


def test_get_daily_history_rechecks_frame_after_footer_claim(monkeypatch, tmp_path):
    """A file replaced after its footer is read must not become a false hit.

    This simulates a concurrent writer replacing the cache between the cheap
    footer check and the full read. The frame actually returned by pandas is
    authoritative, so an insufficient replacement must trigger a refetch.
    """
    requested_start = date(2026, 6, 1)
    requested_end = TODAY
    replacement = _covering_frame().loc[
        lambda frame: frame["timestamp"] >= pd.Timestamp(date(2026, 7, 1))
    ]
    fetched = _covering_frame().loc[
        lambda frame: frame["timestamp"] >= pd.Timestamp(requested_start)
    ]

    class OneShotClient:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
            self.calls += 1
            return fetched.copy(deep=True)

    client = OneShotClient()
    loader = DailyDataLoader(client, cache_dir=tmp_path, request_delay_seconds=0.0)
    _write_cache(loader, _covering_frame(), statistics=True)
    monkeypatch.setattr(
        daily_data_loader,
        "timestamp_bounds",
        lambda _path: (requested_start, requested_end),
    )
    monkeypatch.setattr(
        daily_data_loader.pd,
        "read_parquet",
        lambda *_args, **_kwargs: replacement.copy(deep=True),
    )

    frame, from_cache = loader.get_daily_history(
        _instrument(), requested_start, requested_end
    )

    assert from_cache is False
    assert client.calls == 1
    assert frame["timestamp"].min() <= pd.Timestamp(requested_start)


def test_prefetch_requires_symbol_and_security_id(tmp_path):
    """Malformed rows still fail through the documented validation path."""
    loader = DailyDataLoader(client=None, cache_dir=tmp_path, request_delay_seconds=0.0)

    with pytest.raises(ValueError, match="missing symbol"):
        loader.ensure_daily_history({"symbol": "", "security_id": "1"}, today=TODAY)
    with pytest.raises(ValueError, match="missing security_id"):
        loader.ensure_daily_history({"symbol": "DEMO", "security_id": ""}, today=TODAY)
