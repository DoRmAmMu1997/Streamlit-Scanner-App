"""Headless daily scan command (JOB-001 set + JOB-002 config schedule).

Run with the built-in deterministic set::

    python -m backend.jobs.run_daily_scan

or with a YAML schedule of named scan batches (JOB-002)::

    python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml

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

Exit-code rule:
Schedulers usually decide success/failure from the process exit code, not from a
Streamlit toast or a table. That is why this command is stricter than the UI:
it exits non-zero if history was not persisted, even though the UI can still show
in-memory rows when the database is temporarily unavailable.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, TextIO

import pandas as pd

from backend.daily_data_loader import (
    DailyDataLoader,
    DEFAULT_HISTORY_YEARS_BACK,
    history_start_date,
)
from backend.dhan_client import DhanDataClient
from backend.jobs.daily_scan_config import (
    DailyScanConfigError,
    DailyScanEntry,
    load_daily_scan_config,
)
from backend.observability import configure_logging
from backend.scanning import ScanRunResult, ScanStatus, run_scan
from backend.scanning.service import SessionFactory
from backend.screener_registry import ScreenerDefinition, discover_screeners
from backend.security import (
    redact_exception,
    redact_text,
)
from backend.storage.database import session_scope
from backend.universe_loader import load_universe


# JOB-002 will make scanner selection configurable. JOB-001 keeps the first
# scheduled set in code so the command is useful immediately without introducing
# a YAML parser or schedule format. These three screeners are deterministic and
# already protected by TEST-001 golden snapshots, so they are safer defaults than
# AI-backed screeners that depend on optional external services.
DEFAULT_DAILY_SCAN_KEYS = (
    "bollinger_band_reversal",
    "heikin_ashi_supertrend",
    "envelope_knoxville_buy",
)

# Store the trigger as a stable string because it is persisted into scan_runs.
# Future history/comparison views can distinguish UI runs ("ui:email") from
# scheduled runs ("job:daily_scan") without guessing from timestamps.
TRIGGERED_BY = "job:daily_scan"

# These aliases make the injection points self-documenting. Production passes
# the real functions by default; tests pass fakes with the same tiny contracts.
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

    Field guide for beginners:
    - ``status`` is the SCAN-003 database status when the service ran.
    - ``run_id`` is the persisted ``scan_runs.id``. ``None`` means history could
      not be written.
    - ``message`` is intentionally concise and secret-safe; it should explain
      the class of problem without echoing raw broker/database exception text.
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
    """All screener outcomes from one daily command invocation.

    The summary is deliberately separate from printing. Tests can assert on this
    object directly, while the CLI can still emit human-readable lines for an
    operator watching a scheduled job.
    """

    outcomes: list[DailyScanOutcome]

    @property
    def exit_code(self) -> int:
        """Return the process exit code expected by schedulers and CI.

        A single fatal outcome makes the whole process fail. Non-fatal PARTIAL
        outcomes stay exit 0 because the run was recorded and can be inspected.
        """
        return 1 if any(outcome.fatal for outcome in self.outcomes) else 0


def run_daily_scan(
    *,
    screener_keys: Sequence[str] | None = None,
    scan_entries: Sequence[DailyScanEntry] | None = None,
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

    High-level flow:
    1. Decide the work list: JOB-002 ``scan_entries`` (from ``--config``) take
       precedence; otherwise JOB-001's ``screener_keys`` (or the default set).
    2. Skip disabled entries and bail out clearly if nothing is enabled.
    3. Discover the registry once.
    4. For each enabled entry, load its universe (config override or the
       screener's configured default) and build a Dhan-backed data loader.
    5. Delegate the actual scan/persistence lifecycle to ``run_scan``.
    6. Print one line per screener and return one summary exit code.
    """
    out = output or sys.stdout
    run_date = today or date.today()
    start_date = history_start_date(DEFAULT_HISTORY_YEARS_BACK, run_date)

    # Normalize both entry points to one list of DailyScanEntry objects so the run
    # loop below has a single shape to iterate. JOB-002 config entries take
    # precedence; otherwise each JOB-001 --screener key (or default) becomes a
    # trivially-enabled entry with no universe/params overrides.
    if scan_entries is not None:
        entries = list(scan_entries)
    else:
        entries = [
            DailyScanEntry(name=key, screener_key=key)
            for key in (screener_keys or DEFAULT_DAILY_SCAN_KEYS)
        ]

    # Disabled entries are skipped but still reported, so an operator can see at a
    # glance that the schedule deliberately left them out.
    for entry in entries:
        if not entry.enabled:
            print(
                f"[daily-scan] SKIPPED screener={entry.screener_key} "
                f"name={entry.name!r} (disabled)",
                file=out,
                flush=True,
            )

    enabled_entries = [entry for entry in entries if entry.enabled]
    if not enabled_entries:
        # Only reachable via --config: the default/--screener paths are always
        # enabled. A scheduled job that would silently do nothing is treated as a
        # configuration error so the scheduler's exit-code check notices it.
        outcome = DailyScanOutcome(
            screener_key="<config>",
            fatal=True,
            message=(
                "No enabled scans in the config. "
                "Set enabled: true on at least one entry."
            ),
        )
        _print_outcome(out, outcome)
        return DailyScanSummary(outcomes=[outcome])

    try:
        registry = registry_loader()
    except Exception as exc:  # noqa: BLE001 - command boundary must become exit code
        # Registry discovery happens before any individual screener can run. If it
        # fails, the safest command behavior is one fatal synthetic outcome.
        # redact_exception keeps the exception type plus a secret-masked message,
        # so import/config errors stay useful without leaking paths or tokens.
        outcome = DailyScanOutcome(
            screener_key="<registry>",
            fatal=True,
            message=f"Could not discover screeners. {redact_exception(exc)}",
        )
        _print_outcome(out, outcome)
        return DailyScanSummary(outcomes=[outcome])

    print(
        f"[daily-scan] Running {len(enabled_entries)} screener(s) "
        f"for data through {run_date.isoformat()}.",
        file=out,
        flush=True,
    )

    outcomes: list[DailyScanOutcome] = []
    for entry in enabled_entries:
        definition = registry.get(entry.screener_key)
        if definition is None:
            # Keep going after an unknown key. A scheduled config may contain one
            # typo and two valid screeners; running the valid work gives operators
            # useful history while still returning exit 1.
            outcome = DailyScanOutcome(
                screener_key=entry.screener_key,
                universe_key=entry.universe_key,
                fatal=True,
                message="Unknown screener key.",
            )
        else:
            outcome = _run_one_screener(
                definition=definition,
                universe_loader=universe_loader,
                data_client_factory=data_client_factory,
                data_loader_factory=data_loader_factory,
                scan_runner=scan_runner,
                session_factory=session_factory,
                start_date=start_date,
                end_date=run_date,
                universe_key=entry.universe_key,
                params_override=entry.params,
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
    """Parse CLI arguments and return an integer process exit code.

    ``job_runner`` is injectable for tests. That lets tests prove argument
    parsing without discovering real screeners or trying to create a Dhan client.
    """
    out = output or sys.stdout
    parser = argparse.ArgumentParser(
        description="Run the scanner's configured daily screeners without Streamlit."
    )
    # --screener (JOB-001) and --config (JOB-002) are two ways to choose the same
    # thing, so making them mutually exclusive turns "I passed both" into a clear
    # argparse error instead of a silent precedence surprise.
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument(
        "--screener",
        dest="screener_keys",
        action="append",
        help=(
            "Run one screener key. Repeat to run multiple. "
            "Defaults to the JOB-001 deterministic daily set. "
            "Cannot be combined with --config."
        ),
    )
    selection.add_argument(
        "--config",
        dest="config_path",
        help=(
            "Path to a YAML daily-scan schedule "
            "(see config/daily_scans.example.yaml). Runs the enabled entries. "
            "Cannot be combined with --screener."
        ),
    )
    args = parser.parse_args(argv)

    if args.config_path:
        try:
            scan_entries = load_daily_scan_config(args.config_path)
        except DailyScanConfigError as exc:
            # A bad config is an operator error, not a crash: print one clear line
            # and exit non-zero so a scheduler notices the misconfiguration. The
            # message is our own (file path + reason), never a raw broker/DB token.
            # The config path comes from command-line input. Although paths are
            # normally harmless, a scheduler may interpolate an environment
            # value into one. Reuse SEC-002 here so secret-shaped path fragments
            # cannot bypass the normal CLI redaction boundary.
            safe_message = redact_text(str(exc))
            print(
                f"[daily-scan] Could not load config: {safe_message}",
                file=out,
                flush=True,
            )
            return 1
        summary = job_runner(scan_entries=scan_entries, output=out)
    else:
        summary = job_runner(screener_keys=args.screener_keys or None, output=out)
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
    universe_key: str | None = None,
    params_override: Mapping[str, Any] | None = None,
) -> DailyScanOutcome:
    """Prepare one screener's inputs, run SCAN-003, and classify the outcome.

    Setup failures (universe file missing, credentials missing, loader creation
    failure) happen before ``run_scan`` can create a ``scan_runs`` row, so this
    helper turns them into fatal command outcomes. Once setup succeeds, the scan
    service owns the persistence lifecycle and returns a ``ScanRunResult``.

    ``universe_key`` and ``params_override`` are JOB-002 config overrides. When a
    config entry omits them (and for the default / --screener paths), the
    screener's registry universe and default params are used, matching JOB-001.
    An unknown override universe surfaces here as a clear setup failure, because
    ``universe_loader`` raises ``KeyError("Unknown universe key: ...")``.
    """
    resolved_universe = universe_key or definition.universe
    try:
        universe_df = universe_loader(resolved_universe)
        data_loader = _make_data_loader(
            data_client_factory=data_client_factory,
            data_loader_factory=data_loader_factory,
        )
    except Exception as exc:  # noqa: BLE001 - setup failures should become rows
        # Broker/DB/config exceptions can include tokens, URLs, or local paths, so
        # route them through redact_exception: it keeps the exception type and a
        # secret-masked message. The detailed traceback still belongs in the logs.
        return DailyScanOutcome(
            screener_key=definition.key,
            universe_key=resolved_universe,
            fatal=True,
            message=f"Setup failed. {redact_exception(exc)}",
        )

    params = dict(definition.default_params)
    # Copy defaults before mutating. Registry metadata is shared for the whole
    # process; mutating it here would leak one run's overrides/dates into future
    # runs or tests. Apply config overrides first, then add the date window last
    # so a config entry cannot accidentally override the run dates.
    if params_override:
        params.update(params_override)
    params.update({"start_date": start_date, "end_date": end_date})

    try:
        result = scan_runner(
            screener_key=definition.key,
            universe_key=resolved_universe,
            run_callable=definition.run,
            universe_df=universe_df,
            data_loader=data_loader,
            params=params,
            triggered_by=TRIGGERED_BY,
            session_factory=session_factory,
        )
    except Exception as exc:  # noqa: BLE001 - unexpected service failure
        # run_scan normally converts screener and persistence failures into a
        # result object. This branch is only for unexpected service-boundary
        # exceptions; redact_exception keeps it secret-safe like the rest.
        return DailyScanOutcome(
            screener_key=definition.key,
            universe_key=resolved_universe,
            fatal=True,
            message=f"Scan service failed. {redact_exception(exc)}",
        )

    row_count = 0 if result.results is None else len(result.results)
    fatal = result.status is ScanStatus.FAILED or result.run_id is None
    if result.run_id is None:
        # SCAN-003 is intentionally best-effort for the interactive UI: the user
        # can still see fresh in-memory rows if the database is down. A scheduled
        # job has a different contract. If history is missing, tomorrow's
        # comparison/history tasks cannot know what ran today, so the command
        # must fail loudly.
        message = "History was not persisted."
    elif result.status is ScanStatus.FAILED:
        # SCAN-003 stores/returns secret-safe failed-screener messages that use
        # the exception type, not the raw exception text.
        message = result.error_message or "Screener failed."
    else:
        message = "OK."

    return DailyScanOutcome(
        screener_key=definition.key,
        # Report the universe that was actually scanned. When JOB-002 supplies
        # an override, the registry default is no longer accurate for operator
        # output or callers inspecting the structured outcome.
        universe_key=resolved_universe,
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

    Why one loader per screener:
    ``DailyDataLoader`` stores run statistics such as ``last_failures`` and cache
    hit/miss counts on the loader instance. Creating a fresh loader keeps those
    stats scoped to the screener whose status will be persisted.
    """
    if data_loader_factory is not None:
        return data_loader_factory()
    return DailyDataLoader(data_client_factory())


def _print_outcome(output: TextIO, outcome: DailyScanOutcome) -> None:
    """Print one concise, secret-safe status line for operators.

    This helper never receives raw exception objects. Callers first translate
    failures into short messages, then this function formats those messages
    consistently. That separation keeps accidental secret printing harder.
    """
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
    # Configure logging only when run as a script: tests call main() directly and
    # should not inherit a process-wide logging config. OBS-001's configure_logging
    # sets the level from LOG_LEVEL, renders JSON in production (text in
    # development), and installs the SEC-002 redaction filter so the run_scan and
    # data-loader diagnostics stay secret-safe.
    configure_logging()
    raise SystemExit(main())
