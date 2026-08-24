"""Guard: no loader write path may persist a vendor's duplicate bar (DATA-003).

Beginner note:
DhanHQ sometimes repeats a candle verbatim in its response. Before this guard, the
loader had six places that wrote a frame to the cache and only *one* of them
de-duplicated first, so a redundant bar reached disk, failed DATA-001's
DUPLICATE_DATE check, and silently dropped the symbol from every scan.

That is exactly what happened live: the DATA-002 repair cleaned the cache during
the prefetch, and the very next scan's cache-miss re-download put the duplicates
straight back. This file locks the invariant at every entry point rather than
trusting each call site to remember.
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pandas as pd

from backend.daily_data_loader import DailyDataLoader
from backend.data_quality.candles import validate_candles
from backend.dhan_client import DhanDataClient, normalize_daily_response

TODAY = date(2026, 8, 24)
ROW = {"symbol": "DEMO", "security_id": "1"}


class DuplicatingClient:
    """A client whose vendor response repeats one bar verbatim, as Dhan does."""

    def __init__(self) -> None:
        self.calls = 0

    def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
        self.calls += 1
        # Routed through the real normalizer so the test exercises the same
        # boundary production uses, not a hand-built clean frame.
        return normalize_daily_response(
            {
                "status": "success",
                "data": [
                    {
                        "timestamp": (TODAY - timedelta(days=offset)).isoformat(),
                        "open": 100.0,
                        "high": 105.0,
                        "low": 99.0,
                        "close": 104.0,
                        "volume": 1_000.0,
                    }
                    # `4` appears twice: the repeated bar.
                    for offset in (8, 7, 6, 5, 4, 4, 3, 2, 1, 0)
                ],
            }
        )


def _assert_cache_is_clean(loader: DailyDataLoader) -> pd.DataFrame:
    """The cached parquet must not carry a DUPLICATE_DATE finding."""
    stored = pd.read_parquet(loader.cache_path(ROW["symbol"], ROW["security_id"]))
    report = validate_candles(stored, symbol="DEMO", expected_latest_date=TODAY)
    assert "DUPLICATE_DATE" not in {finding.code for finding in report.findings}
    return stored


def test_get_daily_history_cache_miss_writes_no_duplicates(tmp_path: Path):
    """The path that re-dirtied the cache after every repair."""
    loader = DailyDataLoader(
        cast(DhanDataClient, DuplicatingClient()),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )

    loader.get_daily_history(ROW, start_date=TODAY - timedelta(days=8), end_date=TODAY)

    _assert_cache_is_clean(loader)


def test_ensure_daily_history_fresh_download_writes_no_duplicates(tmp_path: Path):
    """First-ever download for a symbol."""
    loader = DailyDataLoader(
        cast(DhanDataClient, DuplicatingClient()),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )

    _frame, status = loader.ensure_daily_history(ROW, years_back=1, today=TODAY)

    assert status == "fresh_download"
    _assert_cache_is_clean(loader)


def test_ensure_daily_history_backfill_writes_no_duplicates(tmp_path: Path):
    """A cache current at the back but missing early history is refetched whole."""
    loader = DailyDataLoader(
        cast(DhanDataClient, DuplicatingClient()),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )
    # Recent-only cache, so the backfill branch runs.
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime([TODAY - timedelta(days=1), TODAY]),
            "open": [100.0, 100.0],
            "high": [105.0, 105.0],
            "low": [99.0, 99.0],
            "close": [104.0, 104.0],
            "volume": [1_000.0, 1_000.0],
        }
    ).to_parquet(loader.cache_path(ROW["symbol"], ROW["security_id"]), index=False)

    _frame, status = loader.ensure_daily_history(ROW, years_back=5, today=TODAY)

    assert status == "backfilled"
    _assert_cache_is_clean(loader)


def test_a_conflicting_bar_still_reaches_the_cache_to_be_reported(tmp_path: Path):
    """The guard must not become a silent price-picker.

    Two bars sharing a date but disagreeing on value are a real vendor conflict.
    They must survive to disk so DATA-001 quarantines the symbol and the DATA-002
    repair resolves them, rather than one being quietly discarded here.
    """

    class ConflictingClient:
        def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
            return normalize_daily_response(
                {
                    "status": "success",
                    "data": [
                        {
                            "timestamp": TODAY.isoformat(),
                            "open": 100.0, "high": 105.0, "low": 99.0,
                            "close": 104.0, "volume": 1_000.0,
                        },
                        {
                            "timestamp": TODAY.isoformat(),
                            "open": 12.0, "high": 13.0, "low": 11.0,
                            "close": 12.5, "volume": 2_000.0,
                        },
                    ],
                }
            )

    loader = DailyDataLoader(
        cast(DhanDataClient, ConflictingClient()),
        cache_dir=tmp_path,
        request_delay_seconds=0.0,
    )

    loader.get_daily_history(ROW, start_date=TODAY - timedelta(days=1), end_date=TODAY)

    stored = pd.read_parquet(loader.cache_path(ROW["symbol"], ROW["security_id"]))
    report = validate_candles(stored, symbol="DEMO", expected_latest_date=TODAY)
    assert "DUPLICATE_DATE" in {finding.code for finding in report.findings}
