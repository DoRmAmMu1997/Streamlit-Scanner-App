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
from typing import TYPE_CHECKING, Any, Literal

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

UniverseObservationStatus = Literal["valid", "missing", "unreadable", "legacy_unknown"]


@dataclass(frozen=True)
class UniverseHealth:
    """Mapping health for one universe at one point in time.

    Beginner note:
    ``observation_status`` separates a real empty CSV from a failed read. A
    failed read carries zero counts only because no counts were available; it
    must never be treated as evidence that the universe recovered to zero.
    ``membership_complete`` separately says whether the bounded symbol tuple is
    sufficient for an exact set difference.
    """

    universe_key: str
    total_rows: int
    mapped_rows: int
    unmapped_symbols: tuple[str, ...]
    observation_status: UniverseObservationStatus = "valid"
    unmapped_symbols_truncated: bool = False
    membership_complete: bool = True

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


def _unmapped_symbols(frame: Any, unmapped_mask: Any) -> tuple[tuple[str, ...], bool, bool]:
    """Return bounded names plus explicit truncation/completeness evidence.

    Beginner note:
    The count and names come from the same frame so an atomic universe refresh
    cannot make them describe different file generations. Exact newly-missing
    names are safe only when every unmapped row had a name and the 25-name cap
    did not discard any member. The booleans preserve that distinction instead
    of letting a short tuple masquerade as a complete set.
    """
    if "symbol" not in frame.columns:
        return (), False, not bool(unmapped_mask.any())
    raw_names = [str(value).strip() for value in frame.loc[unmapped_mask, "symbol"]]
    symbols = sorted({value for value in raw_names if value})
    truncated = len(symbols) > MAX_REPORTED_SYMBOLS
    membership_complete = not truncated and all(raw_names)
    return tuple(symbols[:MAX_REPORTED_SYMBOLS]), truncated, membership_complete


def collect_universe_health(
    universe_dir: Path | str = UNIVERSE_DIR,
) -> tuple[UniverseHealth, ...]:
    """Read every universe CSV and return its mapping health. Never raises.

    Each CSV is opened exactly once and that one frame supplies both counts and
    names. Missing and unreadable files still produce explicit observations so
    operators can diagnose the gap, but their zero placeholders are never valid
    baselines. A health check that can take the daily job down would be worse
    than the problem it reports.
    """
    # Imported here rather than at module scope: universe_loader pulls in pandas
    # and the universe registry, and this module is imported by the storage-aware
    # job path. Keeping it local matches the lazy-import convention used to break
    # cycles elsewhere in backend/.
    import pandas as pd

    from backend.universe_builder import UNIVERSE_CONFIG, universe_file_path
    results: list[UniverseHealth] = []
    for universe_key in UNIVERSE_CONFIG:
        path = universe_file_path(universe_key, universe_dir)
        if not path.exists():
            logger.warning("universe health source is missing for %s", universe_key)
            results.append(
                UniverseHealth(
                    universe_key=universe_key,
                    total_rows=0,
                    mapped_rows=0,
                    unmapped_symbols=(),
                    observation_status="missing",
                    membership_complete=False,
                )
            )
            continue

        try:
            frame = pd.read_csv(path, dtype=str).fillna("")
        except FileNotFoundError:
            # An atomic refresh can move the file between ``exists`` and open.
            # That race is semantically missing, not a malformed CSV.
            logger.warning("universe health source disappeared for %s", universe_key)
            status: UniverseObservationStatus = "missing"
            frame = None
        except Exception:  # noqa: BLE001 - best-effort observation boundary
            logger.warning("universe health source is unreadable for %s", universe_key, exc_info=True)
            status = "unreadable"
            frame = None

        if frame is None:
            results.append(
                UniverseHealth(
                    universe_key=universe_key,
                    total_rows=0,
                    mapped_rows=0,
                    unmapped_symbols=(),
                    observation_status=status,
                    membership_complete=False,
                )
            )
            continue

        total_rows = len(frame)
        if "mapping_status" in frame.columns:
            mapped_mask = frame["mapping_status"].astype(str).str.lower().eq("mapped")
        elif "security_id" in frame.columns:
            mapped_mask = frame["security_id"].astype(str).str.strip().ne("")
        else:
            mapped_mask = pd.Series(False, index=frame.index)
        mapped_rows = int(mapped_mask.sum())
        symbols, truncated, membership_complete = _unmapped_symbols(frame, ~mapped_mask)

        results.append(
            UniverseHealth(
                universe_key=universe_key,
                total_rows=total_rows,
                mapped_rows=mapped_rows,
                unmapped_symbols=symbols,
                observation_status="valid",
                unmapped_symbols_truncated=truncated,
                membership_complete=membership_complete,
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
            level=(logging.INFO if snapshot.observation_status == "valid" else logging.WARNING),
            universe_key=snapshot.universe_key,
            rows=snapshot.total_rows,
            mapped=snapshot.mapped_rows,
            unmapped=snapshot.unmapped_rows,
            observation_status=snapshot.observation_status,
            unmapped_symbols_truncated=snapshot.unmapped_symbols_truncated,
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
        if snapshot.observation_status != "valid":
            continue
        baseline = previous.get(snapshot.universe_key)
        if baseline is None:
            continue
        previous_unmapped = int(getattr(baseline, "unmapped_rows", 0) or 0)
        if snapshot.unmapped_rows <= previous_unmapped:
            continue

        stored = getattr(baseline, "unmapped_symbols_json", None) or {}
        memberships_complete = (
            snapshot.membership_complete
            and stored.get("membership_complete") is True
            and stored.get("truncated") is False
        )
        known = {str(value) for value in stored.get("symbols", [])}
        newly = (
            tuple(symbol for symbol in snapshot.unmapped_symbols if symbol not in known)
            if memberships_complete
            else ()
        )
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
                "unmapped_symbols_truncated": snapshot.unmapped_symbols_truncated,
                "membership_complete": snapshot.membership_complete,
                "observation_status": snapshot.observation_status,
            }
            for snapshot in snapshots
        ],
    )
    return UniverseHealthReport(snapshots=snapshots, regressions=regressions)
