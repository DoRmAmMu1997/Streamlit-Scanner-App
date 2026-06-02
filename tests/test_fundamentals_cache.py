"""Tests for the on-disk JSON cache used by the Check Fundamentals agent."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from backend.fundamentals.fundamentals_cache import FundamentalsCache


def _sample_payload() -> dict:
    return {
        "symbol": "DEMO",
        "company_name": "Demo Industries",
        "latest_net_profit": 250.0,
        # `fetched_at` is set by FundamentalsCache.set_data if missing, so the
        # tests below sometimes omit it to confirm that auto-stamp behavior.
    }


def test_data_cache_round_trip(tmp_path: Path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("demo", _sample_payload())

    cached = cache.get_data("demo")
    assert cached is not None
    assert cached["symbol"] == "DEMO"
    # The cache must have stamped a `fetched_at` ISO string.
    assert "fetched_at" in cached


def test_data_cache_expires_past_ttl(tmp_path: Path):
    # TTL = 1 day. Write a payload with a fetched_at 2 days in the past.
    cache = FundamentalsCache(cache_dir=tmp_path, data_ttl_days=1)
    stale = _sample_payload()
    stale["fetched_at"] = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    cache.set_data("demo", stale)

    # Manually overwrite the file with the stale timestamp (set_data
    # otherwise refuses to overwrite a user-supplied fetched_at because
    # the helper only fills it when missing).
    cache.data_path("demo").write_text(json.dumps(stale), encoding="utf-8")

    assert cache.get_data("demo") is None


def test_data_cache_missing_symbol_returns_none(tmp_path: Path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    assert cache.get_data("not_cached_yet") is None


def test_data_cache_handles_corrupt_json(tmp_path: Path):
    # A truncated or corrupt cache file must not crash the loader; it should
    # be treated as a cache miss so the caller refetches cleanly.
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.data_path("demo").write_text("not valid json", encoding="utf-8")

    assert cache.get_data("demo") is None


def test_verdict_cache_keyed_by_model_and_data_date(tmp_path: Path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    verdict = {"symbol": "DEMO", "rating": 8}

    cache.set_verdict("demo", "model-a", "2026-05-27", verdict)

    # Same key → hit.
    assert cache.get_verdict("demo", "model-a", "2026-05-27") == verdict
    # Different model → miss.
    assert cache.get_verdict("demo", "model-b", "2026-05-27") is None
    # Different data date → miss.
    assert cache.get_verdict("demo", "model-a", "2026-05-28") is None


def test_invalidate_removes_all_files_for_a_symbol(tmp_path: Path):
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("demo", _sample_payload())
    cache.set_verdict("demo", "model-a", "2026-05-27", {"rating": 8})
    cache.set_verdict("demo", "model-b", "2026-05-27", {"rating": 7})

    removed = cache.invalidate("demo")

    assert removed >= 3
    assert cache.get_data("demo") is None
    assert cache.get_verdict("demo", "model-a", "2026-05-27") is None
    assert cache.get_verdict("demo", "model-b", "2026-05-27") is None


def test_symbol_normalization_resists_dangerous_filenames(tmp_path: Path):
    # Hostile path characters in a symbol must not escape the cache dir.
    cache = FundamentalsCache(cache_dir=tmp_path)
    cache.set_data("../escape\\attempt", {"symbol": "x"})
    # The file should end up INSIDE tmp_path, not in tmp_path's parent.
    matches = list(tmp_path.glob("*_data.json"))
    assert matches, "Sanitized cache file was not written into the cache dir"
    for path in matches:
        assert path.parent == tmp_path
