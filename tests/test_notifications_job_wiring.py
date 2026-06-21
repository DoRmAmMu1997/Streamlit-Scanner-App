"""ALERT-001 wiring: the daily job notifies on completion, non-fatally."""

from __future__ import annotations

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
