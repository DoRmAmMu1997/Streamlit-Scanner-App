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
   rows/final status afterward. These are separate database transactions on
   purpose: the RUNNING row becomes visible immediately, while the actual scan
   can take as long as it needs without holding a database lock.
"""

from __future__ import annotations

import datetime as dt
import logging
import sys
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Any

import pandas as pd
from sqlalchemy.orm import Session

from backend.observability import (
    EVENT_SCAN_COMPLETED,
    EVENT_SCAN_FAILED,
    EVENT_SCAN_PARTIAL,
    EVENT_SCAN_STARTED,
    EVENT_SYMBOL_SCAN_FAILED,
    ExceptionInfo,
    log_event,
)
from backend.scanning.result_contract import normalize_screener_row
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

    Beginner note:
    This dataclass is the service boundary. Streamlit should not need to know
    whether the database write happened in one transaction, two transactions, or
    not at all; it only needs this compact result object.
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
    scan_name: str | None = None,
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

    Lifecycle detail:
    ``create_scan_run`` happens before ``run_callable`` so a long-running scan is
    auditable while it is still running. ``save_scan_results`` and
    ``finish_scan_run`` happen afterward, once the screener has returned or
    failed. That mirrors how a job queue usually records "started" and "finished"
    events.

    ``scan_name`` is optional human-readable context supplied by configured
    headless jobs. The Streamlit UI can omit it because ``screener_key`` still
    identifies the scan; JOB-002 passes its named YAML entry so production logs
    distinguish two scheduled runs of the same screener.
    """
    # The service owns failure observation so it can decide SUCCESS vs PARTIAL and
    # so callers do not each have to wire a callback. Copy params first: we must
    # never mutate the caller's dict because the UI reuses it for charts later.
    compute_failures: list[dict[str, Any]] = []
    run_params = dict(params)
    run_params["compute_failure_callback"] = compute_failures.append
    # OBS-001: monotonic clock so scan_completed can report a wall-clock duration.
    started_at = time.monotonic()

    # --- 1. Create the audit header before scanner execution -------------------
    # This is intentionally a short transaction. Long-running screeners should
    # never keep a database transaction open, but operators should still be able
    # to see that a run started even if it fails minutes later.
    run_id, header_exc_info = _create_run_header(
        screener_key=screener_key,
        universe_key=universe_key,
        params=params,
        triggered_by=triggered_by,
        # SCAN-004: record the universe size so the history page can show how
        # many symbols this run scanned (vs how many it shortlisted).
        symbols_scanned=int(len(universe_df)) if universe_df is not None else None,
        session_factory=session_factory,
    )

    # OBS-001: emit scan_started *after* the header so the event carries the run_id
    # that ties every later event and persisted row back to this run.
    log_event(
        logger,
        EVENT_SCAN_STARTED,
        run_id=run_id,
        scan_name=scan_name,
        screener_key=screener_key,
        universe_key=universe_key,
        triggered_by=triggered_by,
        symbols_scanned=int(len(universe_df)) if universe_df is not None else 0,
    )
    if header_exc_info is not None:
        log_event(
            logger,
            EVENT_SCAN_FAILED,
            level=logging.ERROR,
            exc_info=header_exc_info,
            run_id=None,
            scan_name=scan_name,
            screener_key=screener_key,
            universe_key=universe_key,
            phase="create_header",
            error_type=header_exc_info[0].__name__,
        )

    # --- 2. Run the screener (the primary job; independent of the database) -----
    error_message: str | None = None
    screener_exc_info: ExceptionInfo | None = None
    # Track per-symbol load failures across the try/except so the observability
    # events below can report them whether or not the screener itself raised.
    loader_failures: list[dict[str, Any]] = []
    try:
        results = run_callable(universe_df, data_loader, run_params)
    except Exception as exc:
        # Log the full traceback for the operator, but keep the stored/displayed
        # message secret-free. A raw exception string could echo a token, URL, or
        # broker response body, while the exception class (RuntimeError, KeyError,
        # etc.) is useful and safe enough for the future history UI.
        # Keep the original exception tuple until persistence has finished. That
        # lets the terminal scan_failed event reflect durable state while still
        # carrying the original traceback through the redacting formatter.
        screener_exc_info = sys.exc_info()
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

    # OBS-001: one symbol_scan_failed per failed symbol (load failures from the
    # data loader, compute failures from the screener callback), each tagged with
    # run_id + symbol so a single stock's failure is traceable. Both message
    # sources were already redacted where they were produced.
    for failure in (*loader_failures, *compute_failures):
        log_event(
            logger,
            EVENT_SYMBOL_SCAN_FAILED,
            level=logging.WARNING,
            run_id=run_id,
            scan_name=scan_name,
            screener_key=screener_key,
            universe_key=universe_key,
            symbol=failure.get("symbol"),
            # Named ``error`` (not ``message``) so it stays a distinct JSON field
            # and matches external_api_failed. Source messages are already redacted.
            error=failure.get("message"),
        )

    # --- 3. Persist rows + final status best-effort ----------------------------
    # If the header could not be created, run_id is None and this becomes a no-op.
    # The UI still receives the screener rows. If saving rows fails after the
    # header exists, we try to mark that run FAILED with a secret-safe persistence
    # message so the history table does not get stuck forever in RUNNING.
    persistence_exc_info = _persist_run_outcome(
        run_id=run_id,
        results=results,
        status=status,
        error_message=error_message,
        screener_key=screener_key,
        params=params,
        data_snapshot_date=_as_date(params.get("end_date")),
        session_factory=session_factory,
    )

    # --- 4. Emit terminal events after the durable write attempt ---------------
    # A log consumer may react to scan_completed immediately, so success/partial
    # events are emitted only when the corresponding database row was actually
    # finalized. Persistence and header failures get their own scan_failed phase
    # while the in-memory result remains available to the caller.
    terminal_fields = {
        "run_id": run_id,
        "scan_name": scan_name,
        "screener_key": screener_key,
        "universe_key": universe_key,
        "status": status.value,
        "results_count": 0 if results is None else int(len(results)),
        "loader_failures": len(loader_failures),
        "compute_failures": len(compute_failures),
        "duration_seconds": round(time.monotonic() - started_at, 3),
    }
    if screener_exc_info is not None:
        log_event(
            logger,
            EVENT_SCAN_FAILED,
            level=logging.ERROR,
            exc_info=screener_exc_info,
            phase="screener",
            error_type=screener_exc_info[0].__name__,
            **terminal_fields,
        )
    if persistence_exc_info is not None:
        log_event(
            logger,
            EVENT_SCAN_FAILED,
            level=logging.ERROR,
            exc_info=persistence_exc_info,
            phase="persistence",
            error_type=persistence_exc_info[0].__name__,
            **terminal_fields,
        )
    elif run_id is not None and status is ScanStatus.SUCCESS:
        log_event(logger, EVENT_SCAN_COMPLETED, **terminal_fields)
    elif run_id is not None and status is ScanStatus.PARTIAL:
        log_event(logger, EVENT_SCAN_PARTIAL, level=logging.WARNING, **terminal_fields)

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
    symbols_scanned: int | None,
    session_factory: SessionFactory,
) -> tuple[int | None, ExceptionInfo | None]:
    """Create and commit the RUNNING ``scan_runs`` row before the screener runs.

    Beginner note:
    The header row is the durable "this scan started" breadcrumb. We commit it
    before calling the screener so a slow or failing scan remains auditable. If
    this insert fails, we return ``None`` and the scan still runs normally. That
    tradeoff keeps scan-history helpful but never mandatory for the scanner's
    core job: returning the latest shortlist to the user.
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
                symbols_scanned=symbols_scanned,
            )
            # Read the flushed id before the context manager commits and closes.
            # SQLAlchemy fills this value during flush, which create_scan_run()
            # performs for us; after the context manager exits, tests and callers
            # can use the id to fetch the same run in a new session.
            return run.id, None
    except Exception:
        return None, sys.exc_info()


def _persist_run_outcome(
    *,
    run_id: int | None,
    results: pd.DataFrame,
    status: ScanStatus,
    error_message: str | None,
    screener_key: str,
    params: dict[str, Any],
    data_snapshot_date: dt.date | None,
    session_factory: SessionFactory,
) -> ExceptionInfo | None:
    """Save result rows and stamp the final status for an existing run header.

    This is separate from ``_create_run_header`` so the database transaction is
    open only while we write, not while the screener computes candles. That keeps
    SQLite/Postgres locks short and makes long scans safer.

    If result persistence fails, the returned ``ScanRunResult`` still reflects
    what happened in memory. The database row may be marked FAILED as a separate
    audit concern, because "the scan succeeded but history write failed" is still
    something operators should be able to notice.
    """
    if run_id is None:
        return None

    try:
        with session_factory() as session:
            # Fetch the header in this fresh session. The object returned by
            # _create_run_header() belonged to a different transaction and should
            # not be reused after that transaction has committed.
            run = session.get(ScanRun, run_id)
            if run is None:
                raise RuntimeError("scan run header disappeared before finish")
            save_scan_results(
                session,
                run,
                _result_rows(
                    results,
                    screener_key=screener_key,
                    params=params,
                    data_snapshot_date=data_snapshot_date,
                ),
            )
            finish_scan_run(session, run, status=status, error_message=error_message)
    except Exception as exc:
        persistence_exc_info = sys.exc_info()
        _mark_run_failed_after_persistence_error(run_id, exc, session_factory)
        return persistence_exc_info
    return None


def _mark_run_failed_after_persistence_error(
    run_id: int,
    persistence_error: Exception,
    session_factory: SessionFactory,
) -> None:
    """Best-effort FAILED stamp when result persistence breaks after header insert.

    We include only the exception *type* in the stored message. The full traceback
    is already in logs, while ``scan_runs.error_message`` may be shown in a future
    history UI and must not echo secrets from raw exception text. This is why the
    message says ``IntegrityError`` or ``OperationalError`` instead of copying the
    database driver's whole exception string.
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


def _result_rows(
    results: pd.DataFrame,
    *,
    screener_key: str,
    params: dict[str, Any],
    data_snapshot_date: dt.date | None,
) -> list[dict[str, Any]]:
    """Return normalized persistence copies of the screener's DataFrame rows.

    ``normalize_screener_row`` recursively copies nested values. Provenance and
    JSON conversions therefore affect only the dictionaries passed to storage;
    ``ScanRunResult.results`` remains the exact DataFrame returned by the
    screener for Streamlit to render.
    """
    if results is None or results.empty:
        return []
    return [
        normalize_screener_row(
            row,
            screener_key=screener_key,
            params=params,
            data_snapshot_date=data_snapshot_date,
        )
        for row in results.to_dict("records")
    ]


def _as_date(value: Any) -> dt.date | None:
    """Best-effort extraction of a ``date`` for the run's data-snapshot field."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None
