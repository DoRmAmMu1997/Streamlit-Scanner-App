"""Headless VALID-004 command for computing stored signal forward returns.

Run a bounded batch with:

    python -m backend.jobs.compute_forward_returns --limit 500

Beginner note:
The Streamlit validation dashboard is intentionally read-only. This command is
the operator/scheduler tool that fills the rows the dashboard inspects. Keeping
the compute pass out of Streamlit avoids a long Dhan-backed batch blocking a
browser rerun or surprising a user who only wanted to look at historical metrics.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from backend.daily_data_loader import DailyDataLoader
from backend.dhan_client import DhanDataClient
from backend.observability import (
    EVENT_FORWARD_RETURNS_JOB_COMPLETED,
    EVENT_FORWARD_RETURNS_JOB_FAILED,
    EVENT_FORWARD_RETURNS_JOB_STARTED,
    configure_logging,
    log_event,
)
from backend.security import redact_exception
from backend.storage.database import ensure_database_schema, session_scope
from backend.validation import (
    FORWARD_RETURN_HORIZONS,
    ForwardReturnRunSummary,
    compute_pending_forward_returns,
)

logger = logging.getLogger(__name__)

EnsureSchema = Callable[[], object]
SessionFactory = Callable[[], Any]
DataClientFactory = Callable[[], Any]
DataLoaderFactory = Callable[[Any], Any]
ComputeService = Callable[..., ForwardReturnRunSummary]


@dataclass(frozen=True)
class ForwardReturnJobOutcome:
    """Command result that can be asserted in tests and mapped to an exit code."""

    summary: ForwardReturnRunSummary
    fatal: bool = False
    message: str = ""

    @property
    def exit_code(self) -> int:
        """Schedulers treat a non-zero process exit code as failed work."""
        return 1 if self.fatal else 0


def run_compute_forward_returns(
    *,
    limit: int | None = 500,
    as_of: dt.date | None = None,
    horizons: Sequence[int] = FORWARD_RETURN_HORIZONS,
    ensure_schema: EnsureSchema = ensure_database_schema,
    session_factory: SessionFactory = session_scope,
    data_client_factory: DataClientFactory = DhanDataClient.from_env,
    data_loader_factory: DataLoaderFactory | None = None,
    compute_service: ComputeService = compute_pending_forward_returns,
    output: TextIO | None = None,
) -> ForwardReturnJobOutcome:
    """Run one forward-return compute batch and print a secret-safe summary.

    Dependency injection keeps the command testable without Dhan credentials,
    network calls, Streamlit, or the developer's real local database.
    """
    out = output or sys.stdout
    normalized_horizons = tuple(int(horizon) for horizon in horizons)
    log_event(
        logger,
        EVENT_FORWARD_RETURNS_JOB_STARTED,
        limit=limit,
        as_of=as_of.isoformat() if as_of else None,
        horizons=list(normalized_horizons),
    )

    try:
        # Schema bootstrap happens before the session and data loader so a fresh
        # deployment gets the same best-effort chance to persist validation rows
        # as the web app startup path.
        ensure_schema()
        client = data_client_factory()
        loader = (
            data_loader_factory(client)
            if data_loader_factory is not None
            else DailyDataLoader(client)
        )
        with session_factory() as session:
            summary = compute_service(
                session,
                loader,
                as_of=as_of,
                horizons=normalized_horizons,
                limit=limit,
            )
    except Exception as exc:  # noqa: BLE001 - command boundary becomes exit code
        safe_message = redact_exception(exc)
        outcome = ForwardReturnJobOutcome(
            summary=ForwardReturnRunSummary(),
            fatal=True,
            message=safe_message,
        )
        _print_outcome(out, outcome)
        log_event(
            logger,
            EVENT_FORWARD_RETURNS_JOB_FAILED,
            level=logging.ERROR,
            error=safe_message,
        )
        return outcome

    outcome = ForwardReturnJobOutcome(summary=summary, message="completed")
    _print_outcome(out, outcome)
    log_event(
        logger,
        EVENT_FORWARD_RETURNS_JOB_COMPLETED,
        total_signals=summary.total_signals,
        computed=summary.computed,
        pending=summary.pending,
        insufficient=summary.insufficient,
        benchmark_computed=summary.benchmark_computed,
        benchmark_missing=summary.benchmark_missing,
    )
    return outcome


def _print_outcome(out: TextIO, outcome: ForwardReturnJobOutcome) -> None:
    if outcome.fatal:
        print(f"[forward-returns] FAILED {outcome.message}", file=out, flush=True)
        return
    summary = outcome.summary
    print(
        "[forward-returns] completed "
        f"total_signals={summary.total_signals} "
        f"computed={summary.computed} "
        f"pending={summary.pending} "
        f"insufficient={summary.insufficient} "
        f"benchmark_computed={summary.benchmark_computed} "
        f"benchmark_missing={summary.benchmark_missing}",
        file=out,
        flush=True,
    )


def main(
    argv: Sequence[str] | None = None,
    *,
    job_runner: Callable[..., ForwardReturnJobOutcome] = run_compute_forward_returns,
) -> int:
    """Parse CLI flags and return a process exit code."""
    parser = argparse.ArgumentParser(
        description="Compute pending VALID-002 forward-return rows for stored signals."
    )
    parser.add_argument(
        "--limit",
        type=_positive_int,
        default=500,
        help="Maximum number of signal rows to process in this batch (default: 500).",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_iso_date,
        default=None,
        help="Calendar date to treat as the latest available data, YYYY-MM-DD.",
    )
    parser.add_argument(
        "--horizon",
        action="append",
        type=_positive_int,
        dest="horizons",
        help="Trading-day horizon to compute. Repeat for multiple values.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    outcome = job_runner(
        limit=args.limit,
        as_of=args.as_of,
        horizons=tuple(args.horizons or FORWARD_RETURN_HORIZONS),
    )
    return int(outcome.exit_code)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _parse_iso_date(value: str) -> dt.date:
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date YYYY-MM-DD") from exc


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests
    raise SystemExit(main())
