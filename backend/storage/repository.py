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

from sqlalchemy import select
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


def get_latest_scan_runs(session: Session, limit: int = 50) -> list[ScanRun]:
    """Return the newest scan headers first.

    SCAN-004's history page will likely call this for its initial table. The
    default limit keeps the query bounded even after the app has months of runs.
    """
    stmt = select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
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
