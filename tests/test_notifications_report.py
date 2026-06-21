"""ALERT-001 report builder over seeded scan history."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal

from backend.notifications.config import NotificationSettings
from backend.notifications.report import build_daily_scan_report
from backend.storage import ScanResult, ScanRun, ScanStatus

SETTINGS = NotificationSettings(app_url="https://scanner.example.com")


@dataclass
class FakeOutcome:
    screener_key: str
    universe_key: str | None
    status: object
    run_id: int | None
    row_count: int
    fatal: bool = False
    message: str = ""


@dataclass
class FakeSummary:
    outcomes: list[FakeOutcome]

    @property
    def exit_code(self) -> int:
        return 1 if any(o.fatal for o in self.outcomes) else 0


def _run(symbols_scanned: int) -> ScanRun:
    return ScanRun(
        status=ScanStatus.SUCCESS,
        screener_key="bollinger_band_reversal",
        universe_key="fno",
        started_at=dt.datetime(2026, 6, 21, tzinfo=dt.UTC),
        symbols_scanned=symbols_scanned,
    )


def _seed(session_factory) -> tuple[int, int]:
    with session_factory() as session:
        run_a, run_b = _run(100), _run(20)
        session.add_all([run_a, run_b])
        session.flush()
        a_id, b_id = run_a.id, run_b.id
        session.add_all(
            [
                ScanResult(run_id=a_id, symbol="RELIANCE", rating="BUY", final_score=Decimal("87.50")),
                ScanResult(run_id=a_id, symbol="TCS", rating="BUY", final_score=None),
                ScanResult(run_id=b_id, symbol="INFY", rating="BUY", final_score=Decimal("92.00")),
            ]
        )
    return a_id, b_id


def test_report_totals_and_ranked_order(session_factory) -> None:
    a_id, b_id = _seed(session_factory)
    summary = FakeSummary(
        outcomes=[
            FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, a_id, 2),
            FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, b_id, 1),
        ]
    )

    report = build_daily_scan_report(
        summary, settings=SETTINGS, session_factory=session_factory
    )

    assert report.ok is True
    assert report.total_symbols_scanned == 120  # 100 + 20
    assert report.total_shortlisted == 3
    assert report.failed_count == 0
    # Best score first, unscored row last (portable nulls-last).
    assert [row.symbol for row in report.top_results] == ["INFY", "RELIANCE", "TCS"]
    assert report.top_results[0].final_score == 92.0
    assert report.top_results[-1].final_score is None
    assert report.app_url == "https://scanner.example.com"


def test_top_results_capped_at_ten(session_factory) -> None:
    with session_factory() as session:
        run = _run(50)
        session.add(run)
        session.flush()
        run_id = run.id
        session.add_all(
            [ScanResult(run_id=run_id, symbol=f"SYM{i:02d}", rating="BUY") for i in range(15)]
        )
    summary = FakeSummary(
        outcomes=[FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, run_id, 15)]
    )

    report = build_daily_scan_report(
        summary, settings=SETTINGS, session_factory=session_factory
    )

    assert len(report.top_results) == 10


def test_fatal_only_summary_needs_no_db(session_factory) -> None:
    # A pre-scan failure (no run_id) builds a failure report without any DB read.
    summary = FakeSummary(
        outcomes=[FakeOutcome("<schema>", None, None, None, 0, fatal=True, message="db down")]
    )
    report = build_daily_scan_report(
        summary, settings=SETTINGS, session_factory=session_factory
    )
    assert report.ok is False
    assert report.failed_count == 1
    assert report.top_results == ()
    assert report.total_symbols_scanned is None
