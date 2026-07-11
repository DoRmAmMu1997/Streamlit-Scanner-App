"""Cheap candle-date bounds from Parquet footer statistics (PERF-002).

Beginner note:
Parquet files end with a footer that records optional per-row-group
minimum/maximum statistics for every column. pandas' ``to_parquet`` (via
pyarrow) writes those statistics by default, so for the daily candle cache
the first and last candle dates can usually be estimated by reading a few
kilobytes of footer instead of decompressing a whole multi-year frame.
``backend/health.py`` has used this trick for its cache snapshot since
OBS-002; this module generalizes it for the data loader's cache-coverage
miss decisions ("does this old file definitely fail to cover the requested
range?"), which previously loaded an entire frame that was then discarded.

Footer bounds are advisory. A valid footer can coexist with corrupt data pages
or describe a file that a concurrent writer replaces before the caller reads
it. Callers must validate the frame they actually use before returning a cache
hit or reporting a cache as fresh.

Callers MUST treat ``(None, None)`` as "the footer cannot answer cheaply" and
fall back to their existing full-read logic — never as "the file is empty".
Statistics can be legitimately absent (a writer passed
``write_statistics=False``, an all-null column, a truncated file), and the
loader's behavior for those files has to stay exactly what it was before
PERF-002.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq


def timestamp_bounds(path: Path) -> tuple[dt.date | None, dt.date | None]:
    """Return ``(first, last)`` candle dates using footer statistics only.

    Reads the Parquet footer (schema + row-group statistics) and never the
    data pages. Returns ``(None, None)`` whenever the footer cannot answer
    authoritatively: missing file, no ``timestamp`` column, zero row groups,
    any row group without min/max statistics, a non-date-like statistic, or
    any read/parse error. Deliberately never raises — the caller's full-read
    fallback is the error handler.
    """
    try:
        parquet_file = pq.ParquetFile(path)
        timestamp_index = parquet_file.schema_arrow.get_field_index("timestamp")
        if timestamp_index < 0:
            return (None, None)
        metadata = parquet_file.metadata
        if metadata.num_row_groups == 0:
            return (None, None)

        earliest: dt.date | None = None
        latest: dt.date | None = None
        for row_group_index in range(metadata.num_row_groups):
            column = metadata.row_group(row_group_index).column(timestamp_index)
            statistics = column.statistics
            # PyArrow exposes one flag for the min/max pair. Older versions do
            # not provide a separate ``has_max`` attribute, so use the stable
            # ``has_min_max`` API before reading either value. One statless
            # row group makes the whole answer untrustworthy: its rows could
            # extend past every other group's bounds.
            if statistics is None or not statistics.has_min_max:
                return (None, None)
            first = _as_date(statistics.min)
            last = _as_date(statistics.max)
            if first is None or last is None:
                return (None, None)
            if earliest is None or first < earliest:
                earliest = first
            if latest is None or last > latest:
                latest = last
        return (earliest, latest)
    except Exception:
        # PyArrow raises several format-specific exception classes for corrupt
        # or non-Parquet files. All of them mean the same thing here: the
        # footer cannot answer, so the caller must use its full-read fallback.
        return (None, None)


def _as_date(value: Any) -> dt.date | None:
    """Normalize common PyArrow timestamp-statistic values to a date.

    Mirrors the coercion ``backend/health.py`` applies to the same statistics
    (datetime/date objects, pandas Timestamps via ``to_pydatetime``, ISO
    strings). Anything else — e.g. integer or bytes statistics from a column
    that is not really a timestamp — returns ``None`` so the caller falls
    back rather than trusting a mistyped column.
    """
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if hasattr(value, "to_pydatetime"):
        return value.to_pydatetime().date()
    try:
        return dt.datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None
