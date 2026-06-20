"""JOB-003 read model for latest-vs-previous scan comparisons.

The scan history tables already store every finalized shortlist, so comparison
does not need a new table. This module derives a small immutable view model from
``scan_runs`` and ``scan_results`` for the Streamlit page and CSV export.

Beginner note:
A *read model* is code that only **reads** existing data and reshapes it into a
convenient view; it never writes anything back. Here we load the two most recent
finalized runs for one screener/universe pair, then sort their shortlisted
symbols into five buckets the UI can render directly:

- **New today** - in the latest run but not the previous one.
- **Repeated from yesterday** - in both runs.
- **Dropped today** - in the previous run but not the latest one.
- **Improved score** / **Degraded score** - repeated symbols whose score went
  up / down (only when both runs measured the score the same way).

Everything this module returns is a frozen dataclass (immutable, plain values),
so the Streamlit page can keep using it safely after the database session that
produced it has already closed. ``backend`` never imports Streamlit, which keeps
this logic framework-free and unit-testable.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from backend.storage.models import ScanResult, ScanRun
from backend.storage.repository import (
    get_latest_finalized_scan_runs,
    get_scan_results,
)

# The exact, ordered column set of the flattened CSV export. Declaring it once
# (instead of relying on dict insertion order) guarantees a stable export shape
# even if rows are empty, and lets a test assert the columns directly.
_EXPORT_COLUMNS = [
    "Change type",
    "Symbol",
    "Latest run",
    "Previous run",
    "Latest rating",
    "Previous rating",
    "Latest signal date",
    "Previous signal date",
    "Latest close",
    "Previous close",
    "Latest score",
    "Previous score",
    "Score source",
    "Score delta",
    "Latest reason",
    "Previous reason",
]


@dataclass(frozen=True)
class ComparisonRun:
    """Small run header safe to use after the database session closes.

    Beginner note:
    We copy just the handful of run fields the UI shows into plain strings/ints
    here rather than handing the live ``ScanRun`` ORM row to the page. Once the
    database session closes, touching an ORM row can raise ``DetachedInstanceError``;
    plain values never do.
    """

    run_id: int
    started: str  # UTC, preformatted for display ("YYYY-MM-DD HH:MM UTC")
    finished: str  # UTC, preformatted for display (empty string if still null)
    status: str  # e.g. "success" / "partial" (ScanStatus value)
    screener_key: str
    universe_key: str
    symbols_scanned: int | None  # how many symbols the run examined (may be None)
    shortlisted: int  # how many rows the run shortlisted (len of its results)


@dataclass(frozen=True)
class _ResultSnapshot:
    """One result row normalized for symbol-set comparison.

    Private helper type: we reduce each ``ScanResult`` to exactly the fields the
    comparison needs, with the symbol already trimmed/upper-cased so the same
    instrument matches across two runs.
    """

    symbol: str
    rating: str
    signal_date: str
    close: Decimal | None
    score: Decimal | None
    # Which field the score came from ("final_score" or "confidence"); a delta is
    # only computed when both runs used the *same* source (see ``_comparison_row``).
    score_source: str | None
    reason: str


@dataclass(frozen=True)
class ComparisonRow:
    """One symbol's latest/previous state for a comparison section.

    Every field defaults so a row can describe a symbol that exists on only one
    side: a "New today" row has empty/None ``previous_*`` fields, a "Dropped
    today" row has empty/None ``latest_*`` fields.
    """

    symbol: str
    latest_rating: str = ""
    previous_rating: str = ""
    latest_signal_date: str = ""
    previous_signal_date: str = ""
    latest_close: Decimal | None = None
    previous_close: Decimal | None = None
    latest_score: Decimal | None = None
    previous_score: Decimal | None = None
    score_source: str | None = None  # set only when latest & previous share a source
    score_delta: Decimal | None = None  # latest.score - previous.score, else None
    latest_reason: str = ""
    previous_reason: str = ""


@dataclass(frozen=True)
class ScanComparison:
    """Derived latest-vs-previous comparison for one screener/universe pair.

    ``previous_run`` is ``None`` (and every section is empty) when only one
    finalized run exists yet - the UI shows the latest run's context and a
    "need at least two runs" notice in that case.
    """

    latest_run: ComparisonRun
    previous_run: ComparisonRun | None
    new_today: Sequence[ComparisonRow] = ()
    repeated_from_yesterday: Sequence[ComparisonRow] = ()
    dropped_today: Sequence[ComparisonRow] = ()
    improved_score: Sequence[ComparisonRow] = ()
    degraded_score: Sequence[ComparisonRow] = ()

    def to_export_frame(self) -> pd.DataFrame:
        """Flatten all comparison sections into one stable CSV-ready frame.

        Beginner note:
        The on-screen page shows five separate tables, but a single CSV is easier
        to download and analyze. We stack every section's rows into one list,
        tagging each with a "Change type" column so the section is still obvious,
        then build a DataFrame with the fixed ``_EXPORT_COLUMNS`` order. When
        there are no rows the frame is empty but still carries those columns.
        """
        rows: list[dict[str, object]] = []
        for section, section_rows in (
            ("New today", self.new_today),
            ("Repeated from yesterday", self.repeated_from_yesterday),
            ("Dropped today", self.dropped_today),
            ("Improved score", self.improved_score),
            ("Degraded score", self.degraded_score),
        ):
            # ``previous_run`` is None only when there is a single finalized run,
            # in which case every section above is empty and this never runs.
            rows.extend(
                _export_row(
                    section,
                    row,
                    latest_run_id=self.latest_run.run_id,
                    previous_run_id=(
                        self.previous_run.run_id if self.previous_run else None
                    ),
                )
                for row in section_rows
            )
        return pd.DataFrame(rows, columns=_EXPORT_COLUMNS)


def build_scan_comparison(
    session: Session,
    *,
    screener_key: str,
    universe_key: str,
) -> ScanComparison:
    """Compare the newest finalized run with the previous finalized run.

    "Today" and "yesterday" are product labels for latest and previous finalized
    runs; they are not strict calendar-day filters. That keeps manual UI scans
    and scheduled daily scans comparable through the same read model.

    Args:
        session: An open SQLAlchemy session (the caller owns its lifecycle).
        screener_key: Which screener's history to compare (e.g. "envelope").
        universe_key: Which universe's history to compare (e.g. "nifty_500").

    Returns:
        A :class:`ScanComparison`. ``previous_run`` is ``None`` when only one
        finalized run exists for the pair.

    Raises:
        ValueError: when the pair has **no** finalized run at all. The UI lists
            only pairs with history, so this is a rare time-of-check/time-of-use
            edge (the runs vanished after the page loaded).
    """
    # Pull at most the two newest SUCCESS/PARTIAL runs (newest first). RUNNING and
    # FAILED runs are excluded by the repository helper - see its docstring.
    runs = get_latest_finalized_scan_runs(
        session,
        screener_key=screener_key,
        universe_key=universe_key,
        limit=2,
    )
    if not runs:
        raise ValueError(
            f"No finalized scan runs found for {screener_key}/{universe_key}."
        )

    # The newest run is always "latest". Index its results by symbol so we can do
    # fast set math against the previous run below.
    latest_run = runs[0]
    latest_results = get_scan_results(session, latest_run.id)
    latest = _result_index(latest_results)
    latest_summary = _run_summary(latest_run, len(latest_results))

    # Only one finalized run so far: there is nothing to compare against yet.
    if len(runs) == 1:
        return ScanComparison(latest_run=latest_summary, previous_run=None)

    previous_run = runs[1]
    previous_results = get_scan_results(session, previous_run.id)
    previous = _result_index(previous_results)
    previous_summary = _run_summary(previous_run, len(previous_results))

    # Set operations on the two symbol key-sets drive the three membership
    # buckets. ``sorted(...)`` makes the output deterministic (handy for tests
    # and for a stable on-screen/CSV order).
    latest_symbols = set(latest)
    previous_symbols = set(previous)

    # In latest only -> appeared today.
    new_rows = tuple(
        _comparison_row(symbol, latest=latest[symbol], previous=None)
        for symbol in sorted(latest_symbols - previous_symbols)
    )
    # In both -> carried over; this is also the only set that can have a score delta.
    repeated_rows = tuple(
        _comparison_row(symbol, latest=latest[symbol], previous=previous[symbol])
        for symbol in sorted(latest_symbols & previous_symbols)
    )
    # In previous only -> fell off today.
    dropped_rows = tuple(
        _comparison_row(symbol, latest=None, previous=previous[symbol])
        for symbol in sorted(previous_symbols - latest_symbols)
    )

    # Split the repeated symbols by score movement. ``score_delta`` is None when
    # the two scores cannot be compared (missing, or measured by different
    # sources); those symbols stay in "repeated" only.
    improved_rows: list[ComparisonRow] = []
    degraded_rows: list[ComparisonRow] = []
    for row in repeated_rows:
        if row.score_delta is None:
            continue
        if row.score_delta > 0:
            improved_rows.append(row)
        elif row.score_delta < 0:
            degraded_rows.append(row)

    return ScanComparison(
        latest_run=latest_summary,
        previous_run=previous_summary,
        new_today=new_rows,
        repeated_from_yesterday=repeated_rows,
        dropped_today=dropped_rows,
        improved_score=tuple(improved_rows),
        degraded_score=tuple(degraded_rows),
    )


def _run_summary(run: ScanRun, shortlisted: int) -> ComparisonRun:
    """Copy the display-relevant fields of an ORM ``ScanRun`` into a plain header.

    ``shortlisted`` is passed in (rather than read from ``run.results``) because
    the caller already counted the results it loaded, and lazy-loading the
    relationship here could fail once the session closes.
    """
    return ComparisonRun(
        run_id=int(run.id),
        started=_format_utc_timestamp(run.started_at),
        finished=_format_utc_timestamp(run.finished_at),
        status=run.status.value,
        screener_key=run.screener_key,
        universe_key=run.universe_key,
        symbols_scanned=run.symbols_scanned,
        shortlisted=int(shortlisted),
    )


def _result_index(results: Sequence[ScanResult]) -> dict[str, _ResultSnapshot]:
    """Index result rows by a normalized symbol for fast set comparison.

    Symbols are trimmed and upper-cased so the same instrument from two runs
    matches deterministically. Blank symbols are skipped. If a run somehow lists
    a symbol twice, the last row wins (later rows overwrite the dict entry).
    """
    indexed: dict[str, _ResultSnapshot] = {}
    for result in results:
        symbol = str(result.symbol or "").strip().upper()
        if not symbol:
            continue
        indexed[symbol] = _snapshot(result, symbol=symbol)
    return indexed


def _snapshot(result: ScanResult, *, symbol: str) -> _ResultSnapshot:
    """Reduce one ``ScanResult`` ORM row to the immutable fields we compare.

    ``signal_date`` is rendered as an ISO string (or "") and the score is
    resolved via :func:`_result_score` so callers never re-derive either.
    """
    score, score_source = _result_score(result)
    return _ResultSnapshot(
        symbol=symbol,
        rating=result.rating or "",
        signal_date=result.signal_date.isoformat() if result.signal_date else "",
        close=result.close_price,
        score=score,
        score_source=score_source,
        reason=result.reason or "",
    )


def _result_score(result: ScanResult) -> tuple[Decimal | None, str | None]:
    """Resolve a single comparable score and label which field it came from.

    Preference order:
    1. ``final_score`` (the canonical ranking score) when present, labelled
       ``"final_score"``.
    2. otherwise a numeric ``confidence`` inside the screener's ``raw_result_json``
       (some AI screeners emit this), labelled ``"confidence"``.
    3. otherwise ``(None, None)`` - no comparable score.

    Returning the *source label* alongside the value lets the comparison refuse
    to subtract two scores that mean different things.
    """
    if result.final_score is not None:
        return result.final_score, "final_score"
    raw = result.raw_result_json
    if isinstance(raw, Mapping) and "confidence" in raw:
        score = _decimal_or_none(raw.get("confidence"))
        if score is not None:
            return score, "confidence"
    return None, None


def _decimal_or_none(value: Any) -> Decimal | None:
    """Best-effort parse of an arbitrary JSON value into a finite ``Decimal``.

    ``raw_result_json`` is free-form, so ``confidence`` could be a number, a
    numeric string, ``None``, or junk. We convert via ``str(value)`` (so 4 and
    "4" behave the same) and reject anything unparseable or non-finite
    (``NaN``/``inf``) by returning ``None``, which keeps such symbols out of the
    improved/degraded buckets.
    """
    if value is None:
        return None
    try:
        score = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return score if score.is_finite() else None


def _comparison_row(
    symbol: str,
    *,
    latest: _ResultSnapshot | None,
    previous: _ResultSnapshot | None,
) -> ComparisonRow:
    """Build one section row from a symbol's latest and/or previous snapshot.

    Either side may be ``None`` (the symbol is new or dropped). A score delta is
    computed only when **both** sides have a score **and** measured it via the
    same source - otherwise ``score_source``/``score_delta`` stay ``None`` and the
    symbol cannot land in the improved/degraded buckets.
    """
    score_source: str | None = None
    score_delta: Decimal | None = None
    if (
        latest is not None
        and previous is not None
        and latest.score is not None
        and previous.score is not None
        and latest.score_source == previous.score_source
    ):
        score_source = latest.score_source
        score_delta = latest.score - previous.score

    return ComparisonRow(
        symbol=symbol,
        latest_rating=latest.rating if latest else "",
        previous_rating=previous.rating if previous else "",
        latest_signal_date=latest.signal_date if latest else "",
        previous_signal_date=previous.signal_date if previous else "",
        latest_close=latest.close if latest else None,
        previous_close=previous.close if previous else None,
        latest_score=latest.score if latest else None,
        previous_score=previous.score if previous else None,
        score_source=score_source,
        score_delta=score_delta,
        latest_reason=latest.reason if latest else "",
        previous_reason=previous.reason if previous else "",
    )


def _export_row(
    section: str,
    row: ComparisonRow,
    *,
    latest_run_id: int,
    previous_run_id: int | None,
) -> dict[str, object]:
    """Flatten one ``ComparisonRow`` into a single CSV record for ``section``.

    The run-id columns are blanked where they would be misleading: a "Dropped
    today" symbol has no latest run, and a "New today" symbol has no previous
    run, so those cells are ``None`` for honesty in the export.
    """
    has_latest = section != "Dropped today"
    has_previous = section != "New today"
    return {
        "Change type": section,
        "Symbol": row.symbol,
        "Latest run": latest_run_id if has_latest else None,
        "Previous run": previous_run_id if has_previous else None,
        "Latest rating": row.latest_rating,
        "Previous rating": row.previous_rating,
        "Latest signal date": row.latest_signal_date,
        "Previous signal date": row.previous_signal_date,
        "Latest close": row.latest_close,
        "Previous close": row.previous_close,
        "Latest score": row.latest_score,
        "Previous score": row.previous_score,
        "Score source": row.score_source or "",
        "Score delta": row.score_delta,
        "Latest reason": row.latest_reason,
        "Previous reason": row.previous_reason,
    }


def _format_utc_timestamp(value: dt.datetime | None) -> str:
    """Render a run timestamp as a stable UTC display string, or "" if missing.

    SQLite hands datetimes back *naive* (no tzinfo) even though we store UTC, so
    a missing tzinfo is treated as UTC; timezone-aware values are converted to
    UTC. The fixed ``YYYY-MM-DD HH:MM UTC`` format keeps the UI independent of
    the host's locale.
    """
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC).strftime("%Y-%m-%d %H:%M UTC")
