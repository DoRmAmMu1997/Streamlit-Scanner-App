"""Tests for remembering how far back the vendor's history actually goes (DATA-004).

Beginner note:
The cache-coverage test asks "does this file reach back to the start of the
requested window?". For a stock that listed *after* that start — DMART listed in
2017, well inside a ten-year window — the answer is permanently no, because DhanHQ
has nothing earlier to give. Before this change every prefetch and every scan
re-downloaded those symbols' whole history, wrote the same short frame back, and
did it again next time. 200 of 577 cached symbols were in that state.

The fix records what the vendor actually served, following the ``.checked`` and
``.repaired`` sidecar precedent already in this codebase: once we have asked from
a given date and learned the earliest bar that exists, a cache reaching that bar is
as complete as it can ever be.

The distinction these tests protect is the whole point:

- an **interrupted prefetch** left a partial file and the vendor *does* have
  earlier data → must still refetch;
- a **later listing** means the vendor has nothing earlier → refetching is waste.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from typing import cast

import pandas as pd

from backend.daily_data_loader import DailyDataLoader
from backend.dhan_client import DhanDataClient

TODAY = date(2026, 8, 24)
HISTORY_START = date(2016, 8, 24)  # TODAY minus ten years
LISTED_ON = date(2017, 3, 21)  # a DMART-style later listing
ROW = {"symbol": "DMART", "security_id": "1"}


def _month_series(first: date, last: date) -> pd.DatetimeIndex:
    """Monthly bars that begin exactly on ``first`` and end on or before ``last``.

    ``pd.date_range(..., freq="MS")`` snaps to month starts, which would silently
    move a mid-month listing date *and* leave the newest bar weeks short of the
    requested end. Both endpoints are therefore inserted explicitly: these tests
    turn on the first bar being exactly the listing date, and a snapped last bar
    would make the frame fail the freshness half of the coverage test for reasons
    that have nothing to do with what is being tested.
    """
    months = pd.date_range(first, last, freq="MS")
    return (
        pd.DatetimeIndex([pd.Timestamp(first), *months, pd.Timestamp(last)])
        .unique()
        .sort_values()
    )


class ListedLateClient:
    """A vendor that has no data before ``listed_on``, whatever you ask for.

    This is the real behaviour that made the coverage test unsatisfiable: the
    request reaches back ten years, the response starts at the listing date.
    """

    def __init__(self, listed_on: date = LISTED_ON, *, through: date = TODAY) -> None:
        self.listed_on = listed_on
        self.through = through
        # The loader passes real dates on every path these tests exercise.
        self.calls: list[tuple[date, date]] = []

    def fetch_daily_candles(self, *, from_date, to_date, **_kwargs) -> pd.DataFrame:
        self.calls.append((from_date, to_date))
        # Monthly bars keep the fixture small; only the date bounds matter. The
        # listing date itself is forced in because "MS" snaps to month starts,
        # and the first bar being exactly the listing date is the whole point.
        dates = _month_series(self.listed_on, self.through)
        return pd.DataFrame(
            {
                "timestamp": dates,
                "open": 100.0,
                "high": 105.0,
                "low": 99.0,
                "close": 104.0,
                "volume": 1_000.0,
            }
        )


def _loader(tmp_path: Path, client: object) -> DailyDataLoader:
    # The loader only calls fetch_daily_candles, so the duck-typed fake stands in.
    return DailyDataLoader(
        cast(DhanDataClient, client), cache_dir=tmp_path, request_delay_seconds=0.0
    )


def _write_cache(loader: DailyDataLoader, first: date, last: date) -> Path:
    path = loader.cache_path(ROW["symbol"], ROW["security_id"])
    pd.DataFrame(
        {
            "timestamp": _month_series(first, last),
            "open": 100.0,
            "high": 105.0,
            "low": 99.0,
            "close": 104.0,
            "volume": 1_000.0,
        }
    ).to_parquet(path, index=False)
    return path


def _marker(loader: DailyDataLoader) -> Path:
    return loader.first_bar_path(ROW["symbol"], ROW["security_id"])


# ---------------------------------------------------------------------------
# Learning the vendor's earliest bar
# ---------------------------------------------------------------------------


def test_a_full_download_records_the_vendors_earliest_bar(tmp_path: Path):
    client = ListedLateClient()
    loader = _loader(tmp_path, client)

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "fresh_download"
    payload = json.loads(_marker(loader).read_text(encoding="utf-8"))
    # We asked from the ten-year start and learned the vendor begins at listing.
    assert date.fromisoformat(payload["requested_from"]) == HISTORY_START
    assert date.fromisoformat(payload["earliest_available"]) == LISTED_ON


def test_no_marker_is_written_when_the_vendor_covers_the_whole_window(tmp_path: Path):
    """Nothing to remember when the request was fully satisfied."""
    client = ListedLateClient(listed_on=date(2016, 1, 1))
    loader = _loader(tmp_path, client)

    loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert not _marker(loader).exists()


def test_an_empty_response_records_nothing(tmp_path: Path):
    """An empty answer is no evidence about how far back history goes."""

    class EmptyClient:
        def fetch_daily_candles(self, **_kwargs) -> pd.DataFrame:
            return pd.DataFrame()

    loader = _loader(tmp_path, EmptyClient())

    loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert not _marker(loader).exists()


# ---------------------------------------------------------------------------
# The bug: re-downloading a later listing forever
# ---------------------------------------------------------------------------


def test_a_later_listing_is_not_backfilled_again_on_the_next_pass(tmp_path: Path):
    """The core defect. Second prefetch must not re-download the same history."""
    client = ListedLateClient()
    loader = _loader(tmp_path, client)

    _frame, first_status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)
    calls_after_first = len(client.calls)
    _frame, second_status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert first_status == "fresh_download"
    assert second_status != "backfilled"
    # The tail top-up may still ask; the ten-year backfill must not.
    backfills = [c for c in client.calls[calls_after_first:] if c[0] == HISTORY_START]
    assert backfills == []


def test_a_later_listing_still_gets_its_daily_top_up(tmp_path: Path):
    """Skipping the pointless backfill must not freeze the symbol's tail.

    The whole value of the cache is that it keeps advancing; suppressing the
    backfill must only suppress the backfill.
    """
    client = ListedLateClient()
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=LISTED_ON, last=TODAY - timedelta(days=40))
    loader._write_vendor_earliest(
        ROW["symbol"], ROW["security_id"], requested_from=HISTORY_START,
        earliest_available=LISTED_ON, recorded_on=TODAY,
    )

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "incremental"
    # The request started after the cached tail, not at the ten-year start.
    assert client.calls[-1][0] > TODAY - timedelta(days=41)


def test_a_later_listing_is_a_cache_hit_for_scans(tmp_path: Path):
    """The scan path benefits too: no fetch, no rewrite."""
    client = ListedLateClient()
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=LISTED_ON, last=TODAY)
    loader._write_vendor_earliest(
        ROW["symbol"], ROW["security_id"], requested_from=HISTORY_START,
        earliest_available=LISTED_ON, recorded_on=TODAY,
    )

    _frame, from_cache = loader.get_daily_history(
        ROW, start_date=HISTORY_START, end_date=TODAY
    )

    assert from_cache is True
    assert client.calls == []


# ---------------------------------------------------------------------------
# The distinction that must not be lost
# ---------------------------------------------------------------------------


def test_a_genuinely_partial_cache_is_still_backfilled(tmp_path: Path):
    """An interrupted prefetch, where the vendor DOES have earlier data.

    The marker says history begins in 2016, but the cache starts in 2020 — so
    there are real bars missing and the backfill must run.
    """
    client = ListedLateClient(listed_on=date(2016, 1, 1))
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=date(2020, 1, 1), last=TODAY)
    loader._write_vendor_earliest(
        ROW["symbol"], ROW["security_id"], requested_from=HISTORY_START,
        earliest_available=date(2016, 1, 1), recorded_on=TODAY,
    )

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "backfilled"


def test_a_shallower_probe_does_not_prove_anything_about_earlier_history(tmp_path: Path):
    """A marker from a 5-year probe cannot answer a 10-year request.

    Learning that nothing exists before 2021 when you only asked from 2021 says
    nothing about 2016, so the deeper request must still go to the vendor.
    """
    client = ListedLateClient(listed_on=date(2021, 6, 1))
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=date(2021, 6, 1), last=TODAY)
    loader._write_vendor_earliest(
        ROW["symbol"], ROW["security_id"], requested_from=date(2021, 1, 1),
        earliest_available=date(2021, 6, 1), recorded_on=TODAY,
    )

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "backfilled"


def test_a_stale_marker_is_re_probed(tmp_path: Path):
    """Vendors do occasionally backfill history, so the belief expires."""
    client = ListedLateClient()
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=LISTED_ON, last=TODAY)
    loader._write_vendor_earliest(
        ROW["symbol"], ROW["security_id"], requested_from=HISTORY_START,
        earliest_available=LISTED_ON, recorded_on=date(2026, 1, 1),
    )

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "backfilled"


def test_an_unreadable_marker_fails_open_to_a_refetch(tmp_path: Path):
    """A corrupt marker may cost a request; it must never hide missing history."""
    client = ListedLateClient()
    loader = _loader(tmp_path, client)
    _write_cache(loader, first=LISTED_ON, last=TODAY)
    _marker(loader).write_text("not json", encoding="utf-8")

    _frame, status = loader.ensure_daily_history(ROW, years_back=10, today=TODAY)

    assert status == "backfilled"


# ---------------------------------------------------------------------------
# Housekeeping
# ---------------------------------------------------------------------------


def test_orphan_first_bar_markers_are_cleaned_up(tmp_path: Path):
    """`.firstbar` travels with its parquet, like `.checked` and `.repaired`."""
    loader = _loader(tmp_path, ListedLateClient())
    orphan = tmp_path / "GONE_9.firstbar"
    orphan.write_text("{}", encoding="utf-8")

    removed = loader.cleanup_stale_cache_files(max_age_days=30)

    assert removed >= 1
    assert not orphan.exists()


def test_the_marker_suffix_is_distinct_from_the_other_sidecars(tmp_path: Path):
    loader = _loader(tmp_path, ListedLateClient())
    path = _marker(loader)

    assert path.suffix == ".firstbar"
    assert path.with_suffix(".checked") != path
    assert path.with_suffix(".repaired") != path
