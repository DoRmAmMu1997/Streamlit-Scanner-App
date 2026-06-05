"""JOB-001 daily scan command.

Run with:

    python -m backend.jobs.run_daily_scan

This module is intentionally small and boring. It does not know indicator math,
Streamlit widgets, or database SQL. Instead, it wires together the existing
pieces that already own those jobs:

1. ``backend.screener_registry`` discovers screeners and their configured
   universes.
2. ``backend.universe_loader`` reads the matching universe CSV.
3. ``backend.daily_data_loader`` fetches/caches candles through Dhan.
4. ``backend.scanning.run_scan`` runs the screener and persists history.

Beginner note:
A command-line job is just another caller of backend services. Keeping this
entrypoint UI-free makes it easy to schedule later without carrying Streamlit's
browser/session assumptions into production jobs.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TextIO

import pandas as pd

from backend.daily_data_loader import DailyDataLoader
from backend.dhan_client import DhanDataClient
from backend.scanning import ScanRunResult, ScanStatus, run_scan
from backend.scanning.service import SessionFactory
from backend.screener_registry import ScreenerDefinition, discover_screeners
from backend.storage.database import session_scope
from backend.universe_loader import load_universe


# JOB-002 will make this configurable. JOB-001 keeps the first scheduled set in
# code so the command is useful immediately without introducing a YAML parser or
# schedule format. These three are deterministic and already protected by
# TEST-001 golden snapshots.
DEFAULT_DAILY_SCAN_KEYS = (
    "bollinger_band_reversal",
    "heikin_ashi_supertrend",
    "envelope_knoxville_buy",
)

TRIGGERED_BY = "job:daily_scan"
HISTORY_YEARS_BACK = 10

RegistryLoader = Callable[[], Mapping[str, ScreenerDefinition]]
UniverseLoader = Callable[[str], pd.DataFrame]
DataClientFactory = Callable[[], Any]
DataLoaderFactory = Callable[[], Any]
ScanRunner = Callable[..., ScanRunResult]


@dataclass(frozen=True)
class DailyScanOutcome:
    """One line of command output and exit-code evidence for a screener.

    ``fatal`` means the scheduled job should exit non-zero. A PARTIAL scan is
    not fatal because SCAN-003 already recorded which symbols failed; operators
    can still use the persisted history. A missing ``run_id`` is fatal here even
    when the UI would be best-effort, because a headless daily job is only useful
    if later history/comparison tasks can query what happened.
    """

    screener_key: str
    universe_key: str | None = None
    status: ScanStatus | None = None
    run_id: int | None = None
    row_count: int = 0
    fatal: bool = False
    message: str = ""


@dataclass(frozen=True)
class DailyScanSummary:
    """All screener outcomes from one daily command invocation."""

    outcomes: list[DailyScanOutcome]

    @property
    def exit_code(self) -> int:
        """Return the process exit code expected by schedulers and CI."""
        return 1 if any(outcome.fatal for outcome in self.outcomes) else 0


def run_daily_scan(
    *,
    screener_keys: Sequence[str] | None = None,
    registry_loader: RegistryLoader = discover_screeners,
    universe_loader: UniverseLoader = load_universe,
    data_client_factory: DataClientFactory = DhanDataClient.from_env,
    data_loader_factory: DataLoaderFactory | None = None,
    scan_runner: ScanRunner = run_scan,
    session_factory: SessionFactory = session_scope,
    today: date | None = None,
    output: TextIO | None = None,
) -> DailyScanSummary:
    """Run selected daily screeners and print a secret-safe operator summary.

    Dependency-injection arguments are deliberate. Production uses the defaults;
    tests pass fakes so this command can be verified without Dhan credentials,
    network calls, Streamlit, or the developer's real SQLite database.
    """
    out = output or sys.stdout
    selected_keys = tuple(screener_keys or DEFAULT_DAILY_SCAN_KEYS)
    run_date = today or date.today()
    start_date = _scan_history_start_date(run_date)

    try:
        registry = registry_loader()
    except Exception as exc:  # noqa: BLE001 - command boundary must become exit code
        outcome = DailyScanOutcome(
            screener_key="<registry>",
            fatal=True,
            message=f"Could not discover screeners ({type(exc).__name__}).",
        )
        _print_outcome(out, outcome)
        return DailyScanSummary(outcomes=[outcome])

    print(
        f"[daily-scan] Running {len(selected_keys)} screener(s) "
        f"for data through {run_date.isoformat()}.",
        file=out,
        flush=True,
    )

    outcomes: list[DailyScanOutcome] = []
    for screener_key in selected_keys:
        definition = registry.get(screener_key)
        if definition is None:
            outcome = DailyScanOutcome(
                screener_key=screener_key,
                fatal=True,
                message="Unknown screener key.",
            )
            outcomes.append(outcome)
            _print_outcome(out, outcome)
            continue

        outcome = _run_one_screener(
            definition=definition,
            universe_loader=universe_loader,
            data_client_factory=data_client_factory,
            data_loader_factory=data_loader_factory,
            scan_runner=scan_runner,
            session_factory=session_factory,
            start_date=start_date,
            end_date=run_date,
        )
        outcomes.append(outcome)
        _print_outcome(out, outcome)

    summary = DailyScanSummary(outcomes=outcomes)
    if summary.exit_code:
        print("[daily-scan] Finished with fatal failure(s).", file=out, flush=True)
    else:
        print("[daily-scan] Finished successfully.", file=out, flush=True)
    return summary


def main(
    argv: Sequence[str] | None = None,
    *,
    job_runner: Callable[..., DailyScanSummary] = run_daily_scan,
    output: TextIO | None = None,
) -> int:
    """Parse CLI arguments and return an integer process exit code."""
    parser = argparse.ArgumentParser(
        description="Run the scanner's configured daily screeners without Streamlit."
    )
    parser.add_argument(
        "--screener",
        dest="screener_keys",
        action="append",
        help=(
            "Run one screener key. Repeat to run multiple. "
            "Defaults to the JOB-001 deterministic daily set."
        ),
    )
    args = parser.parse_args(argv)
    summary = job_runner(
        screener_keys=args.screener_keys or None,
        output=output or sys.stdout,
    )
    return summary.exit_code


def _run_one_screener(
    *,
    definition: ScreenerDefinition,
    universe_loader: UniverseLoader,
    data_client_factory: DataClientFactory,
    data_loader_factory: DataLoaderFactory | None,
    scan_runner: ScanRunner,
    session_factory: SessionFactory,
    start_date: date,
    end_date: date,
) -> DailyScanOutcome:
    """Prepare one screener's inputs, run SCAN-003, and classify the outcome."""
    try:
        universe_df = universe_loader(definition.universe)
        data_loader = _make_data_loader(
            data_client_factory=data_client_factory,
            data_loader_factory=data_loader_factory,
        )
    except Exception as exc:  # noqa: BLE001 - setup failures should become rows
        return DailyScanOutcome(
            screener_key=definition.key,
            universe_key=definition.universe,
            fatal=True,
            message=f"Setup failed ({type(exc).__name__}).",
        )

    params = dict(definition.default_params)
    params.update({"start_date": start_date, "end_date": end_date})

    try:
        result = scan_runner(
            screener_key=definition.key,
            universe_key=definition.universe,
            run_callable=definition.run,
            universe_df=universe_df,
            data_loader=data_loader,
            params=params,
            triggered_by=TRIGGERED_BY,
            session_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001 - unexpected service failure
        return DailyScanOutcome(
            screener_key=definition.key,
            universe_key=definition.universe,
            fatal=True,
            message=f"Scan service failed ({type(exc).__name__}).",
        )

    row_count = 0 if result.results is None else len(result.results)
    fatal = result.status is ScanStatus.FAILED or result.run_id is None
    if result.run_id is None:
        message = "History was not persisted."
    elif result.status is ScanStatus.FAILED:
        # SCAN-003 stores/returns secret-safe failed-screener messages that use
        # the exception type, not the raw exception text.
        message = result.error_message or "Screener failed."
    else:
        message = "OK."

    return DailyScanOutcome(
        screener_key=definition.key,
        universe_key=definition.universe,
        status=result.status,
        run_id=result.run_id,
        row_count=row_count,
        fatal=fatal,
        message=message,
    )


def _make_data_loader(
    *,
    data_client_factory: DataClientFactory,
    data_loader_factory: DataLoaderFactory | None,
) -> Any:
    """Create the loader used by one screener run.

    Tests usually pass ``data_loader_factory`` directly. Production leaves it as
    ``None``, which means "build the normal Dhan-backed ``DailyDataLoader`` from
    environment credentials."
    """
    if data_loader_factory is not None:
        return data_loader_factory()
    return DailyDataLoader(data_client_factory())


def _scan_history_start_date(selected_date: date) -> date:
    """Return the same 10-year candle window start date used by Streamlit scans."""
    try:
        return selected_date.replace(year=selected_date.year - HISTORY_YEARS_BACK)
    except ValueError:
        # Feb 29 minus whole years can land on a non-leap year. Match app.py and
        # DailyDataLoader by falling back to Feb 28.
        return selected_date.replace(
            month=2,
            day=28,
            year=selected_date.year - HISTORY_YEARS_BACK,
        )


def _print_outcome(output: TextIO, outcome: DailyScanOutcome) -> None:
    """Print one concise, secret-safe status line for operators."""
    status = outcome.status.value if outcome.status is not None else "fatal"
    run_id = "-" if outcome.run_id is None else str(outcome.run_id)
    universe = outcome.universe_key or "-"
    label = "FAILED" if outcome.fatal else status.upper()
    print(
        "[daily-scan] "
        f"{label:<7} screener={outcome.screener_key} "
        f"universe={universe} status={status} run_id={run_id} "
        f"rows={outcome.row_count} message={outcome.message}",
        file=output,
        flush=True,
    )


if __name__ == "__main__":  # pragma: no cover - exercised by --help verification
    raise SystemExit(main())
