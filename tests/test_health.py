"""Tests for the passive OBS-002 operational health snapshot.

The health collector must be safe to open during an incident. These tests keep
its contract narrow: read local files and scan history, inspect configuration,
and never contact an external provider.
"""

from __future__ import annotations

import datetime as dt
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import backend.health as health
from backend.config.settings import get_settings
from backend.storage.models import ScanStatus


def _settings(tmp_path: Path, **overrides: str):
    """Build settings whose generated data stays inside pytest's temp folder."""
    env = {
        "DATA_DIR": str(tmp_path),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'scanner.db'}",
        **overrides,
    }
    return get_settings(env=env)


@contextmanager
def _fake_session_scope():
    """Yield an inert object so tests can replace repository query behavior."""
    yield object()


def test_collect_admin_health_selects_latest_exact_success_and_failure(
    monkeypatch, tmp_path
):
    """PARTIAL/RUNNING scans must not masquerade as success or failure."""
    success = SimpleNamespace(
        id=11,
        started_at=dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 10, 9, 5, tzinfo=dt.UTC),
        status=ScanStatus.SUCCESS,
        screener_key="momentum",
        universe_key="nifty_500",
        symbols_scanned=500,
        triggered_by="job:daily",
        error_message=None,
    )
    failure = SimpleNamespace(
        id=12,
        started_at=dt.datetime(2026, 6, 11, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 11, 9, 1, tzinfo=dt.UTC),
        status=ScanStatus.FAILED,
        screener_key="fundamentals",
        universe_key="nifty_100",
        symbols_scanned=100,
        triggered_by="ui:admin@example.com",
        error_message="provider failed",
        data_quality_json=None,
    )
    success.data_quality_json = None
    seen_statuses: list[ScanStatus] = []

    def fake_latest(_session, *, limit, status=None):
        if status is None:
            assert limit == 50
            return []
        assert limit == 1
        seen_statuses.append(status)
        return [success] if status is ScanStatus.SUCCESS else [failure]

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", fake_latest)

    snapshot = health.collect_admin_health(_settings(tmp_path))

    assert seen_statuses == [ScanStatus.SUCCESS, ScanStatus.FAILED]
    assert snapshot.last_successful_scan is not None
    assert snapshot.last_successful_scan.run_id == 11
    assert snapshot.last_failed_scan is not None
    assert snapshot.last_failed_scan.run_id == 12
    assert snapshot.last_failed_scan.error_message == "provider failed"


def test_collect_admin_health_copies_latest_data_quality_receipt(
    monkeypatch, tmp_path
):
    """Health uses the newest persisted quality receipt without revalidating data."""
    stale_run = SimpleNamespace(
        id=20,
        started_at=dt.datetime(2026, 6, 9, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 9, 9, 2, tzinfo=dt.UTC),
        status=ScanStatus.SUCCESS,
        screener_key="older",
        universe_key="nifty_50",
        symbols_scanned=50,
        triggered_by="job:daily",
        error_message=None,
        data_quality_json=None,
    )
    quality_run = SimpleNamespace(
        id=21,
        started_at=dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 10, 9, 2, tzinfo=dt.UTC),
        status=ScanStatus.PARTIAL,
        screener_key="envelope",
        universe_key="nifty_500",
        symbols_scanned=500,
        triggered_by="job:daily",
        error_message="1 symbol(s) had fatal candle data quality findings.",
        data_quality_json={
            "schema_version": 1,
            "checked_symbols": 2,
            "usable_symbols": 1,
            "warning_symbols": 1,
            "fatal_symbols": 1,
            "findings": [
                {
                    "symbol": "WIPRO",
                    "severity": "fatal",
                    "code": "HIGH_BELOW_LOW",
                    "message": "token=quality-secret",
                    "affected_rows": 1,
                    "latest_date": "2026-06-01",
                }
            ],
        },
    )

    def fake_latest(_session, *, limit, status=None):
        if status is ScanStatus.SUCCESS:
            return []
        if status is ScanStatus.FAILED:
            return []
        assert status is None
        assert limit == 50
        return [stale_run, quality_run]

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", fake_latest)

    snapshot = health.collect_admin_health(_settings(tmp_path))

    assert snapshot.latest_data_quality_run is not None
    assert snapshot.latest_data_quality_run.run_id == 21
    assert snapshot.latest_data_quality_run.checked_symbols == 2
    assert snapshot.latest_data_quality_run.usable_symbols == 1
    assert snapshot.latest_data_quality_run.fatal_symbols == 1
    assert snapshot.latest_data_quality_run.findings[0].symbol == "WIPRO"
    assert "quality-secret" not in repr(snapshot.latest_data_quality_run)


def test_collect_admin_health_copies_latest_candle_repair_receipt(monkeypatch, tmp_path):
    """DATA-002: health shows the newest repair pass without re-repairing anything."""
    repair_run = SimpleNamespace(
        id=7,
        started_at=dt.datetime(2026, 8, 17, 6, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 8, 17, 6, 4, tzinfo=dt.UTC),
        trigger="prefetch",
        symbols_checked=577,
        symbols_repaired=14,
        symbols_unrepairable=5,
        rows_removed=1_238,
        refetch_count=5,
        receipt_json={
            "schema_version": 1,
            "total_outcomes": 19,
            "outcomes_truncated": True,
            "outcomes": [
                {
                    "symbol": "POONAWALLA",
                    "status": "repaired",
                    "before_codes": ["DUPLICATE_DATE"],
                    "after_codes": [],
                    "actions": ["DEDUPE_EXACT_ROWS"],
                    "rows_before": 3_714,
                    "rows_after": 2_476,
                    "message": "token=repair-secret",
                },
                "not-a-mapping",
            ],
        },
    )

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_a, **_k: [])
    monkeypatch.setattr(health, "get_latest_candle_repair_run", lambda _session: repair_run)

    snapshot = health.collect_admin_health(_settings(tmp_path))

    run = snapshot.latest_candle_repair_run
    assert run is not None
    assert run.run_id == 7
    assert run.trigger == "prefetch"
    assert run.symbols_unrepairable == 5
    assert run.outcomes_truncated is True
    assert run.total_outcomes == 19
    # The junk entry is skipped rather than trusted.
    assert len(run.outcomes) == 1
    assert run.outcomes[0].rows_removed == 1_238
    assert "repair-secret" not in repr(run)


def test_collect_admin_health_tolerates_a_malformed_repair_receipt(monkeypatch, tmp_path):
    """A receipt written by another app version must not break the page.

    The header counts are real columns, so a pass still shows up even when its
    detail blob cannot be parsed.
    """
    repair_run = SimpleNamespace(
        id=8,
        started_at=dt.datetime(2026, 8, 17, 6, 0, tzinfo=dt.UTC),
        finished_at=None,
        trigger="cli",
        symbols_checked=3,
        symbols_repaired=0,
        symbols_unrepairable=0,
        rows_removed=0,
        refetch_count=0,
        receipt_json="this is not a mapping",
    )

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_a, **_k: [])
    monkeypatch.setattr(health, "get_latest_candle_repair_run", lambda _session: repair_run)

    run = health.collect_admin_health(_settings(tmp_path)).latest_candle_repair_run

    assert run is not None
    assert run.symbols_checked == 3
    assert run.outcomes == ()


def test_collect_admin_health_has_no_repair_run_before_the_first_pass(
    monkeypatch, tmp_path
):
    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_a, **_k: [])
    monkeypatch.setattr(health, "get_latest_candle_repair_run", lambda _session: None)

    snapshot = health.collect_admin_health(_settings(tmp_path))

    assert snapshot.latest_candle_repair_run is None


def test_collect_admin_health_reads_latest_candle_date_from_parquet_metadata(
    monkeypatch, tmp_path
):
    """Normal Parquet caches should not load whole timestamp columns into memory."""
    cache_dir = tmp_path / "cache" / "daily"
    cache_dir.mkdir(parents=True)
    pd.DataFrame(
        {
            "timestamp": pd.to_datetime(["2026-06-09", "2026-06-10"], utc=True),
            "close": [100.0, 101.0],
        }
    ).to_parquet(cache_dir / "DEMO_1.parquet", index=False)

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        health,
        "_latest_timestamp_from_column",
        lambda _path: (_ for _ in ()).throw(
            AssertionError("metadata statistics should satisfy this cache file")
        ),
    )

    snapshot = health.collect_admin_health(_settings(tmp_path))

    assert snapshot.cached_symbol_count == 1
    assert snapshot.latest_candle_date == dt.date(2026, 6, 10)
    assert snapshot.unreadable_cache_file_count == 0


def test_collect_admin_health_handles_missing_and_corrupt_caches(
    monkeypatch, tmp_path
):
    """One bad cache file should degrade the page instead of breaking the page."""
    cache_dir = tmp_path / "cache" / "daily"
    cache_dir.mkdir(parents=True)
    (cache_dir / "BROKEN_1.parquet").write_text("not parquet", encoding="utf-8")

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_args, **_kwargs: [])

    snapshot = health.collect_admin_health(_settings(tmp_path))

    assert snapshot.cached_symbol_count == 1
    assert snapshot.latest_candle_date is None
    assert snapshot.unreadable_cache_file_count == 1
    assert snapshot.last_data_refresh is not None


def test_provider_readiness_is_configuration_only(monkeypatch, tmp_path):
    """Provider readiness must not spend quota or depend on network availability."""
    looked_up: list[str] = []

    def fake_find_spec(module_name):
        looked_up.append(module_name)
        return object()

    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(health.importlib.util, "find_spec", fake_find_spec)

    snapshot = health.collect_admin_health(
        _settings(
            tmp_path,
            DHAN_CLIENT_ID="dhan-client",
            DHAN_ACCESS_TOKEN="dhan-token",
            SERPAPI_API_KEY="serp-secret",
        )
    )
    services = {service.name: service for service in snapshot.services}

    assert looked_up == ["claude_agent_sdk"]
    assert services["Dhan"].status == "ready"
    assert services["Claude Agent SDK"].status == "ready"
    assert services["SerpAPI"].status == "ready"
    assert "dhan-client" not in repr(snapshot)
    assert "dhan-token" not in repr(snapshot)
    assert "serp-secret" not in repr(snapshot)


def test_claude_package_check_failure_exposes_only_exception_type(
    monkeypatch, tmp_path
):
    """A broken local package environment must not leak its exception message."""
    monkeypatch.setattr(health, "session_scope", _fake_session_scope)
    monkeypatch.setattr(health, "get_latest_scan_runs", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        health.importlib.util,
        "find_spec",
        lambda _name: (_ for _ in ()).throw(
            RuntimeError("package path contains secret-token")
        ),
    )

    snapshot = health.collect_admin_health(_settings(tmp_path))
    claude = next(
        service for service in snapshot.services if service.name == "Claude Agent SDK"
    )

    assert claude.status == "unavailable"
    assert claude.detail == "Package check failed (RuntimeError)."
    assert "secret-token" not in repr(snapshot)


def test_database_failure_exposes_only_exception_type(monkeypatch, tmp_path):
    """Database passwords, URLs, and driver messages must never reach the UI."""

    @contextmanager
    def broken_session_scope():
        raise RuntimeError(
            "postgresql://admin:super-secret@db.example/scanner connection refused"
        )
        yield  # pragma: no cover - makes this function a context manager

    monkeypatch.setattr(health, "session_scope", broken_session_scope)

    snapshot = health.collect_admin_health(_settings(tmp_path))
    database = next(service for service in snapshot.services if service.name == "Database")

    assert database.status == "unavailable"
    assert database.detail == "Health query failed (RuntimeError)."
    assert "super-secret" not in repr(snapshot)
    assert "db.example" not in repr(snapshot)
