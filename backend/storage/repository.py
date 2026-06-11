"""Repository helpers for persisted scan runs and results.

Beginner note:
A "repository" is a small layer that hides database query details from the rest
of the app. Future Streamlit or service code should call these functions instead
of building ``select(...)`` statements itself. That gives us one obvious place to
handle type conversion, JSON serialization, and ordering rules.

This file deliberately does not create sessions. The caller owns the transaction
using ``backend.storage.database.session_scope()`` or a test session. Keeping
session ownership outside the repository makes it easy for SCAN-003 to wrap
"create run -> run scanner -> save results -> finish run" in one transaction.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, cast

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from backend.storage.models import ScanResult, ScanRun, ScanStatus


def create_scan_run(
    session: Session,
    *,
    screener_key: str,
    universe_key: str,
    params: Mapping[str, Any] | None = None,
    data_snapshot_date: dt.date | None = None,
    app_version: str | None = None,
    git_commit_sha: str | None = None,
    triggered_by: str | None = None,
    symbols_scanned: int | None = None,
) -> ScanRun:
    """Insert a ``scan_runs`` header row in the RUNNING state.

    A scan run is the parent/audit header: it records which screener ran, which
    universe was scanned, which parameters were used, and who triggered it. The
    per-stock shortlist rows are added later with ``save_scan_results``.

    ``session.flush()`` sends the INSERT to the database so SQLAlchemy populates
    ``run.id``. It does not commit the transaction; the caller can still roll the
    whole scan back if something goes wrong.
    """
    run = ScanRun(
        started_at=dt.datetime.now(dt.UTC),
        status=ScanStatus.RUNNING,
        screener_key=screener_key,
        universe_key=universe_key,
        # Params may contain dates/Decimals in future screeners. Store a
        # JSON-safe copy, not the caller's original object.
        params_json=cast(dict[str, Any] | None, _json_safe(dict(params)) if params else None),
        data_snapshot_date=data_snapshot_date,
        app_version=app_version,
        git_commit_sha=git_commit_sha,
        triggered_by=triggered_by,
        # SCAN-004: universe size handed to the screener, shown on the history
        # page. None means the caller did not know (or predates this column).
        symbols_scanned=symbols_scanned,
    )
    session.add(run)
    session.flush()
    return run


def save_scan_results(
    session: Session,
    run: ScanRun,
    rows: Sequence[Mapping[str, Any]],
) -> list[ScanResult]:
    """Persist existing screener output dictionaries as ``scan_results`` rows.

    Current screeners return plain dictionaries, not ORM objects. This mapper
    copies the common fields into typed columns for queries and also stores the
    full original row in ``raw_result_json`` so no screener-specific detail is
    lost. That raw JSON blob is what lets one table support deterministic and AI
    screeners without making a table per strategy.
    """
    results: list[ScanResult] = []
    for row in rows:
        # Existing screeners use "close"; the database column is named
        # "close_price" so it reads clearly months later in history views. Accept
        # both keys to make future normalized rows easy to persist too.
        close_value = row.get("close")
        if _is_missing(close_value):
            close_value = row.get("close_price")

        # PROV-* tickets will eventually standardize this contract. For now we
        # accept both the database-oriented key and the shorter domain key.
        provenance_value = row.get("provenance_json")
        if provenance_value is None and "provenance" in row:
            provenance_value = row.get("provenance")

        result = ScanResult(
            symbol=str(row["symbol"]),
            signal_date=_as_date(row.get("signal_date")),
            close_price=_as_decimal(close_value),
            rating=_as_optional_str(row.get("rating")),
            final_score=_as_decimal(row.get("final_score")),
            reason=_as_optional_str(row.get("reason")),
            raw_result_json=cast(dict[str, Any], _json_safe(dict(row))),
            provenance_json=cast(
                dict[str, Any] | None,
                _json_safe(provenance_value) if provenance_value is not None else None,
            ),
        )
        results.append(result)

    # Extending the relationship fills each result's run_id for us. We flush so
    # tests and callers can inspect result ids before the outer transaction
    # commits.
    run.results.extend(results)
    session.flush()
    return results


def finish_scan_run(
    session: Session,
    run: ScanRun,
    *,
    status: ScanStatus,
    error_message: str | None = None,
) -> None:
    """Set the final scan status, finished timestamp, and optional error text.

    Use ``ScanStatus.SUCCESS`` when every symbol completed, ``PARTIAL`` when the
    scan produced usable rows but some symbols failed, and ``FAILED`` when the
    scan aborted. The free-text ``error_message`` gives the future history page a
    human-readable explanation.
    """
    run.status = status
    run.finished_at = dt.datetime.now(dt.UTC)
    run.error_message = error_message
    session.flush()


def get_latest_scan_runs(
    session: Session,
    limit: int = 50,
    *,
    screener_key: str | None = None,
    universe_key: str | None = None,
    status: ScanStatus | None = None,
    started_from: dt.date | None = None,
    started_to: dt.date | None = None,
    triggered_by: str | None = None,
    symbol: str | None = None,
) -> list[ScanRun]:
    """Return the newest scan headers first, optionally filtered.

    The SCAN-004 history page calls this for its runs table. The default limit
    keeps the query bounded even after the app has months of runs. Every filter
    is optional; ``None`` means "do not filter on this".

    Filter semantics:
    - ``screener_key``: exact match on the registry key.
    - ``universe_key``: exact match on the persisted universe key.
    - ``status``: exact match on the typed ``ScanStatus`` enum.
    - ``started_from`` / ``started_to``: inclusive calendar-day range applied to
      ``started_at``. The comparison binds whole datetimes (start of from-day,
      start of the day after to-day) rather than wrapping ``started_at`` in a SQL
      date() function. Bound datetimes compare correctly against the naive-UTC
      values SQLite stores and the aware values Postgres stores, and they leave
      the column usable by an index.
    - ``symbol``: keep only runs whose results contain this symbol. The match is
      case-insensitive but exact ("RELI" does not match RELIANCE) because ticker
      symbols are short codes, not prose. Implemented as an EXISTS subquery so
      result rows are never loaded just to answer a yes/no question.
    - ``triggered_by``: exact match on the audit identity (for example,
      ``job:daily_scan`` or ``ui:person@example.com``).

    Two runs created within the same millisecond (a daily job firing back-to-back,
    or fast tests) can share a ``started_at`` value. Adding the primary key as a
    tie-breaker keeps the newest-first order deterministic instead of leaving the
    database free to return same-timestamp rows in any order.
    """
    stmt = select(ScanRun)
    if screener_key:
        stmt = stmt.where(ScanRun.screener_key == screener_key)
    if universe_key:
        stmt = stmt.where(ScanRun.universe_key == universe_key)
    if status is not None:
        stmt = stmt.where(ScanRun.status == status)
    if started_from is not None:
        stmt = stmt.where(
            ScanRun.started_at >= dt.datetime.combine(started_from, dt.time.min, dt.UTC)
        )
    if started_to is not None:
        # Half-open upper bound: anything strictly before the next day's start.
        # This keeps the full to-day inclusive without timestamp edge cases.
        next_day = started_to + dt.timedelta(days=1)
        stmt = stmt.where(
            ScanRun.started_at < dt.datetime.combine(next_day, dt.time.min, dt.UTC)
        )
    if triggered_by:
        stmt = stmt.where(ScanRun.triggered_by == triggered_by)
    if symbol and symbol.strip():
        wanted = symbol.strip().upper()
        stmt = stmt.where(
            exists().where(
                ScanResult.run_id == ScanRun.id,
                func.upper(ScanResult.symbol) == wanted,
            )
        )
    stmt = stmt.order_by(ScanRun.started_at.desc(), ScanRun.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def count_scan_results_for_runs(
    session: Session, run_ids: Sequence[int]
) -> dict[int, int]:
    """Return ``{run_id: shortlisted-row count}`` for the given runs.

    The history page needs a "shortlisted results" column for every visible run.
    One grouped COUNT query answers that for the whole page; looping over
    ``run.results`` instead would lazy-load every result row of every run (and
    would crash on detached objects once the session closes).

    Every requested id is present in the returned dict — runs with no results
    map to 0 — so callers never need a ``.get(run_id, 0)`` fallback.
    """
    counts: dict[int, int] = {int(run_id): 0 for run_id in run_ids}
    if not counts:
        return counts
    stmt = (
        select(ScanResult.run_id, func.count())
        .where(ScanResult.run_id.in_(list(counts)))
        .group_by(ScanResult.run_id)
    )
    for run_id, count in session.execute(stmt):
        counts[int(run_id)] = int(count)
    return counts


def list_distinct_screener_keys(session: Session) -> list[str]:
    """Return every screener key that appears in scan history, sorted.

    The history page's screener filter uses this instead of the live screener
    registry on purpose: a screener that was deleted or renamed last month still
    has history worth inspecting, and a broken screener module must never be able
    to take down the audit view.
    """
    stmt = select(ScanRun.screener_key).distinct().order_by(ScanRun.screener_key.asc())
    return list(session.scalars(stmt))


def list_distinct_universe_keys(session: Session) -> list[str]:
    """Return every universe key found in history, sorted and deduplicated."""
    stmt = select(ScanRun.universe_key).distinct().order_by(ScanRun.universe_key.asc())
    return list(session.scalars(stmt))


def list_distinct_triggered_by_values(session: Session) -> list[str]:
    """Return non-empty audit identities for the history trigger filter."""
    stmt = (
        select(ScanRun.triggered_by)
        .where(ScanRun.triggered_by.is_not(None), ScanRun.triggered_by != "")
        .distinct()
        .order_by(ScanRun.triggered_by.asc())
    )
    return list(session.scalars(stmt))


def get_scan_results(session: Session, run_id: int) -> list[ScanResult]:
    """Return all result rows for one run.

    Ordering by symbol makes the output stable for tests and predictable for a
    simple table UI. ``id`` is a tie-breaker in case a screener emits multiple
    rows for the same symbol.
    """
    stmt = (
        select(ScanResult)
        .where(ScanResult.run_id == run_id)
        .order_by(ScanResult.symbol.asc(), ScanResult.id.asc())
    )
    return list(session.scalars(stmt))


def _as_optional_str(value: Any) -> str | None:
    """Convert optional display fields to strings while preserving blanks as NULL."""
    if _is_missing(value):
        return None
    return str(value)


def _as_date(value: Any) -> dt.date | None:
    """Accept common date-ish values and return a real ``date`` for the DB.

    Screeners can hand us a Python date, a datetime, a pandas Timestamp, or a
    simple ``YYYY-MM-DD`` string. Bad or blank values become NULL because some AI
    outputs are not tied to one exact candle.
    """
    if _is_missing(value):
        return None
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value

    try:
        return dt.date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def _as_decimal(value: Any) -> Decimal | None:
    """Convert money/score values to ``Decimal`` without ever using float math."""
    if _is_missing(value):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Return a structure SQLAlchemy's JSON type can serialize safely.

    SQLAlchemy's generic JSON column eventually calls Python's JSON encoder.
    That encoder understands dict/list/str/int/float/bool/None, but not
    ``Decimal``, dates, datetimes, or NumPy scalar objects. This helper walks the
    whole structure and converts those common non-JSON values before insert.

    ``Decimal`` becomes a string on purpose: typed numeric columns are the query
    source of truth, while JSON blobs are audit snapshots where lossless text is
    safer than binary floating-point rounding.
    """
    if _is_missing(value):
        return None
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_json_safe(item) for item in value]
    if isinstance(value, str | int | float | bool):
        return value

    item = getattr(value, "item", None)
    if callable(item):
        try:
            # NumPy and pandas scalar objects commonly expose `.item()`, which
            # turns them into ordinary Python scalars the JSON encoder knows.
            return _json_safe(item())
        except (TypeError, ValueError):
            pass

    # Last-resort fallback: preserve something readable instead of crashing the
    # entire scan because one extra screener column has an unusual object type.
    return str(value)


def _is_missing(value: Any) -> bool:
    """Return True for values we should store as SQL/JSON NULL.

    The ``value != value`` trick catches NaN without importing pandas or NumPy in
    this lightweight storage module, because NaN is the rare value that is not
    equal to itself.
    """
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return False
