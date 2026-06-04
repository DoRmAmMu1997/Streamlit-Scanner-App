"""Repository helpers for persisted scan runs and results."""

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
    """Insert a running scan header and flush so the caller can use its id."""
    run = ScanRun(
        started_at=dt.datetime.now(dt.UTC),
        status=ScanStatus.RUNNING,
        screener_key=screener_key,
        universe_key=universe_key,
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
    """Persist screener rows as typed result records linked to a scan run."""
    results: list[ScanResult] = []
    for row in rows:
        close_value = row.get("close")
        if _is_missing(close_value):
            close_value = row.get("close_price")

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
    """Set the final scan status, finished timestamp, and optional error text."""
    run.status = status
    run.finished_at = dt.datetime.now(dt.UTC)
    run.error_message = error_message
    session.flush()


def get_latest_scan_runs(session: Session, limit: int = 50) -> list[ScanRun]:
    """Return the newest scan headers first."""
    stmt = select(ScanRun).order_by(ScanRun.started_at.desc()).limit(limit)
    return list(session.scalars(stmt))


def get_scan_results(session: Session, run_id: int) -> list[ScanResult]:
    """Return all results for one run, sorted for stable display and tests."""
    stmt = (
        select(ScanResult)
        .where(ScanResult.run_id == run_id)
        .order_by(ScanResult.symbol.asc(), ScanResult.id.asc())
    )
    return list(session.scalars(stmt))


def _as_optional_str(value: Any) -> str | None:
    if _is_missing(value):
        return None
    return str(value)


def _as_date(value: Any) -> dt.date | None:
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
    if _is_missing(value):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _json_safe(value: Any) -> Any:
    """Return a structure SQLAlchemy's JSON type can serialize safely."""
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
            return _json_safe(item())
        except (TypeError, ValueError):
            pass

    return str(value)


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return value == ""
    try:
        return bool(value != value)
    except (TypeError, ValueError):
        return False
