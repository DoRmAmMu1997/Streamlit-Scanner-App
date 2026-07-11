"""Unit tests for the footer-statistics date bounds helper (PERF-002).

The contract under test: ``timestamp_bounds`` answers from the Parquet footer
alone, and returns ``(None, None)`` — never a guess, never an exception —
whenever the footer cannot answer authoritatively. The loader's full-read
fallback depends on that fail-safe shape.
"""

from __future__ import annotations

import datetime as dt

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from backend.parquet_stats import timestamp_bounds


def _candles(dates: list[dt.date]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": [pd.Timestamp(value) for value in dates],
            "open": [100.0] * len(dates),
            "close": [101.0] * len(dates),
        }
    )


def test_bounds_from_a_normal_pandas_written_cache_file(tmp_path):
    path = tmp_path / "DEMO_1.parquet"
    _candles(
        [dt.date(2016, 7, 11), dt.date(2020, 1, 2), dt.date(2026, 7, 10)]
    ).to_parquet(path, index=False)

    assert timestamp_bounds(path) == (dt.date(2016, 7, 11), dt.date(2026, 7, 10))


def test_bounds_span_multiple_row_groups(tmp_path):
    """Min/max must be folded across ALL row groups, not read from the first."""
    path = tmp_path / "DEMO_1.parquet"
    frame = _candles(
        [dt.date(2020, 1, 2), dt.date(2021, 6, 1), dt.date(2024, 3, 3), dt.date(2026, 7, 10)]
    )
    # row_group_size=2 forces two groups; the true bounds straddle them.
    pq.write_table(pa.Table.from_pandas(frame), path, row_group_size=2)
    assert pq.ParquetFile(path).metadata.num_row_groups == 2

    assert timestamp_bounds(path) == (dt.date(2020, 1, 2), dt.date(2026, 7, 10))


def test_missing_file_returns_none_pair(tmp_path):
    assert timestamp_bounds(tmp_path / "absent.parquet") == (None, None)


def test_non_parquet_file_returns_none_pair(tmp_path):
    path = tmp_path / "corrupt.parquet"
    path.write_bytes(b"this is not a parquet footer")
    assert timestamp_bounds(path) == (None, None)


def test_missing_timestamp_column_returns_none_pair(tmp_path):
    path = tmp_path / "DEMO_1.parquet"
    pd.DataFrame({"close": [1.0, 2.0]}).to_parquet(path, index=False)
    assert timestamp_bounds(path) == (None, None)


def test_empty_frame_returns_none_pair(tmp_path):
    path = tmp_path / "DEMO_1.parquet"
    _candles([]).to_parquet(path, index=False)
    assert timestamp_bounds(path) == (None, None)


def test_all_null_timestamps_return_none_pair(tmp_path):
    """A column of NaT has no min/max statistics — must fall back, not guess."""
    path = tmp_path / "DEMO_1.parquet"
    pd.DataFrame(
        {"timestamp": pd.to_datetime([None, None]), "close": [1.0, 2.0]}
    ).to_parquet(path, index=False)
    assert timestamp_bounds(path) == (None, None)


def test_writer_without_statistics_returns_none_pair(tmp_path):
    """``write_statistics=False`` is the canonical 'footer cannot answer' case."""
    path = tmp_path / "DEMO_1.parquet"
    frame = _candles([dt.date(2020, 1, 2), dt.date(2026, 7, 10)])
    pq.write_table(pa.Table.from_pandas(frame), path, write_statistics=False)

    assert timestamp_bounds(path) == (None, None)


def test_non_datelike_timestamp_column_returns_none_pair(tmp_path):
    """Integer statistics in a mistyped 'timestamp' column must not be trusted."""
    path = tmp_path / "DEMO_1.parquet"
    pd.DataFrame({"timestamp": [5, 9], "close": [1.0, 2.0]}).to_parquet(path, index=False)

    assert timestamp_bounds(path) == (None, None)
