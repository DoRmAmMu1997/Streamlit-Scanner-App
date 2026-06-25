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
    loader_failures: int = 0
    compute_failures: int = 0
    rejected_result_rows: int = 0
    ai_validation_failures: int = 0
    data_quality_fatal_symbols: int = 0
    data_quality_fatal_findings: int = 0


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
                ScanResult(
                    run_id=a_id,
                    symbol="RELIANCE",
                    rating="BUY",
                    final_score=Decimal("87.50"),
                ),
                ScanResult(run_id=a_id, symbol="TCS", rating="BUY", final_score=None),
                ScanResult(
                    run_id=b_id,
                    symbol="INFY",
                    rating="BUY",
                    final_score=Decimal("92.00"),
                ),
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
    assert report.top_results[0].score == 92.0
    assert report.top_results[0].score_source == "final_score"
    assert report.top_results[-1].score is None
    assert report.top_results[-1].score_source == "unscored"
    assert report.app_url == "https://scanner.example.com"


def test_report_uses_confidence_fallback_for_unscored_top_results(
    session_factory,
) -> None:
    with session_factory() as session:
        run = _run(50)
        session.add(run)
        session.flush()
        run_id = run.id
        session.add_all(
            [
                ScanResult(
                    run_id=run_id,
                    symbol="LOW_FINAL_SCORE",
                    rating="BUY",
                    final_score=Decimal("10.00"),
                    raw_result_json={"confidence": 1},
                ),
                ScanResult(
                    run_id=run_id,
                    symbol="HIGH_CONFIDENCE",
                    rating="BUY",
                    final_score=None,
                    raw_result_json={"confidence": 94.2},
                ),
                ScanResult(
                    run_id=run_id,
                    symbol="LOW_CONFIDENCE",
                    rating="BUY",
                    final_score=None,
                    raw_result_json={"confidence": 61.5},
                ),
                ScanResult(
                    run_id=run_id,
                    symbol="UNSCORED",
                    rating="BUY",
                    final_score=None,
                    raw_result_json={"confidence": "not-a-number"},
                ),
            ]
        )
    summary = FakeSummary(
        outcomes=[
            FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, run_id, 4)
        ]
    )

    report = build_daily_scan_report(
        summary, settings=SETTINGS, session_factory=session_factory
    )

    assert [row.symbol for row in report.top_results] == [
        "LOW_FINAL_SCORE",
        "HIGH_CONFIDENCE",
        "LOW_CONFIDENCE",
        "UNSCORED",
    ]
    assert [(row.score, row.score_source) for row in report.top_results] == [
        (10.0, "final_score"),
        (94.2, "confidence"),
        (61.5, "confidence"),
        (None, "unscored"),
    ]


def test_report_counts_partial_symbol_failures_separately(session_factory) -> None:
    summary = FakeSummary(
        outcomes=[
            FakeOutcome(
                "bollinger_band_reversal",
                "fno",
                ScanStatus.PARTIAL,
                None,
                3,
                loader_failures=1,
                compute_failures=2,
                rejected_result_rows=1,
                ai_validation_failures=1,
                data_quality_fatal_symbols=1,
                data_quality_fatal_findings=2,
                message="partial symbol failures",
            )
        ]
    )

    report = build_daily_scan_report(
        summary, settings=SETTINGS, session_factory=session_factory
    )

    assert report.failed_count == 0
    assert report.failed_symbols_or_findings == 7


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


def test_summary_content_keeps_counts_but_skips_top_results(session_factory) -> None:
    # ALERT-002: the summary-only content level still reads symbol counts but skips
    # the heavier top-N read, so the report carries no per-stock results.
    a_id, b_id = _seed(session_factory)
    summary = FakeSummary(
        outcomes=[
            FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, a_id, 2),
            FakeOutcome("bollinger_band_reversal", "fno", ScanStatus.SUCCESS, b_id, 1),
        ]
    )
    summary_settings = NotificationSettings(
        app_url="https://scanner.example.com", alert_content="summary"
    )

    report = build_daily_scan_report(
        summary, settings=summary_settings, session_factory=session_factory
    )

    assert report.include_results is False
    assert report.total_symbols_scanned == 120
    assert report.total_shortlisted == 3
    assert report.top_results == ()


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
