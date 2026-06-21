"""ALERT-001 wiring: the daily job notifies on completion, non-fatally."""

from __future__ import annotations

from io import StringIO

import pytest

import backend.notifications as notifications
from backend.jobs.run_daily_scan import DailyScanOutcome, DailyScanSummary, main
from backend.storage import ScanStatus


def _ok_summary() -> DailyScanSummary:
    return DailyScanSummary(
        outcomes=[
            DailyScanOutcome(
                screener_key="bollinger_band_reversal",
                universe_key="fno",
                status=ScanStatus.SUCCESS,
                run_id=1,
                row_count=2,
            )
        ]
    )


def test_main_notifies_with_the_summary(monkeypatch) -> None:
    captured: list[DailyScanSummary] = []
    monkeypatch.setattr(notifications, "notify_daily_scan", captured.append)

    exit_code = main(
        ["--screener", "bollinger_band_reversal"],
        job_runner=lambda **_kwargs: _ok_summary(),
        schema_bootstrapper=lambda: True,
    )

    assert exit_code == 0
    assert len(captured) == 1
    assert captured[0].outcomes[0].screener_key == "bollinger_band_reversal"


def test_notification_failure_never_changes_exit_code(monkeypatch) -> None:
    def boom(_summary: DailyScanSummary) -> None:
        raise RuntimeError("notifier exploded")

    monkeypatch.setattr(notifications, "notify_daily_scan", boom)

    exit_code = main(
        ["--screener", "bollinger_band_reversal"],
        job_runner=lambda **_kwargs: _ok_summary(),
        schema_bootstrapper=lambda: True,
    )

    assert exit_code == 0  # notifier failure is swallowed


def test_schema_bootstrap_failure_sends_failure_alert(monkeypatch) -> None:
    captured: list[DailyScanSummary] = []
    monkeypatch.setattr(notifications, "notify_daily_scan", captured.append)

    def should_not_run(**_kwargs: object) -> DailyScanSummary:
        raise AssertionError("job_runner must not run when schema bootstrap fails")

    exit_code = main(
        ["--screener", "bollinger_band_reversal"],
        job_runner=should_not_run,
        schema_bootstrapper=lambda: False,
        output=StringIO(),
    )

    assert exit_code == 1
    assert len(captured) == 1
    outcome = captured[0].outcomes[0]
    assert outcome.screener_key == "<schema>"
    assert outcome.fatal is True
    assert "schema is unavailable" in outcome.message


def test_config_load_failure_sends_failure_alert(monkeypatch, tmp_path) -> None:
    captured: list[DailyScanSummary] = []
    monkeypatch.setattr(notifications, "notify_daily_scan", captured.append)
    bad_config = tmp_path / "daily_scans.yaml"
    bad_config.write_text("scans:\n  - [broken\n", encoding="utf-8")

    def should_not_run(**_kwargs: object) -> DailyScanSummary:
        raise AssertionError("job_runner must not run when config loading fails")

    exit_code = main(
        ["--config", str(bad_config)],
        job_runner=should_not_run,
        schema_bootstrapper=lambda: True,
        output=StringIO(),
    )

    assert exit_code == 1
    assert len(captured) == 1
    outcome = captured[0].outcomes[0]
    assert outcome.screener_key == "<config>"
    assert outcome.fatal is True
    assert "Could not load config" in outcome.message


def test_unexpected_job_crash_sends_failure_alert(monkeypatch) -> None:
    captured: list[DailyScanSummary] = []
    monkeypatch.setattr(notifications, "notify_daily_scan", captured.append)

    def crash(**_kwargs: object) -> DailyScanSummary:
        raise RuntimeError("daily runner exploded")

    with pytest.raises(RuntimeError, match="daily runner exploded"):
        main(
            ["--screener", "bollinger_band_reversal"],
            job_runner=crash,
            schema_bootstrapper=lambda: True,
            output=StringIO(),
        )

    assert len(captured) == 1
    outcome = captured[0].outcomes[0]
    assert outcome.screener_key == "<job>"
    assert outcome.fatal is True
    assert "Daily scan crashed" in outcome.message
