"""Tests for JOB-001's headless daily scan command.

What this test file proves
--------------------------
JOB-001 should let the scanner run without Streamlit. These tests replace every
external dependency with a tiny fake:

- no Dhan client or broker network calls;
- no Streamlit browser/session state;
- no LLM or Screener.in calls;
- no writes to the developer's real ``data/scanner.db``.

The only real cross-layer path exercised here is the important one:
``backend.jobs.run_daily_scan`` calls the SCAN-003 ``run_scan`` service, which
then writes ``scan_runs`` and ``scan_results`` through the repository layer.

Beginner note:
Most tests in this file pass injected functions into ``run_daily_scan``. That is
not "mocking for its own sake"; it is how we keep the test offline while still
exercising the real job orchestration and, in the persistence test, the real
SCAN-003 service/database path.
"""

from __future__ import annotations

import logging
from datetime import date
from typing import Any

import pandas as pd
import pytest
from sqlalchemy.orm import Session

from backend.observability import (
    EVENT_DAILY_JOB_COMPLETED,
    EVENT_DAILY_JOB_CONFIG_INVALID,
    EVENT_DAILY_JOB_CONFIG_LOADED,
    EVENT_DAILY_JOB_STARTED,
)
from backend.scanning import ScanRunResult, ScanStatus
from backend.screener_registry import ScreenerDefinition
from backend.storage.repository import get_latest_scan_runs, get_scan_results


def _event_fields(caplog, event_name: str) -> list[dict]:
    """Return structured fields for captured events with ``event_name``."""
    return [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == event_name
    ]


# The file-backed ``file_db_engine`` / ``file_session_factory`` fixtures these
# tests use live in tests/conftest.py, so a normal pytest run cannot pollute
# local scan history.


class _FakeLoader:
    """Small data-loader stand-in exposing the fields SCAN-003 reads.

    The real ``DailyDataLoader`` has many methods for Dhan/cache work. JOB-001
    tests do not need any of that because fake screeners return rows directly.
    They only need the status fields that ``run_scan`` checks to decide whether
    a run is SUCCESS or PARTIAL.
    """

    def __init__(self, last_failures: list[dict[str, object]] | None = None) -> None:
        self.last_failures = list(last_failures or [])
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0


def _definition(
    key: str,
    universe: str,
    run,
    default_params: dict[str, Any] | None = None,
) -> ScreenerDefinition:
    """Build registry definitions without importing real screener modules.

    ``ScreenerDefinition`` is the exact shape returned by the production
    registry. Building it here lets the job runner exercise its real registry
    contract while avoiding imports of AI/network-capable screeners in tests.
    """
    return ScreenerDefinition(
        key=key,
        name=key.replace("_", " ").title(),
        description=f"Fake definition for {key}",
        universe=universe,
        timeframe="daily",
        lookback_days=10,
        default_params=dict(default_params or {"threshold": 7}),
        module_name=f"tests.fake_{key}",
        run=run,
    )


def _fake_universe(symbol_prefix: str) -> pd.DataFrame:
    """Create the tiny universe table passed into fake screeners.

    The columns mirror real universe CSVs closely enough that a future change in
    job code cannot accidentally rely on a Streamlit-only shape.
    """
    return pd.DataFrame(
        {
            "symbol": [f"{symbol_prefix}1", f"{symbol_prefix}2"],
            "security_id": ["1", "2"],
            "exchange_segment": ["NSE_EQ", "NSE_EQ"],
            "instrument_type": ["EQUITY", "EQUITY"],
            "mapping_status": ["mapped", "mapped"],
        }
    )


def _row_for(symbol: str) -> dict[str, object]:
    """Return one deterministic shortlisted row for persistence checks.

    The keys follow ``BaseScanner``'s common result contract. That means the real
    repository mapper can store typed columns (symbol/rating/date/close) and the
    full raw JSON row just like it does for production screeners.
    """
    return {
        "symbol": symbol,
        "rating": "BUY",
        "signal_date": date(2026, 6, 5),
        "close": 123.45,
        "reason": "fake daily job signal",
        "provenance": {
            "triggered_rules": ["fake_daily_job_signal"],
            "indicator_values": {"close": 123.45},
            "source": "deterministic",
        },
    }


def test_default_screener_keys_are_the_deterministic_daily_set():
    """The scheduled default should avoid AI/network-only screeners."""
    from backend.jobs.run_daily_scan import DEFAULT_DAILY_SCAN_KEYS

    assert DEFAULT_DAILY_SCAN_KEYS == (
        "bollinger_band_reversal",
        "heikin_ashi_supertrend",
        "envelope_knoxville_buy",
    )


def test_run_daily_scan_uses_registry_universes_and_persists_history(
    file_db_engine,
    file_session_factory,
):
    """Run all defaults through the real service and query saved history.

    This is the most integration-like JOB-001 test. The fake registry/universe
    keep it offline, but ``run_daily_scan`` still delegates to the actual
    SCAN-003 ``run_scan`` service and the real storage repository.
    """
    from backend.jobs.run_daily_scan import DEFAULT_DAILY_SCAN_KEYS, run_daily_scan

    loaded_universes: list[str] = []
    seen_params: dict[str, dict[str, object]] = {}

    def make_run(key: str):
        def run(universe_df, _data_loader, params):
            # Capture params inside the fake screener, not before the call. This
            # proves the job runner copied defaults and added the 10-year date
            # window before handing control to the screener.
            seen_params[key] = dict(params)
            return pd.DataFrame([_row_for(str(universe_df.iloc[0]["symbol"]))])

        return run

    registry = {
        "bollinger_band_reversal": _definition(
            "bollinger_band_reversal", "fno", make_run("bollinger_band_reversal")
        ),
        "heikin_ashi_supertrend": _definition(
            "heikin_ashi_supertrend", "fno", make_run("heikin_ashi_supertrend")
        ),
        "envelope_knoxville_buy": _definition(
            "envelope_knoxville_buy",
            "hemant_super_45",
            make_run("envelope_knoxville_buy"),
        ),
    }

    def load_universe(universe_key: str) -> pd.DataFrame:
        # The job should not hardcode a universe per default. It should read the
        # universe from each ScreenerDefinition, exactly as Streamlit does.
        loaded_universes.append(universe_key)
        return _fake_universe(universe_key.upper())

    summary = run_daily_scan(
        registry_loader=lambda: registry,
        universe_loader=load_universe,
        data_loader_factory=_FakeLoader,
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    assert [outcome.screener_key for outcome in summary.outcomes] == list(
        DEFAULT_DAILY_SCAN_KEYS
    )
    assert loaded_universes == ["fno", "fno", "hemant_super_45"]

    for params in seen_params.values():
        assert params["threshold"] == 7
        assert params["start_date"] == date(2016, 6, 5)
        assert params["end_date"] == date(2026, 6, 5)

    with Session(file_db_engine) as session:
        # Query back through repository helpers instead of raw SQL. That proves
        # the future history page can read the same rows the job created.
        runs = sorted(get_latest_scan_runs(session, limit=10), key=lambda run: run.screener_key)
        assert [run.screener_key for run in runs] == sorted(DEFAULT_DAILY_SCAN_KEYS)
        assert {run.universe_key for run in runs} == {"fno", "hemant_super_45"}
        assert {run.triggered_by for run in runs} == {"job:daily_scan"}
        assert {run.status for run in runs} == {ScanStatus.SUCCESS}

        rows = [row for run in runs for row in get_scan_results(session, run.id)]
        assert len(rows) == 3
        assert {row.rating for row in rows} == {"BUY"}
        assert {row.raw_result_json["reason"] for row in rows} == {
            "fake daily job signal"
        }


def test_partial_scan_is_recorded_and_still_exits_zero(file_session_factory):
    """A scan with symbol-level failures is useful history, not a fatal job.

    Partial means "the scan ran and history captured the problem." That should
    alert operators in history, but it should not make the scheduler think the
    whole daily process crashed.
    """
    from backend.jobs.run_daily_scan import run_daily_scan

    def run(universe_df, _data_loader, _params):
        return pd.DataFrame([_row_for(str(universe_df.iloc[0]["symbol"]))])

    summary = run_daily_scan(
        screener_keys=["partial_screener"],
        registry_loader=lambda: {
            "partial_screener": _definition("partial_screener", "fno", run)
        },
        universe_loader=lambda _key: _fake_universe("PARTIAL"),
        data_loader_factory=lambda: _FakeLoader(
            [{"symbol": "PARTIAL2", "message": "offline fixture failure"}]
        ),
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    assert summary.outcomes[0].status is ScanStatus.PARTIAL
    assert summary.outcomes[0].fatal is False
    assert "failed to load" in summary.outcomes[0].message


def test_failed_screener_is_recorded_and_exits_nonzero(file_session_factory, capsys):
    """A full screener failure should fail the scheduled command safely.

    The fake exception includes an obvious secret marker. The assertion below is
    a regression guard that the CLI prints the exception type, not the raw text.
    """
    from backend.jobs.run_daily_scan import run_daily_scan

    def boom(_universe_df, _data_loader, _params):
        raise RuntimeError("token=LEAKME should not be printed")

    summary = run_daily_scan(
        screener_keys=["broken_screener"],
        registry_loader=lambda: {
            "broken_screener": _definition("broken_screener", "fno", boom)
        },
        universe_loader=lambda _key: _fake_universe("BROKEN"),
        data_loader_factory=_FakeLoader,
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    assert summary.outcomes[0].status is ScanStatus.FAILED
    assert summary.outcomes[0].fatal is True
    output = capsys.readouterr().out
    assert "RuntimeError" in output
    assert "LEAKME" not in output


def test_setup_failure_exits_nonzero_without_printing_raw_exception(capsys):
    """Universe/load setup errors happen before run_scan can persist a row.

    Because no scan header exists yet, the only durable signal is the process
    exit code and the operator summary. That summary must still be secret-safe.
    """
    from backend.jobs.run_daily_scan import run_daily_scan

    summary = run_daily_scan(
        screener_keys=["setup_screener"],
        registry_loader=lambda: {
            "setup_screener": _definition(
                "setup_screener",
                "fno",
                lambda *_args: pd.DataFrame([_row_for("NEVER")]),
            )
        },
        universe_loader=lambda _key: (_ for _ in ()).throw(
            FileNotFoundError("token=LEAKME universe path")
        ),
        data_loader_factory=_FakeLoader,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    assert summary.outcomes[0].fatal is True
    output = capsys.readouterr().out
    assert "FileNotFoundError" in output
    assert "LEAKME" not in output


def test_unknown_screener_exits_nonzero_and_continues_known_scans(file_session_factory):
    """A bad configured key should not prevent later valid keys from running.

    This mirrors how scheduled configs fail in real life: one typo should make
    the job exit non-zero, but it should not waste the valid work that follows.
    """
    from backend.jobs.run_daily_scan import run_daily_scan

    def run(universe_df, _data_loader, _params):
        return pd.DataFrame([_row_for(str(universe_df.iloc[0]["symbol"]))])

    summary = run_daily_scan(
        screener_keys=["missing_screener", "known_screener"],
        registry_loader=lambda: {
            "known_screener": _definition("known_screener", "fno", run)
        },
        universe_loader=lambda _key: _fake_universe("KNOWN"),
        data_loader_factory=_FakeLoader,
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    assert [outcome.screener_key for outcome in summary.outcomes] == [
        "missing_screener",
        "known_screener",
    ]
    assert summary.outcomes[0].fatal is True
    assert summary.outcomes[1].status is ScanStatus.SUCCESS
    assert summary.outcomes[1].run_id is not None


def test_missing_run_id_is_fatal_for_the_scheduled_job(capsys):
    """The UI can be best-effort, but the daily job must know history failed.

    ``run_id=None`` is the signal that SCAN-003 could not create/persist the
    audit header. The in-memory rows might exist, but a daily job without
    persisted history cannot support later comparison or replay tasks.
    """
    from backend.jobs.run_daily_scan import run_daily_scan

    def fake_scan_runner(**_kwargs):
        return ScanRunResult(
            status=ScanStatus.SUCCESS,
            results=pd.DataFrame([_row_for("NOPERSIST")]),
            run_id=None,
        )

    summary = run_daily_scan(
        screener_keys=["no_history"],
        registry_loader=lambda: {
            "no_history": _definition(
                "no_history",
                "fno",
                lambda *_args: pd.DataFrame([_row_for("NOPERSIST")]),
            )
        },
        universe_loader=lambda _key: _fake_universe("NOPERSIST"),
        data_loader_factory=_FakeLoader,
        scan_runner=fake_scan_runner,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    assert summary.outcomes[0].status is ScanStatus.SUCCESS
    assert summary.outcomes[0].run_id is None
    assert "history was not persisted" in capsys.readouterr().out.lower()


def test_main_accepts_repeatable_screener_overrides():
    """``--screener`` can run AI or custom screeners without JOB-002 config.

    ``main`` is tested with a fake job runner so this stays a pure argparse
    check. It proves the CLI surface without discovering real screeners or
    needing Dhan credentials.
    """
    from backend.jobs.run_daily_scan import DailyScanSummary, main

    seen_keys: list[str] = []

    def fake_job_runner(*, screener_keys=None, output=None, **_kwargs):
        seen_keys.extend(screener_keys or [])
        return DailyScanSummary(outcomes=[])

    exit_code = main(
        ["--screener", "technical_analysis", "--screener", "sixty_seven_ka_funda"],
        job_runner=fake_job_runner,
        # A no-op bootstrapper keeps this argparse-focused test from migrating
        # the developer's real DATABASE_URL database.
        schema_bootstrapper=lambda: True,
    )

    assert exit_code == 0
    assert seen_keys == ["technical_analysis", "sixty_seven_ka_funda"]


def test_main_ensures_database_schema_before_running_the_job():
    """The CLI must apply migrations before any scan tries to persist history.

    A fresh checkout has no ``scan_runs`` table until ``alembic upgrade head``
    runs. The command bootstraps the schema itself so the very first scheduled
    run records history instead of exiting 1 with "History was not persisted."
    Both collaborators are injected fakes, so this stays a pure ordering check.
    """
    from backend.jobs.run_daily_scan import DailyScanSummary, main

    calls: list[str] = []

    def fake_schema_bootstrapper() -> bool:
        calls.append("schema")
        return True

    def fake_job_runner(*, screener_keys=None, output=None, **_kwargs):
        calls.append("job")
        return DailyScanSummary(outcomes=[])

    exit_code = main(
        [],
        job_runner=fake_job_runner,
        schema_bootstrapper=fake_schema_bootstrapper,
    )

    assert exit_code == 0
    assert calls == ["schema", "job"]


def test_main_stops_before_running_job_when_schema_bootstrap_fails(capsys):
    """A scheduled scan must not run when its audit tables are unavailable.

    SCAN-004 makes persisted history part of the daily job's observable
    contract. Returning ``False`` simulates an Alembic/bootstrap failure. The
    command should explain that no scan started, exit non-zero, and never call
    the injected job runner.
    """
    from backend.jobs.run_daily_scan import DailyScanSummary, main

    calls: list[str] = []

    def fake_job_runner(**_kwargs):
        calls.append("job")
        return DailyScanSummary(outcomes=[])

    exit_code = main(
        [],
        job_runner=fake_job_runner,
        schema_bootstrapper=lambda: False,
    )

    assert exit_code == 1
    assert calls == []
    output = capsys.readouterr().out.lower()
    assert "schema" in output
    assert "not started" in output


# ---------------------------------------------------------------------------
# JOB-002: config-driven schedule (`--config`)
# ---------------------------------------------------------------------------


def test_config_run_skips_disabled_entries_and_runs_enabled(
    file_session_factory,
    capsys,
):
    """Disabled config entries are reported as skipped and never executed."""
    from backend.jobs.daily_scan_config import DailyScanEntry
    from backend.jobs.run_daily_scan import run_daily_scan

    ran: list[str] = []

    def run(universe_df, _data_loader, _params):
        ran.append(str(universe_df.iloc[0]["symbol"]))
        return pd.DataFrame([_row_for(str(universe_df.iloc[0]["symbol"]))])

    summary = run_daily_scan(
        scan_entries=[
            DailyScanEntry(name="On", screener_key="enabled_one", enabled=True),
            DailyScanEntry(name="Off", screener_key="disabled_one", enabled=False),
        ],
        registry_loader=lambda: {
            "enabled_one": _definition("enabled_one", "fno", run),
            "disabled_one": _definition("disabled_one", "fno", run),
        },
        universe_loader=lambda key: _fake_universe(key.upper()),
        data_loader_factory=_FakeLoader,
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    assert [outcome.screener_key for outcome in summary.outcomes] == ["enabled_one"]
    assert len(ran) == 1  # the disabled screener's run() was never called

    output = capsys.readouterr().out
    assert "SKIPPED" in output
    assert "disabled_one" in output


def test_config_entry_overrides_universe_and_params_reach_the_service(capsys):
    """Config overrides reach both the scan service and operator-facing outcome.

    The resolved universe is more than an internal input: it is also printed in
    scheduled-job output and exposed through ``DailyScanOutcome``. Keeping those
    views aligned prevents an operator from seeing the registry default even
    though the scan actually ran against the configured override.
    """
    from backend.jobs.daily_scan_config import DailyScanEntry
    from backend.jobs.run_daily_scan import run_daily_scan

    captured: dict[str, object] = {}
    loaded_universes: list[str] = []

    def fake_scan_runner(**kwargs):
        captured.update(kwargs)
        return ScanRunResult(
            status=ScanStatus.SUCCESS,
            results=pd.DataFrame([_row_for("X")]),
            run_id=1,
        )

    def load_universe(universe_key: str) -> pd.DataFrame:
        loaded_universes.append(universe_key)
        return _fake_universe(universe_key.upper())

    summary = run_daily_scan(
        scan_entries=[
            DailyScanEntry(
                name="Env override",
                screener_key="envelope_knoxville_buy",
                universe_key="hemant_super_45",
                params={"percent": 9.0},
            )
        ],
        registry_loader=lambda: {
            "envelope_knoxville_buy": _definition(
                "envelope_knoxville_buy",
                "fno",  # registry default; the config overrides it below
                lambda *_args: pd.DataFrame([_row_for("X")]),
                default_params={"percent": 14.0, "ema_period": 200},
            )
        },
        universe_loader=load_universe,
        data_loader_factory=_FakeLoader,
        scan_runner=fake_scan_runner,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    # The override universe is used, not the registry default "fno".
    assert loaded_universes == ["hemant_super_45"]
    assert captured["universe_key"] == "hemant_super_45"
    assert captured["scan_name"] == "Env override"
    assert summary.outcomes[0].universe_key == "hemant_super_45"
    assert "universe=hemant_super_45" in capsys.readouterr().out
    # params: registry default kept, config value overrides, dates added last.
    params = captured["params"]
    assert params["ema_period"] == 200
    assert params["percent"] == 9.0
    assert params["start_date"] == date(2016, 6, 5)
    assert params["end_date"] == date(2026, 6, 5)


def test_outcome_carries_structured_partial_failure_counts() -> None:
    """ALERT-001 needs partial-symbol counts, not just fatal screener counts."""
    from backend.jobs.run_daily_scan import run_daily_scan

    def fake_scan_runner(**_kwargs):
        return ScanRunResult(
            status=ScanStatus.PARTIAL,
            results=pd.DataFrame([_row_for("X")]),
            run_id=10,
            compute_failures=[
                {"symbol": "BAD", "message": "indicator failed"},
                {
                    "symbol": "AI_BAD",
                    "message": "invalid JSON",
                    "phase": "ai_validation",
                },
            ],
            rejected_result_rows=1,
            ai_validation_failures=1,
            data_quality_json={"fatal_symbols": 1, "fatal_findings": 2},
            error_message="partial symbol failures",
        )

    summary = run_daily_scan(
        screener_keys=["envelope_knoxville_buy"],
        registry_loader=lambda: {
            "envelope_knoxville_buy": _definition(
                "envelope_knoxville_buy",
                "fno",
                lambda *_args: pd.DataFrame([_row_for("X")]),
            )
        },
        universe_loader=lambda key: _fake_universe(key.upper()),
        data_loader_factory=lambda: _FakeLoader(
            last_failures=[{"symbol": "LOAD_BAD", "message": "timeout"}]
        ),
        scan_runner=fake_scan_runner,
        today=date(2026, 6, 5),
    )

    outcome = summary.outcomes[0]
    assert outcome.fatal is False
    assert outcome.loader_failures == 1
    assert outcome.compute_failures == 2
    assert outcome.rejected_result_rows == 1
    assert outcome.ai_validation_failures == 1
    assert outcome.data_quality_fatal_symbols == 1
    assert outcome.data_quality_fatal_findings == 2


def test_config_unknown_screener_is_fatal_but_keeps_running_valid_entries(
    file_session_factory,
):
    """A bad screener_key in the config behaves like a bad --screener key."""
    from backend.jobs.daily_scan_config import DailyScanEntry
    from backend.jobs.run_daily_scan import run_daily_scan

    def run(universe_df, _data_loader, _params):
        return pd.DataFrame([_row_for(str(universe_df.iloc[0]["symbol"]))])

    summary = run_daily_scan(
        scan_entries=[
            DailyScanEntry(name="Typo", screener_key="missing_screener"),
            DailyScanEntry(name="Good", screener_key="known_screener"),
        ],
        registry_loader=lambda: {
            "known_screener": _definition("known_screener", "fno", run)
        },
        universe_loader=lambda key: _fake_universe(key.upper()),
        data_loader_factory=_FakeLoader,
        session_factory=file_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    assert summary.outcomes[0].screener_key == "missing_screener"
    assert summary.outcomes[0].fatal is True
    assert summary.outcomes[0].message == "Unknown screener key."
    assert summary.outcomes[1].status is ScanStatus.SUCCESS
    assert summary.outcomes[1].run_id is not None


def test_config_unknown_universe_is_fatal(capsys):
    """An unknown universe_key override surfaces clearly via load_universe."""
    from backend.jobs.daily_scan_config import DailyScanEntry
    from backend.jobs.run_daily_scan import run_daily_scan

    def load_universe(universe_key: str) -> pd.DataFrame:
        # Mirror backend.universe_loader.load_universe's real error for a bad key.
        raise KeyError(f"Unknown universe key: {universe_key}")

    summary = run_daily_scan(
        scan_entries=[
            DailyScanEntry(
                name="Bad universe",
                screener_key="known_screener",
                universe_key="not_a_universe",
            )
        ],
        registry_loader=lambda: {
            "known_screener": _definition(
                "known_screener",
                "fno",
                lambda *_args: pd.DataFrame([_row_for("X")]),
            )
        },
        universe_loader=load_universe,
        data_loader_factory=_FakeLoader,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 1
    outcome = summary.outcomes[0]
    assert outcome.fatal is True
    assert outcome.universe_key == "not_a_universe"  # the override is reported
    output = capsys.readouterr().out
    assert "Unknown universe key" in output


def test_config_with_no_enabled_entries_exits_nonzero_and_logs_invalid(
    capsys, caplog
):
    """An all-disabled schedule is both a fatal result and a config event."""
    from backend.jobs.daily_scan_config import DailyScanEntry
    from backend.jobs.run_daily_scan import run_daily_scan

    with caplog.at_level(logging.INFO):
        summary = run_daily_scan(
            scan_entries=[
                DailyScanEntry(name="Off", screener_key="anything", enabled=False)
            ],
            # registry_loader must not even be needed: we fail before discovery.
            registry_loader=lambda: (_ for _ in ()).throw(
                AssertionError("registry should not load when nothing is enabled")
            ),
            today=date(2026, 6, 5),
        )

    assert summary.exit_code == 1
    assert len(summary.outcomes) == 1
    assert summary.outcomes[0].fatal is True
    assert "No enabled scans" in capsys.readouterr().out
    invalid = _event_fields(caplog, EVENT_DAILY_JOB_CONFIG_INVALID)
    assert len(invalid) == 1
    assert invalid[0]["reason"] == "no_enabled_scans"


def test_main_uses_config_file_and_emits_daily_job_lifecycle(tmp_path, caplog):
    """A valid config emits start, loaded, and exactly one completion event."""
    from backend.jobs.run_daily_scan import (
        DailyScanOutcome,
        DailyScanSummary,
        main,
    )

    config_path = tmp_path / "daily_scans.yaml"
    config_path.write_text(
        "daily_scans:\n"
        "  - name: Enabled scan\n"
        "    screener_key: bollinger_band_reversal\n"
        "    enabled: true\n"
        "  - name: Disabled scan\n"
        "    screener_key: heikin_ashi_supertrend\n"
        "    enabled: false\n",
        encoding="utf-8",
    )

    captured: dict[str, object] = {}

    def fake_job_runner(*, scan_entries=None, output=None, **_kwargs):
        captured["scan_entries"] = scan_entries
        return DailyScanSummary(
            outcomes=[
                DailyScanOutcome(
                    screener_key="bollinger_band_reversal",
                    universe_key="fno",
                    status=ScanStatus.SUCCESS,
                    run_id=42,
                    row_count=3,
                )
            ]
        )

    with caplog.at_level(logging.INFO):
        exit_code = main(["--config", str(config_path)], job_runner=fake_job_runner)

    assert exit_code == 0
    entries = captured["scan_entries"]
    assert [entry.screener_key for entry in entries] == [
        "bollinger_band_reversal",
        "heikin_ashi_supertrend",
    ]
    assert [entry.enabled for entry in entries] == [True, False]

    started = _event_fields(caplog, EVENT_DAILY_JOB_STARTED)
    loaded = _event_fields(caplog, EVENT_DAILY_JOB_CONFIG_LOADED)
    completed = _event_fields(caplog, EVENT_DAILY_JOB_COMPLETED)
    assert len(started) == 1
    assert started[0]["selection_mode"] == "config"
    assert len(loaded) == 1
    assert loaded[0]["entries_count"] == 2
    assert loaded[0]["enabled_count"] == 1
    assert loaded[0]["disabled_count"] == 1
    assert len(completed) == 1
    assert completed[0]["exit_code"] == 0
    assert completed[0]["success_count"] == 1
    assert completed[0]["partial_count"] == 0
    assert completed[0]["failed_count"] == 0
    assert "duration_seconds" in completed[0]


def test_main_without_config_preserves_default_path_and_logs_completion(caplog):
    """The JOB-001 path also emits one start and one completion event."""
    from backend.jobs.run_daily_scan import DailyScanSummary, main

    captured: dict[str, object] = {}

    def fake_job_runner(*, screener_keys=None, scan_entries=None, output=None, **_kw):
        captured["screener_keys"] = screener_keys
        captured["scan_entries"] = scan_entries
        return DailyScanSummary(outcomes=[])

    with caplog.at_level(logging.INFO):
        exit_code = main([], job_runner=fake_job_runner)

    assert exit_code == 0
    assert captured["screener_keys"] is None  # falls back to the default daily set
    assert captured["scan_entries"] is None  # default path does not use config
    assert len(_event_fields(caplog, EVENT_DAILY_JOB_STARTED)) == 1
    assert len(_event_fields(caplog, EVENT_DAILY_JOB_COMPLETED)) == 1


def test_main_logs_completion_when_job_runner_raises(caplog):
    """An unexpected crash still leaves one aggregate failure receipt."""
    from backend.jobs.run_daily_scan import main

    def boom(**_kwargs):
        raise RuntimeError("token=RUNNERSECRET should stay hidden")

    with caplog.at_level(logging.INFO), pytest.raises(RuntimeError):
        main([], job_runner=boom)

    completed = _event_fields(caplog, EVENT_DAILY_JOB_COMPLETED)
    assert len(completed) == 1
    assert completed[0]["exit_code"] == 1
    assert completed[0]["error_type"] == "RuntimeError"
    assert "RUNNERSECRET" not in str(completed[0])


def test_main_rejects_config_combined_with_screener():
    """`--config` and `--screener` are mutually exclusive (clear argparse error)."""
    from backend.jobs.run_daily_scan import main

    with pytest.raises(SystemExit):
        main(["--config", "ignored.yaml", "--screener", "envelope"])


def test_main_bad_config_logs_invalid_and_completes_without_running_job(
    tmp_path, capsys, caplog
):
    """Malformed YAML produces structured failure and completion events."""
    from backend.jobs.run_daily_scan import main

    config_path = tmp_path / "broken.yaml"
    config_path.write_text("daily_scans: [unclosed\n", encoding="utf-8")

    def fail_runner(**_kwargs):
        raise AssertionError("job runner must not run on a bad config")

    with caplog.at_level(logging.INFO):
        exit_code = main(["--config", str(config_path)], job_runner=fail_runner)

    assert exit_code == 1
    assert "Could not load config" in capsys.readouterr().out
    invalid = _event_fields(caplog, EVENT_DAILY_JOB_CONFIG_INVALID)
    completed = _event_fields(caplog, EVENT_DAILY_JOB_COMPLETED)
    assert len(invalid) == 1
    assert invalid[0]["error_type"] == "DailyScanConfigError"
    assert len(completed) == 1
    assert completed[0]["exit_code"] == 1


def test_main_redacts_secret_shaped_text_from_config_load_errors(
    tmp_path, capsys, caplog
):
    """A config filename must not bypass the shared secret-redaction boundary.

    Command-line paths are normally harmless, but schedulers can interpolate
    environment values into arguments. This deliberately fake filename proves a
    token-shaped value is masked before the config error reaches stdout.
    """
    from backend.jobs.run_daily_scan import main

    missing_path = tmp_path / "token=SUPERSECRET.yaml"

    with caplog.at_level(logging.INFO):
        exit_code = main(["--config", str(missing_path)])

    assert exit_code == 1
    output = capsys.readouterr().out
    assert "token=***REDACTED***" in output
    assert "SUPERSECRET" not in output
    invalid = _event_fields(caplog, EVENT_DAILY_JOB_CONFIG_INVALID)
    assert len(invalid) == 1
    assert "SUPERSECRET" not in str(invalid[0])
