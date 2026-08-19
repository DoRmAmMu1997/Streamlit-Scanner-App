"""Tests for the candle cache repair executor (DATA-002).

Beginner note:
Where ``test_candle_repair_planner.py`` checks the *decisions*, this file checks
the *doing*: reading a cached parquet, carrying the plan out, re-downloading from
a fake Dhan client, writing the file back atomically, and reporting honestly what
is still wrong afterwards.
"""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import cast

import pandas as pd
import pytest

from backend.daily_data_loader import DailyDataLoader
from backend.data_quality.cache_repair import (
    REPAIR_SIDECAR_SUFFIX,
    repair_symbol,
    repair_universe,
)
from backend.dhan_client import DhanDataClient

TODAY = date(2026, 6, 10)
ROW = {"symbol": "TEST", "security_id": "123", "exchange_segment": "NSE_EQ"}


def _candles(dates: list[str], **overrides: list[float]) -> pd.DataFrame:
    """Build a clean OHLCV frame for the given dates, with optional overrides."""
    count = len(dates)
    frame = pd.DataFrame(
        {
            "timestamp": pd.to_datetime(dates),
            "open": [100.0] * count,
            "high": [105.0] * count,
            "low": [99.0] * count,
            "close": [104.0] * count,
            "volume": [1_000.0] * count,
        }
    )
    for column, values in overrides.items():
        frame[column] = values
    return frame


def _recent_dates(count: int, *, end: date = TODAY) -> list[str]:
    """Consecutive dates ending at ``end`` — close enough to avoid stale/gap noise."""
    return [(end - timedelta(days=offset)).isoformat() for offset in reversed(range(count))]


class RecordingClient:
    """A fake Dhan client that returns a scripted frame and counts its calls."""

    def __init__(self, frame: pd.DataFrame | None = None) -> None:
        self.frame = frame if frame is not None else pd.DataFrame()
        self.calls: list[tuple[object, object]] = []

    def fetch_daily_candles(self, *, from_date, to_date, **_kwargs) -> pd.DataFrame:
        self.calls.append((from_date, to_date))
        return self.frame.copy(deep=True)


def _loader(tmp_path: Path, client: object | None = None) -> DailyDataLoader:
    # The loader only ever calls ``fetch_daily_candles`` on its client, so the
    # duck-typed fakes above stand in for a real DhanDataClient.
    return DailyDataLoader(
        cast(DhanDataClient | None, client), cache_dir=tmp_path, request_delay_seconds=0.0
    )


def _write_cache(loader: DailyDataLoader, frame: pd.DataFrame) -> Path:
    path = loader.cache_path(ROW["symbol"], ROW["security_id"])
    frame.to_parquet(path, index=False)
    return path


# ---------------------------------------------------------------------------
# The happy paths
# ---------------------------------------------------------------------------


def test_clean_cache_is_reported_clean_and_never_rewritten(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    path = _write_cache(loader, _candles(_recent_dates(5)))
    before_mtime = path.stat().st_mtime_ns

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "clean"
    assert outcome.refetch_count == 0
    assert path.stat().st_mtime_ns == before_mtime


def test_exact_duplicates_are_repaired_offline_without_touching_dhan(tmp_path: Path):
    """The POONAWALLA case: redundant rows vanish, every trading day survives."""
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    clean = _candles(_recent_dates(5))
    path = _write_cache(loader, pd.concat([clean, clean.iloc[[2]]], ignore_index=True))

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "repaired"
    assert outcome.after_codes == ()
    assert outcome.refetch_count == 0
    assert client.calls == []
    assert outcome.rows_before == 6
    assert outcome.rows_after == 5
    # No trading day may be lost to a dedupe.
    assert outcome.dates_before == outcome.dates_after == 5
    assert len(pd.read_parquet(path).index) == 5


def test_impossible_bar_is_refetched_and_then_dropped_when_still_bad(tmp_path: Path):
    """The TVSHLTD case: Dhan re-serves the same bad bar, so we drop that day."""
    dates = _recent_dates(5)
    dirty = _candles(dates)
    dirty.loc[2, "low"] = dirty.loc[2, "high"]  # open now sits below the low
    # The vendor hands back exactly the same impossible bar.
    client = RecordingClient(dirty.iloc[[2]].copy())
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, dirty)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "repaired"
    assert outcome.after_codes == ()
    assert outcome.refetch_count == 1
    assert "DROP_IMPOSSIBLE_BARS" in outcome.actions
    remaining = pd.read_parquet(path)
    assert len(remaining.index) == 4
    assert date.fromisoformat(dates[2]) not in set(
        pd.to_datetime(remaining["timestamp"]).dt.date
    )


def test_conflicting_duplicates_are_replaced_by_a_full_refetch(tmp_path: Path):
    """The MOTHERSON case: two price series merged, so re-download the lot."""
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    client = RecordingClient(clean)
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, pd.concat([clean, conflicting], ignore_index=True))

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "repaired"
    assert "REFETCH_FULL" in outcome.actions
    assert outcome.refetch_count == 1
    assert len(pd.read_parquet(path).index) == 5


def test_window_refetch_keeps_history_outside_the_window(tmp_path: Path):
    """A bounded probe must never truncate the file to just that window."""
    dates = _recent_dates(10)
    dirty = _candles(dates)
    dirty.loc[8, "low"] = dirty.loc[8, "high"]
    fixed = _candles([dates[8]])
    client = RecordingClient(fixed)
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, dirty)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "repaired"
    stored = pd.read_parquet(path)
    assert len(stored.index) == 10  # nothing outside the window was lost
    assert "DROP_IMPOSSIBLE_BARS" not in outcome.actions


# ---------------------------------------------------------------------------
# Honesty about what could not be fixed
# ---------------------------------------------------------------------------


def test_residual_findings_are_reported_not_hidden(tmp_path: Path):
    """A refetch that does not clean the file must not be reported as success."""
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    # Dhan hands back the very same conflicting series.
    client = RecordingClient(dirty)
    loader = _loader(tmp_path, client)
    _write_cache(loader, dirty)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "unrepairable"
    assert "DUPLICATE_DATE" in outcome.after_codes


def test_price_gap_alone_is_accepted_as_vendor_data(tmp_path: Path):
    """A 50%+ overnight move is never rewritten, and never called a failure."""
    frame = _candles(_recent_dates(5))
    frame.loc[4, ["open", "high", "low", "close"]] = [10.0, 12.0, 9.0, 11.0]
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, frame)
    before = pd.read_parquet(path)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "vendor_data"
    assert client.calls == []
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)


def test_a_repair_that_would_gut_the_history_is_refused(tmp_path: Path):
    """Better a known-dirty file an operator can inspect than a hollowed-out one."""
    dates = _recent_dates(10)
    frame = _candles(dates)
    # Half the bars are impossible (low above high); no honest repair drops five
    # of ten trading days, so the whole attempt must be refused.
    frame.loc[: 4, "low"] = [106.0] * 5
    client = RecordingClient(frame)
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, frame)
    before = pd.read_parquet(path)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "unrepairable"
    assert "drop budget" in (outcome.message or "")
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)


def test_missing_cache_file_is_skipped_rather_than_invented(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "skipped"


def test_fetch_failure_is_captured_and_redacted(tmp_path: Path):
    class ExplodingClient:
        def fetch_daily_candles(self, **_kwargs):
            raise RuntimeError("token=abcd1234 refused")

    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    loader = _loader(tmp_path, ExplodingClient())
    path = _write_cache(loader, pd.concat([clean, conflicting], ignore_index=True))
    before = pd.read_parquet(path)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status in {"failed", "unrepairable"}
    assert "abcd1234" not in (outcome.message or "")
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)


# ---------------------------------------------------------------------------
# Idempotence and quota discipline
# ---------------------------------------------------------------------------


def test_second_run_is_a_no_op(tmp_path: Path):
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    clean = _candles(_recent_dates(5))
    _write_cache(loader, pd.concat([clean, clean.iloc[[2]]], ignore_index=True))

    assert repair_symbol(loader, ROW, today=TODAY).status == "repaired"
    second = repair_symbol(loader, ROW, today=TODAY)

    assert second.status == "clean"
    assert second.refetch_count == 0
    assert client.calls == []


def test_a_stubbornly_dirty_symbol_is_not_refetched_every_launch(tmp_path: Path):
    """The sidecar stops a permanently vendor-dirty symbol burning quota daily."""
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    client = RecordingClient(dirty)
    loader = _loader(tmp_path, client)
    _write_cache(loader, dirty)

    first = repair_symbol(loader, ROW, today=TODAY)
    second = repair_symbol(loader, ROW, today=TODAY)

    assert first.status == "unrepairable"
    assert first.refetch_count == 1
    assert second.status == "skipped"
    assert second.refetch_count == 0
    assert len(client.calls) == 1


def test_the_sidecar_expires_so_vendor_fixes_are_eventually_picked_up(tmp_path: Path):
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    client = RecordingClient(dirty)
    loader = _loader(tmp_path, client)
    _write_cache(loader, dirty)
    repair_symbol(loader, ROW, today=TODAY)

    later = repair_symbol(loader, ROW, today=TODAY + timedelta(days=30))

    assert later.status == "unrepairable"
    assert len(client.calls) == 2


def test_force_ignores_the_sidecar(tmp_path: Path):
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    client = RecordingClient(dirty)
    loader = _loader(tmp_path, client)
    _write_cache(loader, dirty)
    repair_symbol(loader, ROW, today=TODAY)

    forced = repair_symbol(loader, ROW, today=TODAY, force=True)

    assert forced.status == "unrepairable"
    assert len(client.calls) == 2


def test_sidecar_records_only_codes_never_prices(tmp_path: Path):
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    loader = _loader(tmp_path, RecordingClient(dirty))
    _write_cache(loader, dirty)

    repair_symbol(loader, ROW, today=TODAY)

    sidecar = loader.cache_path(ROW["symbol"], ROW["security_id"]).with_suffix(
        REPAIR_SIDECAR_SUFFIX
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["residual_codes"] == ["DUPLICATE_DATE"]
    assert set(payload) == {"attempted_on", "residual_codes"}


# ---------------------------------------------------------------------------
# Safety of the write itself
# ---------------------------------------------------------------------------


def test_dry_run_reports_the_repair_without_writing_it(tmp_path: Path):
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    clean = _candles(_recent_dates(5))
    path = _write_cache(loader, pd.concat([clean, clean.iloc[[2]]], ignore_index=True))
    before = pd.read_parquet(path)

    outcome = repair_symbol(loader, ROW, today=TODAY, dry_run=True)

    assert outcome.status == "repaired"
    assert outcome.rows_after == 5
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)


def test_a_failed_write_never_leaves_a_half_written_cache(tmp_path: Path, monkeypatch):
    """An interrupted write must leave the original file byte-for-byte intact."""
    loader = _loader(tmp_path, RecordingClient())
    clean = _candles(_recent_dates(5))
    path = _write_cache(loader, pd.concat([clean, clean.iloc[[2]]], ignore_index=True))
    original = path.read_bytes()

    import backend.data_quality.cache_repair as cache_repair

    def explode(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(cache_repair.os, "replace", explode)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "failed"
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_unreadable_cache_file_is_reported_not_crashed(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    path = loader.cache_path(ROW["symbol"], ROW["security_id"])
    path.write_bytes(b"this is not a parquet file")

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "failed"


# ---------------------------------------------------------------------------
# Universe-level orchestration
# ---------------------------------------------------------------------------


def test_repair_universe_summarises_every_symbol(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    clean = _candles(_recent_dates(5))
    for index in range(3):
        row = {"symbol": f"SYM{index}", "security_id": str(index)}
        frame = clean if index == 0 else pd.concat([clean, clean.iloc[[1]]], ignore_index=True)
        frame.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
    rows = [{"symbol": f"SYM{index}", "security_id": str(index)} for index in range(3)]

    summary = repair_universe(loader, rows, today=TODAY)

    assert summary.symbols_checked == 3
    assert summary.symbols_clean == 1
    assert summary.symbols_repaired == 2
    assert summary.rows_removed == 2


def test_stale_symbols_are_skipped_when_the_top_up_already_succeeded(tmp_path: Path):
    """The stale guard: do not re-ask Dhan for a tail the prefetch just fetched."""
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    row = {"symbol": "SYM", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)

    summary = repair_universe(
        loader, [row], today=TODAY, prefetch_statuses={"SYM": "fresh"}
    )

    assert client.calls == []
    assert summary.refetch_count == 0


def test_stale_symbols_are_retried_when_the_top_up_failed(tmp_path: Path):
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    row = {"symbol": "SYM", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)

    repair_universe(loader, [row], today=TODAY, prefetch_statuses={"SYM": "failed"})

    assert len(client.calls) == 1


def test_one_broken_symbol_does_not_stop_the_others(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    good = {"symbol": "GOOD", "security_id": "1"}
    bad = {"symbol": "BAD", "security_id": "2"}
    clean = _candles(_recent_dates(5))
    pd.concat([clean, clean.iloc[[1]]], ignore_index=True).to_parquet(
        loader.cache_path(good["symbol"], good["security_id"]), index=False
    )
    loader.cache_path(bad["symbol"], bad["security_id"]).write_bytes(b"junk")

    summary = repair_universe(loader, [bad, good], today=TODAY)

    assert summary.symbols_failed == 1
    assert summary.symbols_repaired == 1


def test_summary_receipt_is_capped_and_json_safe(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    clean = _candles(_recent_dates(5))
    rows = []
    for index in range(60):
        row = {"symbol": f"S{index}", "security_id": str(index)}
        pd.concat([clean, clean.iloc[[1]]], ignore_index=True).to_parquet(
            loader.cache_path(row["symbol"], row["security_id"]), index=False
        )
        rows.append(row)

    summary = repair_universe(loader, rows, today=TODAY)
    receipt = summary.as_receipt()

    assert summary.symbols_repaired == 60
    assert receipt["schema_version"] == 1
    assert receipt["outcomes_truncated"] is True
    assert len(receipt["outcomes"]) < 60
    json.dumps(receipt)  # must be serialisable for the JSON column


def test_progress_callback_failure_never_breaks_the_repair(tmp_path: Path):
    loader = _loader(tmp_path, RecordingClient())
    row = {"symbol": "SYM", "security_id": "1"}
    _candles(_recent_dates(5)).to_parquet(
        loader.cache_path(row["symbol"], row["security_id"]), index=False
    )

    def boom(*_args):
        raise RuntimeError("ui exploded")

    summary = repair_universe(loader, [row], today=TODAY, progress_callback=boom)

    assert summary.symbols_checked == 1


def test_repair_universe_without_a_client_still_repairs_offline(tmp_path: Path):
    """Cache-only mode: dedupe still works, refetch-needing symbols are honest."""
    loader = _loader(tmp_path, None)
    clean = _candles(_recent_dates(5))
    offline = {"symbol": "OFFLINE", "security_id": "1"}
    pd.concat([clean, clean.iloc[[1]]], ignore_index=True).to_parquet(
        loader.cache_path(offline["symbol"], offline["security_id"]), index=False
    )

    summary = repair_universe(loader, [offline], today=TODAY)

    assert summary.symbols_repaired == 1
    assert summary.refetch_count == 0


def test_cache_only_mode_skips_symbols_that_need_the_vendor(tmp_path: Path):
    """No credentials must not produce a wall of identical fetch errors."""
    loader = _loader(tmp_path, None)
    row = {"symbol": "STALE", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)

    outcome = repair_symbol(loader, row, today=TODAY)

    assert outcome.status == "skipped"
    assert outcome.message == "repair needs Dhan credentials"


def test_cache_only_mode_still_does_the_offline_half(tmp_path: Path):
    """A symbol needing both a dedupe and a refetch still gets its dedupe."""
    loader = _loader(tmp_path, None)
    row = {"symbol": "MIXED", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    pd.concat([stale, stale.iloc[[1]]], ignore_index=True).to_parquet(
        loader.cache_path(row["symbol"], row["security_id"]), index=False
    )

    outcome = repair_symbol(loader, row, today=TODAY)

    assert outcome.status == "partially_repaired"
    assert "DEDUPE_EXACT_ROWS" in outcome.actions
    assert outcome.rows_after == 5
    # And it says why the rest could not be done.
    assert "credentials" in (outcome.message or "")


def test_standalone_run_honours_the_loader_checked_marker(tmp_path: Path):
    """No prefetch statuses: fall back to the `.checked` sidecar for staleness."""
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    row = {"symbol": "SYM", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
    # A top-up already asked Dhan for this tail today and got nothing back.
    loader.checked_path(row["symbol"], row["security_id"]).write_text(
        TODAY.isoformat(), encoding="utf-8"
    )

    summary = repair_universe(loader, [row], today=TODAY)

    assert client.calls == []
    assert summary.refetch_count == 0


def test_an_outdated_checked_marker_still_allows_a_stale_refetch(tmp_path: Path):
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    row = {"symbol": "SYM", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
    loader.checked_path(row["symbol"], row["security_id"]).write_text(
        (TODAY - timedelta(days=20)).isoformat(), encoding="utf-8"
    )

    repair_universe(loader, [row], today=TODAY)

    assert len(client.calls) == 1


def test_a_dead_credential_stops_the_pass_hammering_dhan(tmp_path: Path):
    """An expired token fails for every symbol; stop asking after a few tries."""

    class DeadClient:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_daily_candles(self, **_kwargs):
            self.calls += 1
            raise RuntimeError("DH-901 Invalid_Authentication")

    client = DeadClient()
    loader = _loader(tmp_path, client)
    rows = []
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    for index in range(30):
        row = {"symbol": f"S{index}", "security_id": str(index)}
        stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
        rows.append(row)

    summary = repair_universe(loader, rows, today=TODAY)

    # Five failures are enough evidence; the other 25 symbols cost nothing.
    assert client.calls == 5
    assert summary.symbols_checked == 30


def test_a_recovered_fetch_resets_the_breaker(tmp_path: Path):
    """One flaky symbol must not disable refetching for the whole universe."""

    class FlakyClient:
        def __init__(self) -> None:
            self.calls = 0

        def fetch_daily_candles(self, **_kwargs):
            self.calls += 1
            if self.calls % 2:
                raise RuntimeError("transient")
            return _candles(_recent_dates(3))

    client = FlakyClient()
    loader = _loader(tmp_path, client)
    rows = []
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    for index in range(12):
        row = {"symbol": f"S{index}", "security_id": str(index)}
        stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
        rows.append(row)

    repair_universe(loader, rows, today=TODAY)

    # Alternating success/failure never reaches five consecutive failures.
    assert client.calls == 12


def test_an_unreadable_checked_marker_does_not_block_a_repair(tmp_path: Path):
    """A corrupt marker may cost an extra request; it must never skip a repair."""
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    row = {"symbol": "SYM", "security_id": "1"}
    stale = _candles(_recent_dates(5, end=TODAY - timedelta(days=40)))
    stale.to_parquet(loader.cache_path(row["symbol"], row["security_id"]), index=False)
    loader.checked_path(row["symbol"], row["security_id"]).write_text(
        "not-a-date", encoding="utf-8"
    )

    repair_universe(loader, [row], today=TODAY)

    assert len(client.calls) == 1


@pytest.mark.parametrize("suffix", [REPAIR_SIDECAR_SUFFIX])
def test_sidecar_suffix_does_not_collide_with_the_existing_checked_marker(suffix: str):
    """`.checked` is the prefetch's marker; the repair must use its own name."""
    assert suffix != ".checked"
    assert suffix.startswith(".")


def test_repair_uses_a_datetime_free_sidecar_timestamp(tmp_path: Path):
    """The sidecar stores a plain ISO date so it stays readable and comparable."""
    clean = _candles(_recent_dates(5))
    conflicting = clean.iloc[[2]].copy()
    conflicting["close"] = 103.0
    dirty = pd.concat([clean, conflicting], ignore_index=True)
    loader = _loader(tmp_path, RecordingClient(dirty))
    _write_cache(loader, dirty)

    repair_symbol(loader, ROW, today=TODAY)

    sidecar = loader.cache_path(ROW["symbol"], ROW["security_id"]).with_suffix(
        REPAIR_SIDECAR_SUFFIX
    )
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert date.fromisoformat(payload["attempted_on"]) == TODAY
    assert not isinstance(payload["attempted_on"], datetime)


# ---------------------------------------------------------------------------
# Codex review follow-ups (PR #110)
# ---------------------------------------------------------------------------


def test_filling_one_of_several_gaps_counts_as_an_improvement(tmp_path: Path):
    """`validate_candles` reports all calendar gaps as ONE finding.

    The gap count lives in ``affected_rows``, so a repair that fills one of three
    holes leaves the finding *count* unchanged. Judging improvement on the number
    of finding objects alone would reject that repair and throw away candles Dhan
    just supplied.
    """
    # Three ~30-day holes, all inside a frame that is otherwise current.
    dates = ["2026-01-05", "2026-02-10", "2026-03-16", "2026-04-20", *_recent_dates(4)]
    dirty = _candles(dates)
    # The vendor can supply the bars bridging the first hole only.
    bridge = _candles(
        [
            (date(2026, 1, 5) + timedelta(days=offset)).isoformat()
            for offset in range(1, 36, 5)
        ]
    )
    client = RecordingClient(bridge)
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, dirty)
    rows_before = len(pd.read_parquet(path).index)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status == "partially_repaired"
    # The recovered candles must actually be persisted, not discarded.
    assert len(pd.read_parquet(path).index) > rows_before


def test_improvement_still_rejects_a_repair_that_changes_nothing(tmp_path: Path):
    """The looser rule must not start accepting no-op repairs."""
    dates = ["2026-01-05", "2026-02-10", *_recent_dates(3)]
    dirty = _candles(dates)
    # Dhan has nothing for the hole, so the gap count cannot go down.
    client = RecordingClient()
    loader = _loader(tmp_path, client)
    path = _write_cache(loader, dirty)
    before = pd.read_parquet(path)

    outcome = repair_symbol(loader, ROW, today=TODAY)

    assert outcome.status in {"unrepairable", "vendor_data"}
    pd.testing.assert_frame_equal(pd.read_parquet(path), before)
