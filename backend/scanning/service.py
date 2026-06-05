"""SCAN-003 - run a screener and persist the run plus results.

Beginner note:
Before this layer, ``app.py`` ran a screener and kept the result only in the
current Streamlit browser session. This service wraps one scan in the SCAN-002
persistence lifecycle:

1. Create a ``scan_runs`` audit header in the RUNNING state.
2. Run the screener.
3. Save any shortlisted ``scan_results`` rows.
4. Stamp the run SUCCESS, PARTIAL, or FAILED.

Two deliberate design choices:

1. **UI-agnostic.** This module never imports Streamlit. The exact same
   ``run_scan(...)`` call can be reused later by a headless daily-scan job
   (JOB-001). The caller already has the prepared inputs - the screener's
   callable, the loaded universe, and a data loader - so it passes them in.

2. **Persistence is best-effort.** Running the screener is the primary job, so a
   database problem is logged and the results are still returned to the caller.
   We create the audit header before scanner execution when possible, then save
   rows/final status afterward. If any persistence phase fails, the scanner that
   worked before scan-history existed still keeps working.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from backend.storage.database import session_scope
from backend.storage.models import ScanRun, ScanStatus
from backend.storage.repository import (
    create_scan_run,
    finish_scan_run,
    save_scan_results,
)

logger = logging.getLogger(__name__)


# A "run callable" is any screener's ``run(universe_df, data_loader, params)``.
RunCallable = Callable[[pd.DataFrame, Any, dict[str, Any]], pd.DataFrame]
# A session factory returns a transactional Session context manager (commit on
# success, rollback on error). Defaults to the real database; tests pass a factory
# bound to a temporary database.
SessionFactory = Callable[[], AbstractContextManager[Session]]


@dataclass(frozen=True)
class ScanRunResult:
    """Structured outcome of one scan, returned to the caller.

    ``status`` is SUCCESS (every symbol fine), PARTIAL (usable rows but some
    symbols failed), or FAILED (the screener raised before producing results).
    ``results`` is the screener's DataFrame, unchanged, so the UI renders exactly
    as it did before persistence existed. ``run_id`` is the database id when a
    run header was created, or ``None`` when persistence was unavailable.
    """

    status: ScanStatus
    results: pd.DataFrame
    run_id: int | None = None
    compute_failures: list[dict[str, Any]] = field(default_factory=list)
    error_message: str | None = None


def run_scan(
    *,
    screener_key: str,
    universe_key: str,
    run_callable: RunCallable,
    universe_df: pd.DataFrame,
    data_loader: Any,
    params: dict[str, Any],
    triggered_by: str | None = "ui",
    session_factory: SessionFactory = session_scope,
) -> ScanRunResult:
    """Run one screener, persist its audit trail, and return a safe result object.

    The function does not raise for screener failures or database failures. A
    caller such as Streamlit gets one predictable ``ScanRunResult`` to render,
    while operators still get detailed tracebacks in logs.
    """
    # The service owns failure observation so it can decide SUCCESS vs PARTIAL and
    # so callers do not each have to wire a callback. Copy params first: we must
    # never mutate the caller's dict because the UI reuses it for charts later.
    compute_failures: list[dict[str, Any]] = []
    run_params = dict(params)
    run_params["compute_failure_callback"] = compute_failures.append

    # --- 1. Create the audit header before scanner execution -------------------
    # This is intentionally a short transaction. Long-running screeners should
    # never keep a database transaction open, but operators should still be able
    # to see that a run started even if it fails minutes later.
    run_id = _create_run_header(
        screener_key=screener_key,
        universe_key=universe_key,
        params=params,
        triggered_by=triggered_by,
        session_factory=session_factory,
    )

    # --- 2. Run the screener (the primary job; independent of the database) -----
    error_message: str | None = None
    try:
        results = run_callable(universe_df, data_loader, run_params)
    except Exception as exc:
        # Log the full traceback for the operator, but keep the stored/displayed
        # message secret-free. A raw exception string could echo a token or URL.
        logger.exception("Screener %s raised during scan", screener_key)
        results = pd.DataFrame()
        status = ScanStatus.FAILED
        error_message = (
            f"The screener raised {type(exc).__name__} before producing results."
        )
    else:
        # A scan is PARTIAL when it produced a usable frame but some symbols could
        # not be loaded (data loader) or computed (screener per-symbol callback).
        loader_failures = list(getattr(data_loader, "last_failures", None) or [])
        if loader_failures or compute_failures:
            status = ScanStatus.PARTIAL
            error_message = (
                f"{len(loader_failures)} symbol(s) failed to load and "
                f"{len(compute_failures)} failed to compute."
            )
        else:
            status = ScanStatus.SUCCESS

    # --- 3. Persist rows + final status best-effort ----------------------------
    # If the header could not be created, run_id is None and this becomes a no-op.
    # If saving rows fails after the header exists, we try to mark that run FAILED
    # with a secret-safe persistence message so the history table is still honest.
    _persist_run_outcome(
        run_id=run_id,
        results=results,
        status=status,
        error_message=error_message,
        session_factory=session_factory,
    )

    return ScanRunResult(
        status=status,
        results=results,
        run_id=run_id,
        compute_failures=compute_failures,
        error_message=error_message,
    )


def _create_run_header(
    *,
    screener_key: str,
    universe_key: str,
    params: dict[str, Any],
    triggered_by: str | None,
    session_factory: SessionFactory,
) -> int | None:
    """Create and commit the RUNNING ``scan_runs`` row before the screener runs.

    Beginner note:
    The header row is the durable "this scan started" breadcrumb. We commit it
    before calling the screener so a slow or failing scan remains auditable. If
    this insert fails, we return ``None`` and the scan still runs normally.
    """
    try:
        with session_factory() as session:
            run = create_scan_run(
                session,
                screener_key=screener_key,
                universe_key=universe_key,
                # Strip callbacks/functions so only JSON-storable params are saved.
                params=_params_snapshot(params),
                data_snapshot_date=_as_date(params.get("end_date")),
                triggered_by=triggered_by,
            )
            # Read the flushed id before the context manager commits and closes.
            return run.id
    except Exception:
        logger.warning(
            "Could not create scan run header for %s; continuing without history.",
            screener_key,
            exc_info=True,
        )
        return None


def _persist_run_outcome(
    *,
    run_id: int | None,
    results: pd.DataFrame,
    status: ScanStatus,
    error_message: str | None,
    session_factory: SessionFactory,
) -> None:
    """Save result rows and stamp the final status for an existing run header.

    This is separate from ``_create_run_header`` so the database transaction is
    open only while we write, not while the screener computes candles. That keeps
    SQLite/Postgres locks short and makes long scans safer.
    """
    if run_id is None:
        return

    try:
        with session_factory() as session:
            run = session.get(ScanRun, run_id)
            if run is None:
                raise RuntimeError("scan run header disappeared before finish")
            save_scan_results(session, run, _result_rows(results))
            finish_scan_run(session, run, status=status, error_message=error_message)
    except Exception as exc:
        logger.warning(
            "Could not persist outcome for scan run %s; marking it failed if possible.",
            run_id,
            exc_info=True,
        )
        _mark_run_failed_after_persistence_error(run_id, exc, session_factory)


def _mark_run_failed_after_persistence_error(
    run_id: int,
    persistence_error: Exception,
    session_factory: SessionFactory,
) -> None:
    """Best-effort FAILED stamp when result persistence breaks after header insert.

    We include only the exception *type* in the stored message. The full traceback
    is already in logs, while ``scan_runs.error_message`` may be shown in a future
    history UI and must not echo secrets from raw exception text.
    """
    error_message = (
        f"Could not persist scan results ({type(persistence_error).__name__})."
    )
    try:
        with session_factory() as session:
            run = session.get(ScanRun, run_id)
            if run is None:
                return
            finish_scan_run(
                session,
                run,
                status=ScanStatus.FAILED,
                error_message=error_message,
            )
    except Exception:
        logger.warning(
            "Could not mark scan run %s failed after persistence error.",
            run_id,
            exc_info=True,
        )


def _params_snapshot(params: dict[str, Any]) -> dict[str, Any]:
    """Drop callables (e.g. progress/compute callbacks) so params stay JSON-safe."""
    return {key: value for key, value in params.items() if not callable(value)}


def _result_rows(results: pd.DataFrame) -> list[dict[str, Any]]:
    """Convert the screener DataFrame into the row dicts the repository expects."""
    if results is None or results.empty:
        return []
    return results.to_dict("records")


def _as_date(value: Any) -> dt.date | None:
    """Best-effort extraction of a ``date`` for the run's data-snapshot field."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None
