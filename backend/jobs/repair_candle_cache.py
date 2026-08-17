"""Headless candle cache-repair job (DATA-002).

Beginner note:
``python app.py`` runs this same repair automatically after it finishes topping up
candles. This module exists so you can also run it on its own — to inspect what
*would* change (``--dry-run``), to force a retry of a symbol the sidecar is
currently skipping (``--force``), or to work on one ticker while debugging
(``--symbol RELIANCE``).

Usage::

    python -m backend.jobs.repair_candle_cache --dry-run
    python -m backend.jobs.repair_candle_cache
    python -m backend.jobs.repair_candle_cache --force --symbol MOTHERSON

The job is idempotent: a second consecutive run over a clean cache does nothing
and spends no Dhan requests.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TextIO, cast

from backend.config import ensure_project_dirs
from backend.daily_data_loader import DEFAULT_HISTORY_YEARS_BACK, DailyDataLoader
from backend.data_quality.cache_repair import CacheRepairSummary, repair_universe
from backend.dhan_client import DhanDataClient
from backend.observability import (
    EVENT_CANDLE_CACHE_REPAIR_COMPLETED,
    EVENT_CANDLE_CACHE_REPAIR_STARTED,
    configure_logging,
    log_event,
)
from backend.security import install_secret_redaction_filter, redact_text
from backend.storage import (
    create_candle_repair_run,
    ensure_database_schema,
    finish_candle_repair_run,
    session_scope,
)
from backend.universe_loader import union_of_mapped_universes

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RepairJobOutcome:
    """What one job invocation did, plus the process exit code to return."""

    summary: CacheRepairSummary | None
    exit_code: int = 0
    message: str | None = None


def run_repair_candle_cache(
    *,
    symbols: Sequence[str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    today: dt.date | None = None,
    years_back: int = DEFAULT_HISTORY_YEARS_BACK,
    trigger: str = "cli",
    out: TextIO | None = None,
) -> RepairJobOutcome:
    """Repair the cached daily candles for every mapped universe symbol.

    Returns a ``RepairJobOutcome`` rather than raising, so a scheduled run exits
    with a status code instead of a traceback. A missing Dhan credential is not
    fatal: the offline repairs (de-duplication, dropping unusable rows) still run,
    and anything needing a re-download is reported as unrepairable.
    """
    stream = out if out is not None else sys.stdout
    install_secret_redaction_filter(logging.getLogger())
    ensure_project_dirs()
    resolved_today = today or dt.date.today()

    union = union_of_mapped_universes()
    if union.empty:
        print(
            "[repair] No mapped stocks found in any universe; nothing to repair.",
            file=stream,
            flush=True,
        )
        return RepairJobOutcome(summary=None, message="no mapped stocks")

    # ``to_dict("records")`` is typed with ``Hashable`` keys; a universe frame's
    # columns are always plain strings, so narrow it for the repair signature.
    rows = cast(list[dict[str, object]], union.to_dict("records"))
    if symbols:
        wanted = {str(symbol).strip().upper() for symbol in symbols}
        rows = [row for row in rows if str(row.get("symbol", "")).strip().upper() in wanted]
        if not rows:
            print(
                f"[repair] None of {sorted(wanted)} are mapped in any universe.",
                file=stream,
                flush=True,
            )
            return RepairJobOutcome(summary=None, exit_code=1, message="no matching symbols")

    # A cache-only loader is a legitimate mode here (see DailyDataLoader.__init__):
    # the offline half of every repair still works without credentials.
    loader = _build_loader(stream)

    log_event(
        logger,
        EVENT_CANDLE_CACHE_REPAIR_STARTED,
        trigger=trigger,
        symbols=len(rows),
        dry_run=dry_run,
        force=force,
    )
    print(
        f"[repair] Checking {len(rows)} cached symbol(s)"
        f"{' (dry run — nothing will be written)' if dry_run else ''}...",
        file=stream,
        flush=True,
    )

    summary = repair_universe(
        loader,
        rows,
        today=resolved_today,
        years_back=years_back,
        force=force,
        dry_run=dry_run,
    )
    print_repair_summary(summary, out=stream, dry_run=dry_run)

    if not dry_run:
        persist_repair_summary(summary, trigger=trigger, stream=stream)

    log_event(
        logger,
        EVENT_CANDLE_CACHE_REPAIR_COMPLETED,
        trigger=trigger,
        symbols_checked=summary.symbols_checked,
        symbols_repaired=summary.symbols_repaired,
        symbols_unrepairable=summary.symbols_unrepairable,
        symbols_failed=summary.symbols_failed,
        rows_removed=summary.rows_removed,
        refetch_count=summary.refetch_count,
        dry_run=dry_run,
    )
    # Deliberately exit 0 even with unrepairable symbols: "the vendor's data is
    # still dirty" is a reported condition, not a job failure. Only an outright
    # error (an unreadable file, a write that could not complete) is non-zero.
    return RepairJobOutcome(
        summary=summary, exit_code=1 if summary.symbols_failed else 0
    )


def print_repair_summary(
    summary: CacheRepairSummary, *, out: TextIO | None = None, dry_run: bool = False
) -> None:
    """Print a per-symbol line for everything that changed, then the totals.

    Only *interesting* symbols get a line. On a healthy cache that means the
    output is two lines, which is the point: a wall of "clean" for 600 symbols
    would bury the one that needs attention.
    """
    stream = out if out is not None else sys.stdout
    verb = "would repair" if dry_run else "repaired"

    for outcome in summary.changed_outcomes:
        detail = ", ".join(outcome.actions) or "no action"
        removed = f", -{outcome.rows_removed} row(s)" if outcome.rows_removed else ""
        print(
            f"[repair]   {outcome.symbol:<14} {verb}: {detail}{removed}",
            file=stream,
            flush=True,
        )

    for outcome in summary.needs_attention:
        codes = ", ".join(outcome.after_codes) or "unknown"
        note = f" — {outcome.message}" if outcome.message else ""
        print(
            f"[repair]   {outcome.symbol:<14} STILL DIRTY ({codes}){note}",
            file=stream,
            flush=True,
        )

    print(
        f"[repair] Cache repair complete: checked={summary.symbols_checked} "
        f"clean={summary.symbols_clean} repaired={summary.symbols_repaired} "
        f"partial={summary.symbols_partially_repaired} "
        f"vendor_data={summary.symbols_vendor_data} "
        f"unrepairable={summary.symbols_unrepairable} "
        f"skipped={summary.symbols_skipped} failed={summary.symbols_failed} "
        f"rows_removed={summary.rows_removed} refetches={summary.refetch_count}.",
        file=stream,
        flush=True,
    )


def _build_loader(stream: TextIO) -> DailyDataLoader:
    """Build a loader with Dhan credentials when available, cache-only otherwise."""
    try:
        return DailyDataLoader(DhanDataClient.from_env())
    except Exception as exc:  # noqa: BLE001 - degrade to offline repairs
        logger.warning("Candle repair running without Dhan credentials")
        print(
            "[repair] WARNING: no Dhan credentials "
            f"({redact_text(str(exc))}). Offline repairs only.",
            file=stream,
            flush=True,
        )
        return DailyDataLoader(client=None)


def persist_repair_summary(
    summary: CacheRepairSummary, *, trigger: str, stream: TextIO | None = None
) -> None:
    """Record the pass in ``candle_repair_runs`` for Admin health.

    Best-effort by design: the repair itself already happened on disk, so a
    database hiccup must not turn a successful cleanup into a failed job.

    Note ``symbols_repaired`` counts fully *and* partially repaired symbols: both
    rewrote the cache file, which is what the column means. The receipt keeps the
    two apart for anyone who needs the distinction.
    """
    output = stream if stream is not None else sys.stdout
    try:
        ensure_database_schema()
        with session_scope() as session:
            run = create_candle_repair_run(session, trigger=trigger)
            finish_candle_repair_run(
                session,
                run,
                symbols_checked=summary.symbols_checked,
                symbols_repaired=summary.symbols_repaired
                + summary.symbols_partially_repaired,
                symbols_unrepairable=summary.symbols_unrepairable,
                rows_removed=summary.rows_removed,
                refetch_count=summary.refetch_count,
                receipt=summary.as_receipt(),
            )
    except Exception as exc:  # noqa: BLE001 - never fail the repair over a receipt
        logger.exception("Could not persist the candle repair receipt")
        print(
            f"[repair] WARNING: could not record the repair receipt "
            f"({type(exc).__name__}).",
            file=output,
            flush=True,
        )


def main(argv: Sequence[str] | None = None) -> int:
    """Parse CLI flags, run the repair, and return a process exit code.

    This is the ``python -m backend.jobs.repair_candle_cache`` entry point.
    ``--symbol`` is repeatable (``--symbol ABREL --symbol MOTHERSON``).
    """
    parser = argparse.ArgumentParser(
        description="Repair dirty cached daily candles (DATA-002)."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would change without writing any file.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Retry symbols whose last repair attempt is still within its cooldown.",
    )
    parser.add_argument(
        "--symbol",
        action="append",
        dest="symbols",
        help="Restrict the pass to this symbol. Repeat for multiple values.",
    )
    parser.add_argument(
        "--as-of",
        type=_parse_iso_date,
        default=None,
        help="Calendar date to treat as today, YYYY-MM-DD.",
    )
    args = parser.parse_args(argv)

    configure_logging()
    outcome = run_repair_candle_cache(
        symbols=args.symbols,
        force=args.force,
        dry_run=args.dry_run,
        today=args.as_of,
    )
    return int(outcome.exit_code)


def _parse_iso_date(value: str) -> dt.date:
    """argparse ``type=`` validator: parse a ``YYYY-MM-DD`` string into a date."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date YYYY-MM-DD") from exc


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests
    raise SystemExit(main())
