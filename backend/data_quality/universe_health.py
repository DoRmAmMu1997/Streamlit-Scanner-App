"""OBS-004 - notice when a universe silently stops being scannable.

The problem this solves
-----------------------
When Dhan's instrument master stops listing a symbol (a merger, a delisting, a
ticker change), :func:`backend.universe_builder.refresh_universe_files` writes it
into the universe CSV as ``mapping_status='missing_security_id'`` and
:func:`backend.universe_loader.mapped_only` then filters it out of every scan.
That is the correct behaviour - we cannot fetch candles for a security id we do
not have - but until OBS-004 nothing *said* so outside the interactive Streamlit
sidebar. ``universe_status()`` had exactly one non-test caller
(``ui/status_panel.py``), the headless daily job emitted no mapping signal, and
``backend/notifications/`` never mentioned ``mapping_status``. A universe could
therefore shrink for weeks without anyone noticing; ~3% of the Hemant Good 200
list was already unscanned when this module was written.

The shape of the answer
-----------------------
Three deliberately separate pieces, because they have different requirements:

* :func:`collect_universe_health` is **pure** - it reads the CSVs and returns
  counts. Anything can call it, including the Streamlit prefetch.
* :func:`detect_mapping_regressions` is **pure** - it compares today's counts to
  a previous set and returns only the universes that got worse.
* :func:`check_universe_health` is the **stateful** one: it needs a database
  session because detecting "worse than last time" requires a durable baseline,
  and the Render daily-scan cron runs on an ephemeral filesystem with no disk.

Beginner note on why only the alerting path persists:
Whoever writes the baseline decides what "last time" means. If the morning
Streamlit prefetch also recorded a snapshot, a symbol that dropped out at 09:00
would already be part of the baseline by the time the evening job ran, and the
alert would never fire. So the prefetch calls the pure helpers for logging only,
and ``check_universe_health`` - used by the daily job - owns the baseline.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from backend.config import UNIVERSE_DIR
from backend.observability import (
    EVENT_UNIVERSE_HEALTH_CHECKED,
    EVENT_UNIVERSE_MAPPING_REGRESSED,
    log_event,
)

if TYPE_CHECKING:  # pragma: no cover - import-cycle break for type checking only
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

#: How many unmapped symbol names we are willing to store and report per
#: universe. A universe whose CSV went badly wrong should not be able to write an
#: unbounded blob into the database or a multi-page message into a Telegram
#: alert; past this point the count alone tells the story.
MAX_REPORTED_SYMBOLS = 25


@dataclass(frozen=True)
class UniverseHealth:
    """Mapping health for one universe at one point in time."""

    universe_key: str
    total_rows: int
    mapped_rows: int
    unmapped_symbols: tuple[str, ...]

    @property
    def unmapped_rows(self) -> int:
        """Rows the scanner cannot fetch, derived from the two counts.

        Beginner note: derived rather than stored so it can never disagree with
        ``total_rows``/``mapped_rows``. The database column is written from this
        property, so the persisted row is internally consistent too.
        """
        return max(self.total_rows - self.mapped_rows, 0)


@dataclass(frozen=True)
class MappingRegression:
    """One universe that has more unmapped symbols than it did last check."""

    universe_key: str
    previous_unmapped: int
    current_unmapped: int
    newly_unmapped: tuple[str, ...]

    def describe(self) -> str:
        """Return a one-line, alert-ready summary naming what changed."""
        delta = self.current_unmapped - self.previous_unmapped
        detail = ", ".join(self.newly_unmapped) if self.newly_unmapped else "symbols not named"
        return (
            f"{self.universe_key}: {self.previous_unmapped} -> {self.current_unmapped} "
            f"unmapped (+{delta}); {detail}"
        )


@dataclass(frozen=True)
class UniverseHealthReport:
    """What one check found: every universe's counts, plus any regressions."""

    snapshots: tuple[UniverseHealth, ...] = ()
    regressions: tuple[MappingRegression, ...] = ()


def _unmapped_symbols(frame: Any) -> tuple[str, ...]:
    """Return the sorted, capped symbols in ``frame`` that cannot be fetched.

    Beginner note: ``universe_status()`` gives us counts but not names, and a
    count alone makes for a useless alert ("one more symbol is missing" - which
    one?). This re-reads the same frame for the names. Sorted so the stored list
    is stable and two runs are directly comparable.
    """
    if "symbol" not in frame.columns:
        return ()
    if "mapping_status" in frame.columns:
        unmapped = frame.loc[
            ~frame["mapping_status"].astype(str).str.lower().eq("mapped")
        ]
    elif "security_id" in frame.columns:
        unmapped = frame.loc[frame["security_id"].astype(str).str.strip().eq("")]
    else:
        return ()
    symbols = sorted({str(value).strip() for value in unmapped["symbol"] if str(value).strip()})
    return tuple(symbols[:MAX_REPORTED_SYMBOLS])


def collect_universe_health(
    universe_dir: Path | str = UNIVERSE_DIR,
) -> tuple[UniverseHealth, ...]:
    """Read every universe CSV and return its mapping health. Never raises.

    Reuses :func:`backend.universe_loader.universe_status` for the counts rather
    than re-deriving them, so the numbers here and the numbers in the Streamlit
    status panel can never disagree. A universe whose CSV is missing or
    unreadable yields a zero row instead of an exception - a health check that
    can take the daily job down would be worse than the problem it reports.
    """
    # Imported here rather than at module scope: universe_loader pulls in pandas
    # and the universe registry, and this module is imported by the storage-aware
    # job path. Keeping it local matches the lazy-import convention used to break
    # cycles elsewhere in backend/.
    import pandas as pd

    from backend.universe_builder import UNIVERSE_CONFIG, universe_file_path
    from backend.universe_loader import universe_status

    results: list[UniverseHealth] = []
    for universe_key in UNIVERSE_CONFIG:
        status = universe_status(universe_key, universe_dir)
        total_rows = int(status.get("rows", 0) or 0)
        mapped_rows = int(status.get("mapped_rows", 0) or 0)

        symbols: tuple[str, ...] = ()
        # Only worth re-reading the file when it exists, parsed cleanly, and
        # actually has something unmapped to name.
        if status.get("exists") and not status.get("error") and total_rows > mapped_rows:
            try:
                frame = pd.read_csv(
                    universe_file_path(universe_key, universe_dir), dtype=str
                ).fillna("")
                symbols = _unmapped_symbols(frame)
            except Exception:  # noqa: BLE001 - a health check must never break the caller
                logger.warning(
                    "could not read unmapped symbols for universe %s", universe_key, exc_info=True
                )

        results.append(
            UniverseHealth(
                universe_key=universe_key,
                total_rows=total_rows,
                mapped_rows=mapped_rows,
                unmapped_symbols=symbols,
            )
        )
    return tuple(results)


def log_universe_health(snapshots: Sequence[UniverseHealth]) -> None:
    """Emit one structured event per universe so the counts are searchable.

    This runs on every check, not just when something is wrong: an operator
    asking "was Good 200 already down two names last Tuesday?" needs the routine
    receipts, not only the alarms.
    """
    for snapshot in snapshots:
        log_event(
            logger,
            EVENT_UNIVERSE_HEALTH_CHECKED,
            universe_key=snapshot.universe_key,
            rows=snapshot.total_rows,
            mapped=snapshot.mapped_rows,
            unmapped=snapshot.unmapped_rows,
        )


def detect_mapping_regressions(
    current: Sequence[UniverseHealth],
    previous: Mapping[str, Any],
) -> tuple[MappingRegression, ...]:
    """Return the universes whose unmapped count grew since ``previous``.

    ``previous`` maps a universe key to its last persisted snapshot row (anything
    exposing ``unmapped_rows`` and ``unmapped_symbols_json``).

    Two deliberate rules:

    * **A universe with no previous row never regresses.** The first check has no
      baseline, so treating "absent" as zero would alert on every pre-existing
      unmapped symbol - exactly the noise that makes people mute alerts.
    * **Only an increase counts.** A universe sitting at a steady three unmapped
      symbols is already-known damage and stays quiet; recovery (the count going
      down) is good news and is not an alert either.
    """
    regressions: list[MappingRegression] = []
    for snapshot in current:
        baseline = previous.get(snapshot.universe_key)
        if baseline is None:
            continue
        previous_unmapped = int(getattr(baseline, "unmapped_rows", 0) or 0)
        if snapshot.unmapped_rows <= previous_unmapped:
            continue

        stored = getattr(baseline, "unmapped_symbols_json", None) or {}
        known = {str(value) for value in stored.get("symbols", [])}
        newly = tuple(symbol for symbol in snapshot.unmapped_symbols if symbol not in known)
        regressions.append(
            MappingRegression(
                universe_key=snapshot.universe_key,
                previous_unmapped=previous_unmapped,
                current_unmapped=snapshot.unmapped_rows,
                newly_unmapped=newly,
            )
        )
    return tuple(regressions)


def check_universe_health(
    session: Session,
    *,
    universe_dir: Path | str = UNIVERSE_DIR,
) -> UniverseHealthReport:
    """Collect, log, compare against the stored baseline, then record today's.

    The caller owns the transaction (REFACTOR-002): this adds rows and flushes,
    but never commits.

    Ordering matters. The baseline is read *before* today's snapshot is written,
    otherwise every run would compare against itself and nothing would ever
    regress. Writing afterwards is also what makes the alert fire exactly once -
    the next run's baseline already contains the drop-out.
    """
    # Local import keeps the repository boundary one-directional and avoids a
    # module-level cycle between data_quality and storage.
    from backend.storage import repository

    snapshots = collect_universe_health(universe_dir)
    log_universe_health(snapshots)

    previous = repository.get_latest_universe_health_snapshots(session)
    regressions = detect_mapping_regressions(snapshots, previous)

    for regression in regressions:
        log_event(
            logger,
            EVENT_UNIVERSE_MAPPING_REGRESSED,
            level=logging.WARNING,
            universe_key=regression.universe_key,
            previous_unmapped=regression.previous_unmapped,
            current_unmapped=regression.current_unmapped,
            newly_unmapped=list(regression.newly_unmapped),
        )

    repository.record_universe_health_snapshots(
        session,
        [
            {
                "universe_key": snapshot.universe_key,
                "total_rows": snapshot.total_rows,
                "mapped_rows": snapshot.mapped_rows,
                "unmapped_rows": snapshot.unmapped_rows,
                "unmapped_symbols": list(snapshot.unmapped_symbols),
            }
            for snapshot in snapshots
        ],
    )
    return UniverseHealthReport(snapshots=snapshots, regressions=regressions)
