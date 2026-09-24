"""Regressions for preserving shared candle history during bounded refreshes.

Beginner note:
A caller owns its requested date interval, not the whole symbol file. These
tests use real parquet files and fake vendors to catch history loss on disk.
"""

from __future__ import annotations

import concurrent.futures
import multiprocessing
import threading
from datetime import date
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from backend.daily_data_loader import DailyDataLoader
from backend.data_quality.cache_repair import repair_symbol
from backend.dhan_client import DhanDataClient

ROW = {"symbol": "TEST", "security_id": "123"}
TODAY = date(2026, 6, 10)


def _frame(dates: list[str], *, close: float = 104.0) -> pd.DataFrame:
    """Build valid candles whose dates make preservation failures obvious."""
    return pd.DataFrame({
        "timestamp": pd.to_datetime(dates), "open": 100.0, "high": 110.0,
        "low": 99.0, "close": close, "volume": 1000.0,
    })


class _Client:
    """Return a fresh copy so caller-side normalization cannot alter fixtures."""

    def __init__(self, frame: pd.DataFrame) -> None:
        self.frame = frame

    def fetch_daily_candles(self, **_kwargs: object) -> pd.DataFrame:
        return self.frame.copy()


def _loader(tmp_path: Path, client: object) -> DailyDataLoader:
    return DailyDataLoader(
        cast(DhanDataClient, client), cache_dir=tmp_path,
        request_delay_seconds=0.0, today_func=lambda: TODAY,
    )


@pytest.mark.parametrize("force", [False, True])
def test_bounded_refresh_keeps_both_ends_and_replaces_inclusive_interval(tmp_path: Path, force: bool):
    """A missing earlier boundary or forced refresh must never shrink history.

    Beginner note:
    June 8 is deliberately absent from the answer: replacing the interval must
    remove that old row, while June 1 and June 15 survive outside the interval.
    The non-forced case asks before the old cache's first date to force a miss.
    """
    response = _frame(["2026-06-05", "2026-06-09"], close=106.0)
    loader = _loader(tmp_path, _Client(response))
    path = loader.cache_path("TEST", "123")
    old_dates = ["2026-06-01", "2026-06-08", "2026-06-15"] if force else ["2026-06-08", "2026-06-15"]
    _frame(old_dates).to_parquet(path, index=False)

    result, hit = loader.get_daily_history(ROW, "2026-06-05", "2026-06-09", force_refresh=force)

    assert hit is False
    pd.testing.assert_frame_equal(result, response)
    stored = pd.read_parquet(path)
    expected = (["2026-06-01"] if force else []) + ["2026-06-05", "2026-06-09", "2026-06-15"]
    assert stored.timestamp.dt.strftime("%Y-%m-%d").tolist() == expected


def test_empty_refresh_keeps_cache_bytes_and_marker(tmp_path: Path):
    """No vendor rows are no authority to erase existing history or evidence."""
    loader = _loader(tmp_path, _Client(pd.DataFrame()))
    path = loader.cache_path("TEST", "123")
    _frame(["2026-06-01", "2026-06-10"]).to_parquet(path, index=False)
    marker = loader.first_bar_path("TEST", "123")
    marker.write_text("prior evidence", encoding="utf-8")
    before = path.read_bytes()

    result, hit = loader.get_daily_history(ROW, "2026-06-05", "2026-06-09", force_refresh=True)

    assert result.empty and not hit
    assert path.read_bytes() == before
    assert marker.read_text(encoding="utf-8") == "prior evidence"


def test_direct_fetch_clips_vendor_extras_but_records_raw_first_bar(tmp_path: Path):
    """Out-of-request vendor corrections cannot modify another caller's dates.

    Beginner note:
    The vendor's June 1 bar proves it ignored the June 5 lower bound. Keep the
    old June 1 price, and do not claim June 5 is the vendor's first available bar
    merely because clipping made it the first row that this caller stores.
    """
    loader = _loader(tmp_path, _Client(_frame(["2026-06-01", "2026-06-05", "2026-06-15"], close=106.0)))
    path = loader.cache_path("TEST", "123")
    original = _frame(["2026-06-01", "2026-06-15"])
    original.to_parquet(path, index=False)

    result, _hit = loader.get_daily_history(ROW, "2026-06-05", "2026-06-09", force_refresh=True)

    assert result.timestamp.tolist() == [pd.Timestamp("2026-06-05")]
    stored = pd.read_parquet(path)
    assert stored.close.tolist() == [104.0, 106.0, 104.0]
    assert not loader.first_bar_path("TEST", "123").exists()


@pytest.mark.parametrize("mode", ["incremental", "backfilled", "fresh_download"])
def test_ensure_rereads_cache_after_fetch_before_merging(tmp_path: Path, mode: str):
    """Every prefetch branch must preserve a download that completes during I/O.

    Beginner note:
    Completing the other writer inside the fake vendor gives a deterministic
    lost-update interleaving without a scheduler-dependent sleep. A lock around
    network work would deadlock this test; a stale merge would erase June 15.
    """
    concurrent = _loader(tmp_path, _Client(_frame(["2026-06-15"])))

    class Client:
        def fetch_daily_candles(self, **_kwargs: object) -> pd.DataFrame:
            concurrent.get_daily_history(ROW, "2026-06-15", "2026-06-15", force_refresh=True)
            return _frame(["2026-06-10"])

    loader = _loader(tmp_path, Client())
    path = loader.cache_path("TEST", "123")
    if mode != "fresh_download":
        first = "2025-06-10" if mode == "incremental" else "2026-06-08"
        _frame([first, "2026-06-09"]).to_parquet(path, index=False)

    _result, status = loader.ensure_daily_history(ROW, years_back=1, today=TODAY)

    assert status == mode
    assert pd.Timestamp("2026-06-15") in pd.read_parquet(path).timestamp.tolist()


def _process_refresh(cache_dir, day, barrier):
    """Refresh one day's interval from a separately spawned worker.

    Args:
        cache_dir: Shared directory containing the symbol's existing Parquet.
        day: ISO date used as both inclusive bounds of this worker's request.
        barrier: Process-shared rendezvous reached inside each fake vendor call.

    Beginner note:
    Spawn creates a fresh interpreter and its own Python lock registry, so only
    the OS lock can coordinate these workers. Placing the barrier in the vendor
    makes both requests reach the network phase before either may publish. It
    also catches a writer that wrongly holds the cache lock during network I/O:
    the other worker could not reach the barrier, and the bounded wait fails.
    """
    class Client:
        def fetch_daily_candles(self, **_kwargs):
            barrier.wait(timeout=30)
            return _frame([day])

    _loader(Path(cache_dir), Client()).get_daily_history(ROW, day, day, force_refresh=True)


def test_disjoint_process_fetches_preserve_each_others_rows(tmp_path: Path):
    """Keep the original history and both disjoint spawned-worker refreshes.

    Beginner note:
    The June 5 and June 9 requests own different one-day intervals, so neither
    may erase the other's result or the original June 1 row. The vendor barrier
    brings both independent processes to publication together; a stale merge
    or whole-file replacement can lose one of those three days. The final file
    assertion checks preservation regardless of which worker publishes first.
    Bounded joins expose deadlocks and worker failures instead of hanging the
    suite. The finally block terminates any surviving child even when an earlier
    assertion fails, keeping a failed concurrency test from stranding processes.
    """
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(2)
    loader = _loader(tmp_path, _Client(pd.DataFrame()))
    path = loader.cache_path("TEST", "123")
    _frame(["2026-06-01"]).to_parquet(path, index=False)
    workers = [context.Process(target=_process_refresh, args=(str(tmp_path), day, barrier))
               for day in ["2026-06-05", "2026-06-09"]]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=45)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=10)
    assert pd.read_parquet(path).timestamp.dt.strftime("%Y-%m-%d").tolist() == [
        "2026-06-01", "2026-06-05", "2026-06-09",
    ]


def test_refresh_preserves_conflicts_in_vendor_answer(tmp_path: Path):
    """Two vendor prices on one date stay visible to the quality quarantine."""
    response = pd.concat([_frame(["2026-06-05"]), _frame(["2026-06-05"], close=106.0)], ignore_index=True)
    loader = _loader(tmp_path, _Client(response))
    path = loader.cache_path("TEST", "123")
    _frame(["2026-06-01", "2026-06-10"]).to_parquet(path, index=False)

    loader.get_daily_history(ROW, "2026-06-05", "2026-06-05", force_refresh=True)

    stored = pd.read_parquet(path)
    assert len(stored) == 4
    assert stored.loc[stored.timestamp.eq(pd.Timestamp("2026-06-05")), "close"].tolist() == [104.0, 106.0]


def test_disjoint_thread_fetches_preserve_each_others_rows(tmp_path: Path):
    """Both calls finish their network request before either may publish.

    Beginner note:
    The barrier makes both writers start from the same old file. Re-reading
    under the shared lock is necessary; serializing just the rename still loses
    the first writer's interval when the second publishes its stale snapshot.
    """
    barrier = threading.Barrier(2)

    class Client:
        def fetch_daily_candles(self, *, from_date: str, **_kwargs: object) -> pd.DataFrame:
            barrier.wait(timeout=10)
            return _frame([from_date])

    first = _loader(tmp_path, Client())
    second = _loader(tmp_path, Client())
    path = first.cache_path("TEST", "123")
    _frame(["2026-06-01"]).to_parquet(path, index=False)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(loader.get_daily_history, ROW, day, day, True)
                   for loader, day in [(first, "2026-06-05"), (second, "2026-06-09")]]
        for future in futures:
            future.result(timeout=20)
    assert pd.read_parquet(path).timestamp.dt.strftime("%Y-%m-%d").tolist() == [
        "2026-06-01", "2026-06-05", "2026-06-09",
    ]


def test_repair_refuses_to_overwrite_download_completed_during_vendor_call(tmp_path: Path):
    """Reject both a stale repair candidate and its stale retry marker.

    Beginner note:
    Repair captures the conflicting June 8 cache and its content revision before
    asking the fake vendor for a correction. Inside that vendor call, a separate
    loader publishes June 10, changing the file while repair still reasons about
    its old input. This callback fixes the ordering without timing-based sleeps.
    Returning skipped after the revision comparison protects the new June 10
    row from the candidate that contains only June 8 and June 9. The absence of
    .repaired is equally important: a rejected decision must not leave a retry
    marker that could suppress a subsequent repair against the current file.
    """
    concurrent = _loader(tmp_path, _Client(_frame(["2026-06-10"])))

    class Client:
        def fetch_daily_candles(self, **_kwargs: object) -> pd.DataFrame:
            concurrent.get_daily_history(ROW, "2026-06-10", "2026-06-10", force_refresh=True)
            return _frame(["2026-06-08", "2026-06-09"])

    loader = _loader(tmp_path, Client())
    path = loader.cache_path("TEST", "123")
    pd.concat([_frame(["2026-06-08"]), _frame(["2026-06-08"], close=106.0)]).to_parquet(path, index=False)

    outcome = repair_symbol(loader, ROW, today=TODAY, force=True)

    assert outcome.status == "skipped"
    assert "changed" in (outcome.message or "")
    assert pd.Timestamp("2026-06-10") in pd.read_parquet(path).timestamp.tolist()
    assert not path.with_suffix(".repaired").exists()


def test_forced_refresh_leaves_unreadable_cache_untouched(tmp_path: Path):
    """A failed old-file read must not turn a narrow fetch into destructive recovery."""
    loader = _loader(tmp_path, _Client(_frame(["2026-06-09"])))
    path = loader.cache_path("TEST", "123")
    path.write_bytes(b"unreadable original cache")

    with pytest.raises(Exception):
        loader.get_daily_history(ROW, "2026-06-09", "2026-06-09", force_refresh=True)

    assert path.read_bytes() == b"unreadable original cache"


def test_future_vendor_first_bar_never_creates_evidence(tmp_path: Path):
    """An impossible future candle cannot produce a future-dated evidence marker."""
    loader = _loader(tmp_path, _Client(_frame(["2026-06-15"])))

    loader.get_daily_history(ROW, "2026-06-01", "2026-06-15")

    assert not loader.first_bar_path("TEST", "123").exists()


def test_interrupted_serialization_keeps_original_bytes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """A serializer that fails after writing its header cannot damage the live file."""
    loader = _loader(tmp_path, _Client(_frame(["2026-06-09"])))
    path = loader.cache_path("TEST", "123")
    _frame(["2026-06-01"]).to_parquet(path, index=False)
    before = path.read_bytes()

    def interrupted(_frame: pd.DataFrame, target: Path, **_kwargs: object) -> None:
        Path(target).write_bytes(b"partial parquet header")
        raise OSError("simulated interrupted write")

    monkeypatch.setattr(pd.DataFrame, "to_parquet", interrupted)
    with pytest.raises(OSError, match="interrupted"):
        loader.get_daily_history(ROW, "2026-06-09", "2026-06-09", force_refresh=True)

    assert path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def _with_undateable_row(frame: pd.DataFrame) -> pd.DataFrame:
    """Append one raw vendor row whose timestamp cannot be parsed."""
    bad = pd.DataFrame({"timestamp": [pd.NaT], "open": [1.0], "high": [1.0], "low": [1.0],
                        "close": [1.0], "volume": [1.0]})
    return pd.concat([frame, bad], ignore_index=True)


def test_full_window_refresh_replaces_undateable_cached_rows(tmp_path: Path):
    """A refetch that covers every dated cached row is authoritative for all of it.

    Beginner note:
    An undateable row cannot be placed inside or outside a narrow window, so a
    narrow refresh keeps it as evidence. When the requested window spans the
    cache's whole dated range, though, the fresh vendor answer describes that
    entire history; a stale NaT row would otherwise survive every refresh and
    keep the symbol dirty until the separate repair job ran.
    """
    response = _frame(["2026-06-01", "2026-06-05", "2026-06-09"], close=106.0)
    loader = _loader(tmp_path, _Client(response))
    path = loader.cache_path("TEST", "123")
    _with_undateable_row(_frame(["2026-06-02", "2026-06-08"])).to_parquet(path, index=False)

    loader.get_daily_history(ROW, "2026-06-01", "2026-06-09", force_refresh=True)

    stored = pd.read_parquet(path)
    assert stored["timestamp"].notna().all()
    assert stored.timestamp.dt.strftime("%Y-%m-%d").tolist() == ["2026-06-01", "2026-06-05", "2026-06-09"]


def test_narrow_refresh_keeps_undateable_rows_as_evidence(tmp_path: Path):
    loader = _loader(tmp_path, _Client(_frame(["2026-06-05"], close=106.0)))
    path = loader.cache_path("TEST", "123")
    _with_undateable_row(_frame(["2026-06-01", "2026-06-05", "2026-06-09"])).to_parquet(path, index=False)

    loader.get_daily_history(ROW, "2026-06-05", "2026-06-05", force_refresh=True)

    assert int(pd.read_parquet(path)["timestamp"].isna().sum()) == 1


def test_publish_retries_a_transient_sharing_violation(tmp_path: Path, monkeypatch):
    """Windows refuses to replace a parquet an unlocked reader has open.

    Beginner note:
    Readers take no lock (atomic replace is what protects them), so a scan that
    is mid-read when the prefetch publishes makes ``os.replace`` raise
    ``PermissionError`` on Windows. The read finishes quickly; a short bounded
    retry publishes instead of failing that symbol's refresh.
    """
    import os
    from types import SimpleNamespace

    from backend import candle_cache

    real_replace = os.replace
    attempts: list[int] = []
    sleeps: list[float] = []

    def flaky(src, dst):
        attempts.append(1)
        if len(attempts) < 3:
            raise PermissionError(13, "file in use")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky)
    monkeypatch.setattr(candle_cache, "time", SimpleNamespace(sleep=sleeps.append), raising=False)
    monkeypatch.setattr(candle_cache, "_REPLACE_ATTEMPTS", 5, raising=False)
    destination = tmp_path / "TEST_123.parquet"

    candle_cache.atomic_write_parquet(_frame(["2026-06-01"]), destination)

    assert len(attempts) == 3 and len(sleeps) == 2
    assert pd.read_parquet(destination).shape[0] == 1
    assert [p.name for p in tmp_path.iterdir()] == ["TEST_123.parquet"]


def test_publish_gives_up_after_bounded_retries_and_cleans_temp(tmp_path: Path, monkeypatch):
    import os
    from types import SimpleNamespace

    from backend import candle_cache

    def always_locked(src, dst):
        raise PermissionError(13, "file in use")

    monkeypatch.setattr(os, "replace", always_locked)
    monkeypatch.setattr(candle_cache, "time", SimpleNamespace(sleep=lambda _s: None), raising=False)
    monkeypatch.setattr(candle_cache, "_REPLACE_ATTEMPTS", 3, raising=False)

    with pytest.raises(PermissionError):
        candle_cache.atomic_write_parquet(_frame(["2026-06-01"]), tmp_path / "TEST_123.parquet")
    assert list(tmp_path.iterdir()) == []


def test_stale_cleanup_removes_old_temp_files_but_never_locks(tmp_path: Path):
    """A writer killed mid-publish leaves an inert temp file behind forever."""
    import os
    import time

    loader = _loader(tmp_path, _Client(pd.DataFrame()))
    old_tmp = tmp_path / ".TEST_123.abcd1234.tmp"
    new_tmp = tmp_path / ".TEST_123.efgh5678.tmp"
    lock = tmp_path / "TEST_123.lock"
    for item in (old_tmp, new_tmp, lock):
        item.write_bytes(b"\0")
    long_ago = time.time() - 10 * 86400
    os.utime(old_tmp, (long_ago, long_ago))
    os.utime(lock, (long_ago, long_ago))

    loader.cleanup_stale_cache_files(max_age_days=3)

    assert not old_tmp.exists()
    assert new_tmp.exists() and lock.exists()
