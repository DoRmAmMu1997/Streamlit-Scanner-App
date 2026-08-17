"""Carrying out candle-cache repairs against the on-disk parquet files (DATA-002).

Beginner note:
``repair.py`` decides *what* would fix a dirty candle file. This module does it:
reads the cached parquet, re-downloads what the plan asks for, applies the
offline fixes, **re-validates its own work**, and writes the file back — but only
when something genuinely changed, and only through an atomic rename so an
interrupted run can never leave half a file behind.

The two habits worth copying if you extend this:

- **Verify, don't assume.** Every repair path ends with another
  ``validate_candles`` call, and whatever findings survive are reported as
  ``after_codes``. A repair that did not work says so.
- **Cost the vendor once.** A symbol that stays dirty even after a full
  re-download gets a ``.repaired`` sidecar so the next app launch skips it
  instead of re-downloading ten years of history every morning.

Layering note: ``DailyDataLoader`` is imported under ``TYPE_CHECKING`` only and
passed in as a parameter, so ``backend.data_quality`` never grows a runtime
dependency on the loader that already imports it.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pandas as pd

from backend.data_quality.candles import CandleQualityReport, validate_candles
from backend.data_quality.repair import (
    ACTION_NO_ACTION_VENDOR_DATA,
    ACTION_REFETCH_FULL,
    REPAIR_RETRY_AFTER_DAYS,
    RepairAction,
    apply_frame_actions,
    exceeds_drop_budget,
    parsed_candle_dates,
    plan_repair,
)
from backend.observability import (
    EVENT_CANDLE_CACHE_REPAIR_FAILED,
    EVENT_CANDLE_CACHE_SYMBOL_REPAIRED,
    log_event,
)
from backend.security import redact_text

if TYPE_CHECKING:  # pragma: no cover - import for type checking only
    from backend.daily_data_loader import DailyDataLoader

logger = logging.getLogger(__name__)

#: Sidecar remembering the last repair attempt for a symbol. Deliberately a
#: different suffix from the loader's existing ``.checked`` marker so the two
#: never overwrite each other.
REPAIR_SIDECAR_SUFFIX = ".repaired"

#: Temporary file used for the atomic write. Named so a crash leaves an obvious
#: artefact rather than a plausible-looking cache file.
_TEMP_SUFFIX = ".repair.tmp"

#: How many per-symbol outcomes the persisted receipt keeps. The aggregate counts
#: always describe the whole run; this only bounds the detail sample, exactly
#: like ``MAX_PERSISTED_FINDINGS`` does for the DATA-001 quality receipt.
MAX_RECEIPT_OUTCOMES = 25

#: How many symbols in a row may fail their Dhan re-download before the pass gives
#: up on the vendor entirely. Sized small because the failure this guards against —
#: an expired access token — fails identically for every symbol, so a handful of
#: attempts is ample evidence.
MAX_CONSECUTIVE_REFETCH_FAILURES = 5

#: Prefetch statuses that prove the top-up already reached Dhan for this symbol.
#: When one of these is present, a lingering ``STALE_LATEST_CANDLE`` is vendor
#: reality, not something a second request would fix.
_SUCCESSFUL_PREFETCH_STATUSES = frozenset(
    {"fresh", "incremental", "fresh_download", "backfilled"}
)

#: Outcome statuses, most-actionable first. The receipt sample keeps this order
#: so a truncated list still shows an operator the things that need attention.
_STATUS_PRIORITY = (
    "failed",
    "unrepairable",
    "partially_repaired",
    "repaired",
    "vendor_data",
    "skipped",
    "clean",
)


@dataclass(frozen=True)
class SymbolRepairOutcome:
    """What happened to one symbol's cache file.

    ``status`` is one of:

    - ``clean`` — nothing was wrong.
    - ``repaired`` — it was dirty and now validates cleanly.
    - ``partially_repaired`` — some findings resolved, others remain.
    - ``vendor_data`` — the findings are ones we deliberately never auto-fix
      (a possible unadjusted split, or staleness the vendor cannot improve).
    - ``unrepairable`` — the repair did not help, or was refused by the drop
      budget. The file is left exactly as it was.
    - ``skipped`` — no cache file, or a recent attempt already failed.
    - ``failed`` — an unexpected error; the file is left exactly as it was.
    """

    symbol: str
    status: str
    before_codes: tuple[str, ...] = ()
    after_codes: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()
    rows_before: int = 0
    rows_after: int = 0
    dates_before: int = 0
    dates_after: int = 0
    refetch_count: int = 0
    message: str | None = None
    #: True when a Dhan call raised. Drives the pass-level circuit breaker below.
    refetch_failed: bool = False

    @property
    def rows_removed(self) -> int:
        """How many rows the repair discarded (never negative)."""
        return max(0, self.rows_before - self.rows_after)

    @property
    def changed(self) -> bool:
        """True when the repair actually rewrote the cache file."""
        return self.status in {"repaired", "partially_repaired"}

    def as_receipt_entry(self) -> dict[str, Any]:
        """A small JSON-safe dict for the persisted receipt.

        Carries codes, counts, and the symbol only — never a price, never a raw
        exception string (``message`` is redacted at construction).
        """
        return {
            "symbol": self.symbol,
            "status": self.status,
            "before_codes": list(self.before_codes),
            "after_codes": list(self.after_codes),
            "actions": list(self.actions),
            "rows_before": self.rows_before,
            "rows_after": self.rows_after,
            "refetch_count": self.refetch_count,
            "message": self.message,
        }


@dataclass(frozen=True)
class CacheRepairSummary:
    """Aggregate result of one repair pass over a universe."""

    symbols_checked: int = 0
    symbols_clean: int = 0
    symbols_repaired: int = 0
    symbols_partially_repaired: int = 0
    symbols_vendor_data: int = 0
    symbols_unrepairable: int = 0
    symbols_skipped: int = 0
    symbols_failed: int = 0
    rows_removed: int = 0
    refetch_count: int = 0
    outcomes: tuple[SymbolRepairOutcome, ...] = ()

    @property
    def changed_outcomes(self) -> tuple[SymbolRepairOutcome, ...]:
        """Only the symbols whose file was actually rewritten."""
        return tuple(outcome for outcome in self.outcomes if outcome.changed)

    @property
    def needs_attention(self) -> tuple[SymbolRepairOutcome, ...]:
        """Symbols an operator should look at (still dirty or errored)."""
        return tuple(
            outcome
            for outcome in self.outcomes
            if outcome.status in {"unrepairable", "partially_repaired", "failed"}
        )

    def as_receipt(self) -> dict[str, Any]:
        """Build the bounded, JSON-safe receipt persisted with the run.

        The counts always describe the entire pass; only the ``outcomes`` detail
        list is capped, most-actionable first, with ``outcomes_truncated``
        flagging the omission so nobody mistakes the sample for the whole story.
        """
        ranked = sorted(
            self.outcomes,
            key=lambda outcome: (
                _STATUS_PRIORITY.index(outcome.status)
                if outcome.status in _STATUS_PRIORITY
                else len(_STATUS_PRIORITY),
                outcome.symbol,
            ),
        )
        # "clean" symbols are the overwhelming majority and say nothing useful in
        # a detail table; the counts above already cover them.
        interesting = [outcome for outcome in ranked if outcome.status != "clean"]
        sample = interesting[:MAX_RECEIPT_OUTCOMES]
        return {
            "schema_version": 1,
            "symbols_checked": self.symbols_checked,
            "symbols_clean": self.symbols_clean,
            "symbols_repaired": self.symbols_repaired,
            "symbols_partially_repaired": self.symbols_partially_repaired,
            "symbols_vendor_data": self.symbols_vendor_data,
            "symbols_unrepairable": self.symbols_unrepairable,
            "symbols_skipped": self.symbols_skipped,
            "symbols_failed": self.symbols_failed,
            "rows_removed": self.rows_removed,
            "refetch_count": self.refetch_count,
            "total_outcomes": len(interesting),
            "outcomes_truncated": len(interesting) > len(sample),
            "outcomes": [outcome.as_receipt_entry() for outcome in sample],
        }


@dataclass
class _RepairTally:
    """Mutable accumulator used while streaming through a universe."""

    outcomes: list[SymbolRepairOutcome] = field(default_factory=list)

    def add(self, outcome: SymbolRepairOutcome) -> None:
        self.outcomes.append(outcome)

    def build(self) -> CacheRepairSummary:
        counts: dict[str, int] = {}
        for outcome in self.outcomes:
            counts[outcome.status] = counts.get(outcome.status, 0) + 1
        return CacheRepairSummary(
            symbols_checked=len(self.outcomes),
            symbols_clean=counts.get("clean", 0),
            symbols_repaired=counts.get("repaired", 0),
            symbols_partially_repaired=counts.get("partially_repaired", 0),
            symbols_vendor_data=counts.get("vendor_data", 0),
            symbols_unrepairable=counts.get("unrepairable", 0),
            symbols_skipped=counts.get("skipped", 0),
            symbols_failed=counts.get("failed", 0),
            rows_removed=sum(outcome.rows_removed for outcome in self.outcomes),
            refetch_count=sum(outcome.refetch_count for outcome in self.outcomes),
            outcomes=tuple(self.outcomes),
        )


ProgressCallback = Callable[[int, int, str], None]


def repair_symbol(
    loader: DailyDataLoader,
    instrument: Mapping[str, object],
    *,
    today: date,
    years_back: int | None = None,
    allow_stale_refetch: bool = True,
    force: bool = False,
    dry_run: bool = False,
    allow_refetch: bool = True,
) -> SymbolRepairOutcome:
    """Validate, repair, and re-validate one symbol's cached candle file.

    Args:
        loader: supplies the cache path and the no-write ``fetch_window`` helper.
        instrument: a universe row (needs at least ``symbol`` + ``security_id``).
        today: the newest date worth asking the vendor for.
        years_back: history window length; defaults to the loader's own default.
        allow_stale_refetch: see ``plan_repair`` — ``False`` when the prefetch
            already topped this symbol up successfully.
        force: ignore the ``.repaired`` sidecar and retry regardless.
        dry_run: compute and report the repair without writing anything.

    Never raises for a bad file: every failure becomes a ``failed`` outcome with
    a redacted message, because one unreadable parquet must not abort a
    600-symbol pass.
    """
    from backend.daily_data_loader import DEFAULT_HISTORY_YEARS_BACK, history_start_date

    row = dict(instrument)
    symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
    security_id = str(row.get("security_id", "")).strip()
    if not security_id:
        return SymbolRepairOutcome(
            symbol=symbol, status="skipped", message="row has no security_id"
        )

    path = loader.cache_path(symbol, security_id)
    if not path.exists():
        # Nothing cached yet is not a defect — the prefetch downloads it.
        return SymbolRepairOutcome(
            symbol=symbol, status="skipped", message="no cached candle file"
        )

    history_start = history_start_date(
        int(years_back if years_back is not None else DEFAULT_HISTORY_YEARS_BACK), today
    )

    try:
        cached = pd.read_parquet(path)
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop the pass
        return _failure(symbol, exc, "could not read the cached parquet")

    report = validate_candles(cached, symbol=symbol, expected_latest_date=today)
    before_codes = _codes(report)
    rows_before = len(cached.index)
    dates_before = _distinct_date_count(cached)
    if not report.findings:
        return SymbolRepairOutcome(
            symbol=symbol,
            status="clean",
            rows_before=rows_before,
            rows_after=rows_before,
            dates_before=dates_before,
            dates_after=dates_before,
        )

    plan = plan_repair(
        cached,
        report,
        today=today,
        history_start=history_start,
        allow_stale_refetch=allow_stale_refetch,
    )
    actionable = [
        action for action in plan.actions if action.code != ACTION_NO_ACTION_VENDOR_DATA
    ]
    if not actionable:
        # Every finding is one we deliberately leave alone. Not a failure.
        return SymbolRepairOutcome(
            symbol=symbol,
            status="vendor_data",
            before_codes=before_codes,
            after_codes=before_codes,
            rows_before=rows_before,
            rows_after=rows_before,
            dates_before=dates_before,
            dates_after=dates_before,
            message=_vendor_reason(plan.actions),
        )

    # A loader built without credentials can still de-duplicate and drop bad rows,
    # but it cannot ask the vendor anything. Say that once, plainly, instead of
    # attempting a fetch per symbol and reporting a wall of identical errors.
    can_refetch = allow_refetch and getattr(loader, "client", None) is not None
    if plan.needs_refetch and not can_refetch and not _has_offline_work(plan):
        return SymbolRepairOutcome(
            symbol=symbol,
            status="skipped",
            before_codes=before_codes,
            after_codes=before_codes,
            rows_before=rows_before,
            rows_after=rows_before,
            dates_before=dates_before,
            dates_after=dates_before,
            message="repair needs Dhan credentials",
        )

    sidecar = path.with_suffix(REPAIR_SIDECAR_SUFFIX)
    if (
        not force
        and plan.needs_refetch
        and can_refetch
        and _recently_attempted(sidecar, today, before_codes)
    ):
        return SymbolRepairOutcome(
            symbol=symbol,
            status="skipped",
            before_codes=before_codes,
            after_codes=before_codes,
            rows_before=rows_before,
            rows_after=rows_before,
            dates_before=dates_before,
            dates_after=dates_before,
            message="a recent repair attempt left the same findings",
        )

    try:
        working, actions, refetch_count, fetch_error = _run_plan(
            loader,
            row,
            cached,
            plan,
            today=today,
            history_start=history_start,
            can_refetch=can_refetch,
        )
    except Exception as exc:  # noqa: BLE001 - defensive, same reason as above
        return _failure(symbol, exc, "repair aborted", before_codes=before_codes)

    after_report = validate_candles(working, symbol=symbol, expected_latest_date=today)
    after_codes = _codes(after_report)
    rows_after = len(working.index)
    dates_after = _distinct_date_count(working)

    # The "abort rather than mangle" guard. Refusing here leaves a known-dirty
    # file an operator can inspect, which beats a silently hollowed-out one.
    if exceeds_drop_budget(dates_before=dates_before, dates_after=dates_after):
        return SymbolRepairOutcome(
            symbol=symbol,
            status="unrepairable",
            before_codes=before_codes,
            after_codes=before_codes,
            rows_before=rows_before,
            rows_after=rows_before,
            dates_before=dates_before,
            dates_after=dates_before,
            refetch_count=refetch_count,
            message=(
                f"refused: repair exceeds the drop budget "
                f"({dates_before - dates_after} of {dates_before} trading days)"
            ),
        )

    # Only ever rewrite the cache when the repair genuinely made the data better.
    # A frame that merely got re-sorted while staying just as broken is churn: it
    # bumps the file's mtime (invalidating chart caches) and buys nothing. This
    # also makes "unrepairable means untouched" a real, testable invariant.
    changed = bool(actions) and not _frames_equal(cached, working)
    improved = changed and _is_improvement(report, after_report)
    if improved and not dry_run:
        try:
            _atomic_write_parquet(working, path)
        except Exception as exc:  # noqa: BLE001 - report, never corrupt
            return _failure(
                symbol, exc, "could not write the repaired parquet", before_codes=before_codes
            )

    status: str
    message: str | None
    if not improved:
        # Nothing got better. Say so plainly rather than claiming a repair, and
        # report the file's real (unchanged) shape.
        status = "unrepairable"
        message = fetch_error or "no repair could be applied"
        after_codes = before_codes
        rows_after, dates_after = rows_before, dates_before
    else:
        status = "partially_repaired" if after_report.findings else "repaired"
        message = fetch_error

    if not dry_run:
        _record_attempt(sidecar, today, after_codes, refetched=refetch_count > 0)

    outcome = SymbolRepairOutcome(
        symbol=symbol,
        status=status,
        before_codes=before_codes,
        after_codes=after_codes,
        actions=tuple(actions) if improved else (),
        rows_before=rows_before,
        rows_after=rows_after,
        dates_before=dates_before,
        dates_after=dates_after,
        refetch_count=refetch_count,
        message=message,
        refetch_failed=fetch_error is not None and refetch_count > 0,
    )
    _log_outcome(outcome, security_id=security_id)
    return outcome


def repair_universe(
    loader: DailyDataLoader,
    rows: Sequence[Mapping[str, object]],
    *,
    today: date,
    years_back: int | None = None,
    prefetch_statuses: Mapping[str, str] | None = None,
    force: bool = False,
    dry_run: bool = False,
    progress_callback: ProgressCallback | None = None,
) -> CacheRepairSummary:
    """Repair every symbol in ``rows`` and summarise the pass.

    ``prefetch_statuses`` maps symbol → the status the prefetch top-up returned
    for it. It exists purely to suppress pointless staleness refetches: if the
    top-up already asked Dhan and got nothing new, asking again on the very next
    line of the same script would waste a request per symbol — 577 of them on a
    full universe. Only symbols whose top-up *failed* (or that the caller knows
    nothing about) are allowed a stale refetch.
    """
    tally = _RepairTally()
    total = len(rows)
    statuses = {
        str(key).strip().upper(): str(value) for key, value in (prefetch_statuses or {}).items()
    }
    # Circuit breaker. An expired Dhan token fails identically for every symbol, so
    # without this a single bad credential would spend one pointless request per
    # symbol — 570 of them — on every app launch. After a short run of failures we
    # stop asking and finish the pass with offline repairs only.
    consecutive_refetch_failures = 0
    refetch_disabled = False

    for index, row in enumerate(rows, start=1):
        symbol = str(row.get("symbol", "")).strip().upper() or "UNKNOWN"
        prefetch_status = statuses.get(symbol)
        if prefetch_status is not None:
            allow_stale_refetch = prefetch_status not in _SUCCESSFUL_PREFETCH_STATUSES
        else:
            # Standalone runs have no prefetch statuses to consult, so fall back to
            # the loader's own ``.checked`` marker: it records the last date a
            # top-up asked Dhan for this symbol's tail and got nothing back. If
            # that already covers today, the staleness is the vendor's, not ours.
            allow_stale_refetch = not _tail_already_checked(loader, row, today)
        outcome = repair_symbol(
            loader,
            row,
            today=today,
            years_back=years_back,
            allow_stale_refetch=allow_stale_refetch,
            force=force,
            dry_run=dry_run,
            allow_refetch=not refetch_disabled,
        )
        tally.add(outcome)

        if outcome.refetch_failed:
            consecutive_refetch_failures += 1
            if consecutive_refetch_failures >= MAX_CONSECUTIVE_REFETCH_FAILURES:
                refetch_disabled = True
                logger.warning(
                    "Disabling candle repair refetches after %d consecutive Dhan "
                    "failures; the remaining symbols get offline repairs only.",
                    consecutive_refetch_failures,
                )
        elif outcome.refetch_count:
            consecutive_refetch_failures = 0

        _notify_progress(progress_callback, index, total, symbol)

    return tally.build()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _run_plan(
    loader: DailyDataLoader,
    row: dict[str, object],
    cached: pd.DataFrame,
    plan: Any,
    *,
    today: date,
    history_start: date,
    can_refetch: bool = True,
) -> tuple[pd.DataFrame, list[str], int, str | None]:
    """Execute a plan: refetch first, then the offline fixes.

    Returns ``(frame, applied_action_codes, refetch_count, fetch_error)``. The
    refetch half runs first on purpose — dropping a bar is only ever a fallback
    for one the vendor could not re-supply.
    """
    working = cached
    actions: list[str] = []
    refetch_count = 0
    fetch_error: str | None = None

    refetch_actions = plan.refetch_actions() if can_refetch else ()
    if plan.needs_refetch and not can_refetch:
        fetch_error = "no Dhan credentials; offline repairs only"

    for action in refetch_actions:
        # Count the attempt, not the success: an attempt is what actually costs a
        # Dhan request, and a summary reporting "0 refetches" beside a page of
        # fetch failures would be actively misleading.
        refetch_count += 1
        try:
            fetched = loader.fetch_window(row, action.window_start, action.window_end)
        except Exception as exc:  # noqa: BLE001 - a dead fetch is not a crash
            # Record it and carry on with the offline fixes; something is often
            # still repairable without the vendor.
            fetch_error = redact_text(f"refetch failed: {exc}")
            continue

        if fetched.empty:
            # The vendor genuinely has nothing here. For a gap probe that *proves*
            # the gap is real history rather than dropped data.
            continue

        working = (
            fetched
            if action.code == ACTION_REFETCH_FULL
            else _merge_window(working, fetched, action)
        )
        actions.append(action.code)

    # Re-plan against the (possibly refreshed) frame so a defect the vendor just
    # fixed is not "repaired" a second time, and only the offline steps remain.
    interim_report = validate_candles(
        working, symbol=plan.symbol, expected_latest_date=today
    )
    interim_plan = plan_repair(
        working,
        interim_report,
        today=today,
        history_start=history_start,
        allow_stale_refetch=False,
    )
    working, applied = apply_frame_actions(
        working,
        interim_plan,
        # Only now — after the vendor has had its chance — may the volume-only
        # duplicate tiebreak run.
        allow_conflicting_fallback=True,
    )
    actions.extend(applied)
    return working, actions, refetch_count, fetch_error


def _merge_window(
    cached: pd.DataFrame, fetched: pd.DataFrame, action: RepairAction
) -> pd.DataFrame:
    """Overlay a freshly fetched window on the cached history.

    Rows inside the window are replaced wholesale by the vendor's answer; every
    row outside it is kept untouched. This is the difference between repairing a
    file and truncating it to the repaired window.
    """
    parsed = parsed_candle_dates(cached)
    if parsed is None or action.window_start is None or action.window_end is None:
        return fetched

    inside = parsed.between(action.window_start, action.window_end).fillna(False)
    kept = cached.loc[~inside.to_numpy()]
    merged = pd.concat([kept, fetched], ignore_index=True)
    # Preserve the cached column order so the parquet schema stays stable.
    columns = [column for column in cached.columns if column in merged.columns]
    extra = [column for column in merged.columns if column not in columns]
    return merged[columns + extra]


def _atomic_write_parquet(frame: pd.DataFrame, path: Path) -> None:
    """Write ``frame`` to ``path`` so an interrupted run cannot corrupt the cache.

    The parquet goes to a sibling temp file first; ``os.replace`` then swaps it
    into place in a single filesystem operation. If anything fails the original
    file is still whole and the temp file is removed.
    """
    temp_path = path.with_suffix(_TEMP_SUFFIX)
    try:
        frame.to_parquet(temp_path, index=False)
        os.replace(temp_path, path)
    finally:
        # ``missing_ok`` keeps the happy path (already renamed away) quiet.
        temp_path.unlink(missing_ok=True)


def _recently_attempted(
    sidecar: Path, today: date, before_codes: tuple[str, ...]
) -> bool:
    """True when a recent attempt already failed on exactly these findings.

    Without this, a symbol whose dirt lives in the vendor's own data would
    re-download its entire history on every single app launch, forever.
    """
    if not sidecar.exists():
        return False
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
        attempted_on = date.fromisoformat(str(payload["attempted_on"]))
        residual = tuple(payload.get("residual_codes") or ())
    except (OSError, ValueError, KeyError, TypeError):
        # An unreadable marker must never block a repair.
        return False
    if (today - attempted_on) >= timedelta(days=REPAIR_RETRY_AFTER_DAYS):
        return False
    # Different findings than last time means something changed; try again.
    return tuple(residual) == before_codes


def _record_attempt(
    sidecar: Path, today: date, residual_codes: tuple[str, ...], *, refetched: bool
) -> None:
    """Remember (or clear) the "we already tried this" marker.

    Only written when the attempt cost a Dhan round-trip *and* left findings
    behind — the two conditions that make a retry expensive and pointless.
    """
    try:
        if not residual_codes or not refetched:
            sidecar.unlink(missing_ok=True)
            return
        sidecar.write_text(
            json.dumps(
                {"attempted_on": today.isoformat(), "residual_codes": list(residual_codes)}
            ),
            encoding="utf-8",
        )
    except OSError:
        # The marker is an optimisation, never a correctness requirement.
        logger.warning("Could not update the candle repair marker at %s", sidecar)


def _has_offline_work(plan: Any) -> bool:
    """True when the plan contains a step that needs no network."""
    from backend.data_quality.repair import FRAME_ACTION_CODES

    return any(action.code in FRAME_ACTION_CODES for action in plan.actions)


def _tail_already_checked(
    loader: DailyDataLoader, row: Mapping[str, object], today: date
) -> bool:
    """True when a top-up already asked Dhan for this symbol's tail through today.

    Reads the loader's existing ``.checked`` sidecar — the marker
    ``ensure_daily_history`` writes when an incremental request comes back empty
    (weekend, holiday, pre-open). If it covers today, re-requesting the tail would
    buy nothing and cost one Dhan call per symbol.

    Any problem reading it means "not checked", so an unreadable marker can only
    ever cause an extra request, never a missed repair.
    """
    try:
        symbol = str(row.get("symbol", "")).strip().upper()
        security_id = str(row.get("security_id", "")).strip()
        if not symbol or not security_id:
            return False
        marker = loader.checked_path(symbol, security_id)
        if not marker.exists():
            return False
        return date.fromisoformat(marker.read_text(encoding="utf-8").strip()) >= today
    except (OSError, ValueError, AttributeError):
        return False


def _codes(report: CandleQualityReport) -> tuple[str, ...]:
    """The distinct finding codes of a report, in a stable order."""
    return tuple(sorted({finding.code for finding in report.findings}))


def _distinct_date_count(frame: pd.DataFrame) -> int:
    """How many distinct trading days the frame covers (the drop-budget unit)."""
    parsed = parsed_candle_dates(frame)
    if parsed is None:
        return 0
    return len(set(parsed.dropna()))


def _is_improvement(before: CandleQualityReport, after: CandleQualityReport) -> bool:
    """True when the repaired frame is genuinely healthier than the original.

    Judged on *fatal* findings first, because those are what stop a symbol being
    scanned at all. Note that a legitimate repair can trade a fatal finding for a
    warning — dropping one impossible bar leaves a one-day hole, which may raise
    ``CALENDAR_DATE_GAP`` — so a plain "fewer codes than before" test would wrongly
    reject it.
    """
    if not after.findings:
        return True

    fatal_before = sum(1 for finding in before.findings if finding.severity == "fatal")
    fatal_after = sum(1 for finding in after.findings if finding.severity == "fatal")
    if fatal_after < fatal_before:
        return True
    if fatal_after > fatal_before:
        # Never accept a "repair" that introduced a new structural problem.
        return False
    # No change in fatal status: only count it as progress if warnings shrank.
    return fatal_after == 0 and len(after.findings) < len(before.findings)


def _frames_equal(left: pd.DataFrame, right: pd.DataFrame) -> bool:
    """True when a repair produced a byte-for-byte equivalent frame."""
    if left.shape != right.shape:
        return False
    try:
        return left.reset_index(drop=True).equals(right.reset_index(drop=True))
    except Exception:  # noqa: BLE001 - comparison must never raise
        return False


def _vendor_reason(actions: tuple[RepairAction, ...]) -> str | None:
    """The first "we left this alone on purpose" explanation, for the receipt."""
    for action in actions:
        if action.code == ACTION_NO_ACTION_VENDOR_DATA:
            return action.reason
    return "findings need no local repair"


def _failure(
    symbol: str,
    exc: Exception,
    context: str,
    *,
    before_codes: tuple[str, ...] = (),
) -> SymbolRepairOutcome:
    """Build a redacted failure outcome and log it."""
    message = redact_text(f"{context}: {exc}")
    log_event(
        logger,
        EVENT_CANDLE_CACHE_REPAIR_FAILED,
        level=logging.WARNING,
        symbol=symbol,
        error_type=type(exc).__name__,
        error=message,
    )
    return SymbolRepairOutcome(
        symbol=symbol,
        status="failed",
        before_codes=before_codes,
        after_codes=before_codes,
        message=message,
    )


def _log_outcome(outcome: SymbolRepairOutcome, *, security_id: str) -> None:
    """Emit the per-symbol structured event (codes and counts only, no prices)."""
    if outcome.status in {"clean", "skipped"}:
        return
    log_event(
        logger,
        EVENT_CANDLE_CACHE_SYMBOL_REPAIRED,
        level=logging.INFO if outcome.status == "repaired" else logging.WARNING,
        symbol=outcome.symbol,
        security_id=security_id,
        repair_status=outcome.status,
        before_codes=list(outcome.before_codes),
        after_codes=list(outcome.after_codes),
        actions=list(outcome.actions),
        rows_removed=outcome.rows_removed,
        refetch_count=outcome.refetch_count,
    )


def _notify_progress(
    progress_callback: ProgressCallback | None, index: int, total: int, symbol: str
) -> None:
    """Call the progress callback without letting a UI error break the pass."""
    if progress_callback is None:
        return
    try:
        progress_callback(index, total, symbol)
    except Exception:  # noqa: BLE001 - mirrors DailyDataLoader._notify_progress
        logger.exception("Repair progress callback raised for %s", symbol)
