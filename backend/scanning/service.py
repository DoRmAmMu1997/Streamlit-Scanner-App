"""SCAN-003 — the scan service: run a screener and persist the run + results.

Beginner note:
Before this layer, ``app.py`` ran a screener and threw the result away when the
Streamlit session ended. This service wraps that single run in the SCAN-002
persistence lifecycle — create a ``scan_runs`` header, run the screener, save the
shortlisted ``scan_results``, and stamp a final status — so the app builds an
auditable history of "what did we scan, and what did it find?".

Two deliberate design choices:

1. **UI-agnostic.** This module never imports Streamlit. The exact same
   ``run_scan(...)`` call can be reused later by a headless daily-scan job
   (JOB-001). The caller (the UI today) already holds the prepared inputs — the
   screener's run-callable, the loaded universe, and a data loader — so it passes
   them in. That keeps the service easy to unit-test and avoids re-discovering
   screeners on every scan.

2. **Persistence is best-effort.** Running the screener is the primary job, so a
   database problem (for example, migrations not applied yet) is logged and the
   results are still returned to the caller. The run simply ends up unrecorded
   (``run_id`` is ``None``). The scanner that worked before scan-history existed
   must never break *because of* scan-history.
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
from backend.storage.models import ScanStatus
from backend.storage.repository import create_scan_run, finish_scan_run, save_scan_results

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

    - ``status`` — SUCCESS (every symbol fine), PARTIAL (usable rows but some
      symbols failed), or FAILED (the screener raised before producing results).
    - ``results`` — the screener's own DataFrame, unchanged, so the UI renders
      exactly as it did before this layer existed.
    - ``run_id`` — the persisted ``scan_runs`` id, or ``None`` when persistence was
      skipped/failed (the results are still valid either way).
    - ``compute_failures`` — per-symbol compute errors the screener reported.
    - ``error_message`` — a short, secret-free explanation for PARTIAL/FAILED runs.
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
    """Run one screener, persist the run + its results, and return a structured result.

    This function does not raise for a screener failure or a database failure —
    both are captured in the returned ``ScanRunResult`` so the caller has a single,
    predictable object to render.
    """
    # The service owns failure observation so it can decide SUCCESS vs PARTIAL and
    # so callers do not each have to wire a callback. Copy params first — we must
    # never mutate the caller's dict (it is also used to build charts later).
    compute_failures: list[dict[str, Any]] = []
    run_params = dict(params)
    run_params["compute_failure_callback"] = compute_failures.append

    # --- 1. Run the screener (the primary job; independent of the database) -----
    error_message: str | None = None
    try:
        results = run_callable(universe_df, data_loader, run_params)
    except Exception:
        # Log the full traceback for the operator, but keep the stored/displayed
        # message secret-free: a raw exception string could echo a token or URL.
        logger.exception("Screener %s raised during scan", screener_key)
        results = pd.DataFrame()
        status = ScanStatus.FAILED
        error_message = "The screener raised an error before producing results."
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

    # --- 2. Persist best-effort: a DB problem must not lose the scan ------------
    run_id = _persist_run(
        screener_key=screener_key,
        universe_key=universe_key,
        params=params,
        results=results,
        status=status,
        error_message=error_message,
        triggered_by=triggered_by,
        session_factory=session_factory,
    )

    return ScanRunResult(
        status=status,
        results=results,
        run_id=run_id,
        compute_failures=compute_failures,
        error_message=error_message,
    )


def _persist_run(
    *,
    screener_key: str,
    universe_key: str,
    params: dict[str, Any],
    results: pd.DataFrame,
    status: ScanStatus,
    error_message: str | None,
    triggered_by: str | None,
    session_factory: SessionFactory,
) -> int | None:
    """Write the run + results in one transaction; return the run id, or ``None``.

    Wrapped in a broad ``try/except`` on purpose: persisting scan history is a
    secondary feature, so a missing table or a locked database degrades to "no
    history recorded" instead of breaking the user's scan. The repository owns the
    create → save → finish steps; we own the one transaction around them.
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
            save_scan_results(session, run, _result_rows(results))
            finish_scan_run(session, run, status=status, error_message=error_message)
            # Read the flushed id before the context manager commits and closes.
            return run.id
    except Exception:
        logger.warning(
            "Could not persist scan run for %s; returning results without history.",
            screener_key,
            exc_info=True,
        )
        return None


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
