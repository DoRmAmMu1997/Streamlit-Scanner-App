"""Tests for the OBS-004 universe mapping-health check.

These use the shared in-memory ``db_session`` fixture and tiny hand-written
universe CSVs in ``tmp_path``, so nothing touches Dhan, the network, or the
developer's real database.

The behaviour under test is a *comparison against a stored baseline*, so most of
these tests run the check twice and assert on what changed between the two runs.
"""

from __future__ import annotations

import logging
import sys

import pandas as pd
import pytest

from backend.data_quality.universe_health import (
    MAX_REPORTED_SYMBOLS,
    MappingRegression,
    UniverseHealth,
    check_universe_health,
    collect_universe_health,
    detect_mapping_regressions,
    log_universe_health,
)
from backend.storage import repository


def _write_universe(directory, universe_key, rows):
    """Write a minimal universe CSV of ``(symbol, mapping_status)`` pairs."""
    frame = pd.DataFrame(
        [
            {
                "universe": universe_key,
                "symbol": symbol,
                "security_id": "1234" if status == "mapped" else "",
                "mapping_status": status,
            }
            for symbol, status in rows
        ]
    )
    frame.to_csv(directory / f"{universe_key}.csv", index=False)


@pytest.fixture
def universe_dir(tmp_path, monkeypatch):
    """A universe directory holding exactly one registered universe key."""
    from backend import universe_builder

    # Restrict the registry so the test does not depend on how many universes the
    # real UNIVERSE_CONFIG happens to hold today.
    monkeypatch.setattr(
        universe_builder,
        "UNIVERSE_CONFIG",
        {"nifty_100": {"file_name": "nifty_100.csv", "display_name": "NIFTY 100"}},
    )
    return tmp_path


def test_collect_reports_counts_and_names_the_unmapped_symbols(universe_dir):
    _write_universe(
        universe_dir,
        "nifty_100",
        [("RELIANCE", "mapped"), ("TCS", "mapped"), ("GUJGASLTD", "missing_security_id")],
    )

    (health,) = collect_universe_health(universe_dir)

    assert health.universe_key == "nifty_100"
    assert health.total_rows == 3
    assert health.mapped_rows == 2
    assert health.unmapped_rows == 1
    # A count alone makes a useless alert; the names are what an operator acts on.
    assert health.unmapped_symbols == ("GUJGASLTD",)


def test_collect_survives_a_missing_universe_file(universe_dir):
    """A health check must never be the reason the daily job dies."""
    (health,) = collect_universe_health(universe_dir)

    assert health.total_rows == 0
    assert health.mapped_rows == 0
    assert health.unmapped_symbols == ()


def test_collect_caps_the_reported_symbol_list(universe_dir):
    """A badly broken CSV must not write an unbounded blob or a huge alert."""
    rows = [(f"SYM{index:03d}", "missing_security_id") for index in range(MAX_REPORTED_SYMBOLS + 10)]
    _write_universe(universe_dir, "nifty_100", rows)

    (health,) = collect_universe_health(universe_dir)

    assert health.unmapped_rows == MAX_REPORTED_SYMBOLS + 10
    assert len(health.unmapped_symbols) == MAX_REPORTED_SYMBOLS


def test_no_baseline_never_regresses():
    """The first check has nothing to compare against, so it must stay quiet.

    Treating "absent" as zero would alert on every pre-existing unmapped symbol
    on first run - exactly the noise that makes people mute an alert channel.
    """
    current = [
        UniverseHealth(
            universe_key="nifty_100",
            total_rows=10,
            mapped_rows=7,
            unmapped_symbols=("A", "B", "C"),
        )
    ]

    assert detect_mapping_regressions(current, {}) == ()


def test_steady_state_and_recovery_do_not_regress():
    """Already-known damage stays quiet, and getting better is not an alert."""

    class _Baseline:
        unmapped_rows = 3
        unmapped_symbols_json = {"symbols": ["A", "B", "C"]}

    steady = [
        UniverseHealth(
            universe_key="nifty_100", total_rows=10, mapped_rows=7, unmapped_symbols=("A", "B", "C")
        )
    ]
    recovered = [
        UniverseHealth(
            universe_key="nifty_100", total_rows=10, mapped_rows=9, unmapped_symbols=("A",)
        )
    ]

    assert detect_mapping_regressions(steady, {"nifty_100": _Baseline()}) == ()
    assert detect_mapping_regressions(recovered, {"nifty_100": _Baseline()}) == ()


def test_regression_names_only_the_newly_unmapped_symbols():
    class _Baseline:
        unmapped_rows = 1
        unmapped_symbols_json = {"symbols": ["A"]}

    current = [
        UniverseHealth(
            universe_key="nifty_100", total_rows=10, mapped_rows=8, unmapped_symbols=("A", "GUJGASLTD")
        )
    ]

    (regression,) = detect_mapping_regressions(current, {"nifty_100": _Baseline()})

    assert regression.previous_unmapped == 1
    assert regression.current_unmapped == 2
    # "A" was already known; only the new drop-out is worth naming.
    assert regression.newly_unmapped == ("GUJGASLTD",)


def test_describe_is_a_single_actionable_line():
    regression = MappingRegression(
        universe_key="hemant_good_200",
        previous_unmapped=6,
        current_unmapped=8,
        newly_unmapped=("GUJGASLTD", "JBCHEPHARM"),
    )

    assert regression.describe() == (
        "hemant_good_200: 6 -> 8 unmapped (+2); GUJGASLTD, JBCHEPHARM"
    )


def test_log_universe_health_emits_one_event_per_universe(caplog):
    snapshots = [
        UniverseHealth(
            universe_key="nifty_100", total_rows=10, mapped_rows=8, unmapped_symbols=("A", "B")
        )
    ]

    with caplog.at_level(logging.INFO):
        log_universe_health(snapshots)

    # log_event stashes the key/value detail on the record as `structured_fields`
    # (see backend/observability), which is how the other suites read it back.
    fields = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == "universe_health_checked"
    ]
    assert len(fields) == 1
    assert fields[0] == {
        "universe_key": "nifty_100",
        "rows": 10,
        "mapped": 8,
        "unmapped": 2,
    }


def test_check_records_a_baseline_and_stays_quiet_on_the_first_run(db_session, universe_dir):
    _write_universe(
        universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("GUJGASLTD", "missing_security_id")]
    )

    report = check_universe_health(db_session, universe_dir=universe_dir)

    assert report.regressions == ()
    stored = repository.get_latest_universe_health_snapshots(db_session)
    assert stored["nifty_100"].unmapped_rows == 1
    assert stored["nifty_100"].unmapped_symbols_json == {"symbols": ["GUJGASLTD"]}


def test_check_alerts_exactly_once_when_a_symbol_drops_out(db_session, universe_dir, caplog):
    """The whole point of OBS-004: one alert on the run where it happens."""
    _write_universe(universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("TCS", "mapped")])
    assert check_universe_health(db_session, universe_dir=universe_dir).regressions == ()

    # TCS leaves Dhan's master overnight.
    _write_universe(
        universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("TCS", "missing_security_id")]
    )
    with caplog.at_level(logging.WARNING):
        second = check_universe_health(db_session, universe_dir=universe_dir)

    (regression,) = second.regressions
    assert regression.newly_unmapped == ("TCS",)
    assert any(
        getattr(record, "event", None) == "universe_mapping_regressed"
        for record in caplog.records
    )

    # Third run: nothing further changed, so the alert must NOT repeat. This is
    # the assertion that proves the baseline write actually happened.
    third = check_universe_health(db_session, universe_dir=universe_dir)
    assert third.regressions == ()


def test_check_reads_the_baseline_before_writing_todays_snapshot(db_session, universe_dir):
    """Ordering guard: comparing after the write would make regression impossible."""
    _write_universe(universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("TCS", "mapped")])
    check_universe_health(db_session, universe_dir=universe_dir)
    _write_universe(
        universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("TCS", "missing_security_id")]
    )

    report = check_universe_health(db_session, universe_dir=universe_dir)

    # Two checks, two history rows retained (append-only), and a real regression.
    assert len(report.regressions) == 1
    rows = db_session.query(repository.UniverseHealthSnapshot).all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Wiring: the daily job, the alert, and the Streamlit prefetch.
# ---------------------------------------------------------------------------


def test_daily_job_surfaces_regressions_without_ever_failing(
    session_factory, universe_dir, monkeypatch, capsys
):
    """The job prints and returns warnings, and a broken check stays non-fatal."""
    from backend.jobs import run_daily_scan as job

    _write_universe(universe_dir, "nifty_100", [("RELIANCE", "mapped"), ("TCS", "mapped")])
    monkeypatch.setattr(
        "backend.config.UNIVERSE_DIR", universe_dir, raising=False
    )

    import backend.data_quality.universe_health as health_module

    monkeypatch.setattr(
        health_module,
        "check_universe_health",
        lambda session, **_: health_module.UniverseHealthReport(
            regressions=(
                health_module.MappingRegression(
                    universe_key="nifty_100",
                    previous_unmapped=0,
                    current_unmapped=1,
                    newly_unmapped=("TCS",),
                ),
            )
        ),
    )

    warnings = job._check_universe_health(session_factory, sys.stdout)

    assert warnings == ("nifty_100: 0 -> 1 unmapped (+1); TCS",)
    assert "Universe mapping regressed" in capsys.readouterr().out


def test_daily_job_swallows_a_broken_health_check(monkeypatch):
    """A universe CSV that will not parse must never take the night's scan down."""
    from backend.jobs import run_daily_scan as job

    def _explode():
        raise RuntimeError("database unavailable")

    assert job._check_universe_health(_explode, sys.stdout) == ()


def test_summary_defaults_keep_existing_constructions_working():
    """Every pre-OBS-004 DailyScanSummary(...) call still has to work."""
    from backend.jobs.run_daily_scan import DailyScanSummary

    assert DailyScanSummary(outcomes=[]).universe_warnings == ()


def test_alert_renders_universe_warnings_even_in_summary_only_mode():
    """A shrinking universe is a warning about the scan, not a per-stock result."""
    from backend.notifications.render import render_telegram
    from backend.notifications.report import DailyScanReport

    report = DailyScanReport(
        ok=True,
        screeners=(),
        total_symbols_scanned=100,
        total_shortlisted=3,
        failed_count=0,
        failed_symbols_or_findings=0,
        top_results=(),
        app_url="",
        # ALERT-002 summary-only: the results block is suppressed, but the
        # integrity warning must still reach the operator.
        include_results=False,
        universe_warnings=("hemant_good_200: 6 -> 8 unmapped (+2); GUJGASLTD",),
    )

    text = render_telegram(report)

    assert "Universe warnings:" in text
    assert "hemant_good_200: 6 -> 8 unmapped (+2); GUJGASLTD" in text
    assert "Top results:" not in text


def test_alert_omits_the_warning_block_when_everything_is_healthy():
    from backend.notifications.render import render_telegram
    from backend.notifications.report import DailyScanReport

    report = DailyScanReport(
        ok=True,
        screeners=(),
        total_symbols_scanned=100,
        total_shortlisted=0,
        failed_count=0,
        failed_symbols_or_findings=0,
        top_results=(),
        app_url="",
    )

    assert "Universe warnings:" not in render_telegram(report)


def test_prefetch_logs_health_without_recording_a_baseline(monkeypatch, caplog):
    """The prefetch must not move the baseline, or the evening alert never fires."""
    import app as app_module

    recorded: list[str] = []
    monkeypatch.setattr(
        "backend.data_quality.universe_health.collect_universe_health",
        lambda *_args, **_kwargs: (
            UniverseHealth(
                universe_key="nifty_100",
                total_rows=5,
                mapped_rows=4,
                unmapped_symbols=("TCS",),
            ),
        ),
    )
    monkeypatch.setattr(
        "backend.storage.repository.record_universe_health_snapshots",
        lambda *args, **kwargs: recorded.append("written"),
    )

    with caplog.at_level(logging.INFO):
        app_module._log_universe_health()

    assert recorded == []
    assert any(
        getattr(record, "event", None) == "universe_health_checked"
        for record in caplog.records
    )


def test_prefetch_health_logging_is_best_effort(monkeypatch):
    """A failure here must not stop the prefetch from launching Streamlit."""
    import app as app_module

    def _explode(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(
        "backend.data_quality.universe_health.collect_universe_health", _explode
    )

    app_module._log_universe_health()  # must not raise
