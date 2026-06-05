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

from contextlib import contextmanager
from datetime import date
from typing import Any

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.scanning import ScanRunResult, ScanStatus
from backend.screener_registry import ScreenerDefinition
from backend.storage.models import Base
from backend.storage.repository import get_latest_scan_runs, get_scan_results


@pytest.fixture
def job_engine(tmp_path) -> Engine:
    """Create a throwaway SQLite database for job-history assertions.

    Beginner note:
    The production command writes to whatever ``DATABASE_URL`` points at. Tests
    use this temp database instead so a normal pytest run cannot pollute local
    scan history.
    """
    engine = create_engine(
        f"sqlite:///{(tmp_path / 'job-scan-history.db').as_posix()}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        """Match the app's SQLite parent/child safety setting."""
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def job_session_factory(job_engine):
    """Return the transaction helper shape expected by ``run_scan``.

    ``run_scan`` does not create SQLAlchemy sessions by hand; it asks for a
    factory that behaves like ``backend.storage.database.session_scope``. This
    fixture gives it the same commit-on-success / rollback-on-error behavior,
    but against the temporary test engine.
    """

    @contextmanager
    def factory():
        with Session(job_engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return factory


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
    job_engine,
    job_session_factory,
    capsys,
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
        session_factory=job_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    assert [outcome.screener_key for outcome in summary.outcomes] == list(
        DEFAULT_DAILY_SCAN_KEYS
    )
    assert loaded_universes == ["fno", "fno", "hemant_super_45"]
    assert "LEAK" not in capsys.readouterr().out

    for params in seen_params.values():
        assert params["threshold"] == 7
        assert params["start_date"] == date(2016, 6, 5)
        assert params["end_date"] == date(2026, 6, 5)

    with Session(job_engine) as session:
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


def test_partial_scan_is_recorded_and_still_exits_zero(job_session_factory):
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
        session_factory=job_session_factory,
        today=date(2026, 6, 5),
    )

    assert summary.exit_code == 0
    assert summary.outcomes[0].status is ScanStatus.PARTIAL
    assert summary.outcomes[0].fatal is False


def test_failed_screener_is_recorded_and_exits_nonzero(job_session_factory, capsys):
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
        session_factory=job_session_factory,
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


def test_unknown_screener_exits_nonzero_and_continues_known_scans(job_session_factory):
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
        session_factory=job_session_factory,
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
    )

    assert exit_code == 0
    assert seen_keys == ["technical_analysis", "sixty_seven_ka_funda"]
