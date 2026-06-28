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
from typing import Any, cast

import pandas as pd
from sqlalchemy.orm import Session

from backend.data_quality import CandleQualityReport
from backend.observability import (
    EVENT_SCAN_COMPLETED,
    EVENT_SCAN_FAILED,
    EVENT_SCAN_PARTIAL,
    EVENT_SCAN_SCORING_FAILED,
    EVENT_SCAN_STARTED,
    EVENT_SYMBOL_SCAN_FAILED,
    ExceptionInfo,
    log_event,
)
from backend.scanning.result_contract import ResultContractError, normalize_screener_row
from backend.scoring import ScoringContext, load_scoring_config, score_candidates
from backend.security import redact_text
from backend.storage.database import session_scope
from backend.storage.models import ScanStatus
from backend.storage.repository import (
    create_scan_run,
    finish_scan_run,
    get_scan_run,
    save_ai_evaluations,
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
    symbols failed), or FAILED (computation or durable finalization failed).
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
    rejected_result_rows: int = 0
    # AI-004 (AC3): how many AI verdicts were rejected because their output failed
    # strict validation within the configured attempt budget (a subset of
    # ``compute_failures`` tagged ``phase="ai_validation"``).
    ai_validation_failures: int = 0
    # DATA-001B candle-quality receipt for this run (None when nothing was checked).
    data_quality_json: dict[str, Any] | None = None
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
    ai_evaluations: list[Any] = []
    run_params = dict(params)
    run_params["compute_failure_callback"] = compute_failures.append
    run_params["ai_evaluation_callback"] = ai_evaluations.append
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
        symbols_scanned=len(universe_df) if universe_df is not None else None,
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
        symbols_scanned=len(universe_df) if universe_df is not None else 0,
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
    data_quality_json: dict[str, Any] | None = None
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
        screener_exc_info = _current_exc_info()
        results = pd.DataFrame()
        status = ScanStatus.FAILED
        error_message = (
            f"The screener raised {type(exc).__name__} before producing results."
        )
    else:
        results = _score_results_safely(
            results,
            run_id=run_id,
            scan_name=scan_name,
            screener_key=screener_key,
            universe_key=universe_key,
            universe_df=universe_df,
            data_loader=data_loader,
            data_snapshot_date=_as_date(params.get("end_date")),
        )
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

    # DATA-001B: summarize the loader's per-symbol quality reports into the
    # receipt persisted on this run, then let fatal candle defects influence the
    # final status so the run is reported truthfully (never a false "success").
    data_quality_json = _data_quality_receipt(
        data_loader,
        expected_latest_date=_as_date(params.get("end_date")),
    )
    data_quality_fatal_symbols = (
        int(data_quality_json["fatal_symbols"]) if data_quality_json else 0
    )
    if data_quality_fatal_symbols:
        # At least one symbol was quarantined for bad data:
        #   - a previously-clean run drops to PARTIAL (some data was dropped), and
        #   - if NO symbol survived AND the screener produced no rows, the run is a
        #     FAILED (there was effectively nothing to scan).
        # An already-PARTIAL/FAILED status is left as-is (it's at least as severe).
        if status is ScanStatus.SUCCESS:
            status = ScanStatus.PARTIAL
        if (
            data_quality_json is not None
            and int(data_quality_json["usable_symbols"]) == 0
            and (results is None or results.empty)
        ):
            status = ScanStatus.FAILED
        error_message = _combine_error_messages(
            error_message,
            f"{data_quality_fatal_symbols} symbol(s) had fatal candle data quality findings.",
        )

    emitted_rejected_rows = sum(
        1
        for failure in compute_failures
        if failure.get("phase") == "result_contract"
    )
    normalized_rows, persistence_rejected_rows = _result_rows(
        results,
        screener_key=screener_key,
        params=params,
        data_snapshot_date=_as_date(params.get("end_date")),
    )
    rejected_result_rows = emitted_rejected_rows + persistence_rejected_rows
    if rejected_result_rows:
        rejection_message = (
            f"{rejected_result_rows} result row(s) failed the result contract."
        )
        status = ScanStatus.PARTIAL if normalized_rows else ScanStatus.FAILED
        error_message = _combine_error_messages(error_message, rejection_message)

    # AI-004 (AC3): surface AI output-validation failures explicitly at the run
    # level. They are a subset of compute_failures — the AI screeners tag them
    # phase="ai_validation" once the configured attempt budget is exhausted
    # (AIValidationError) — so the generic "failed to compute" count already
    # includes them. This adds a separately-countable clause + terminal event
    # field so an operator can tell "the AI was unavailable" (an SDK/usage-limit
    # failure) apart from "the AI returned junk we could not parse".
    ai_validation_failures = sum(
        1 for failure in compute_failures if failure.get("phase") == "ai_validation"
    )
    if ai_validation_failures:
        error_message = _combine_error_messages(
            error_message,
            f"{ai_validation_failures} AI output(s) failed validation "
            "within the configured attempt budget.",
        )

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
        normalized_rows=normalized_rows,
        ai_evaluations=ai_evaluations,
        status=status,
        error_message=error_message,
        data_quality_json=data_quality_json,
        session_factory=session_factory,
    )
    if persistence_exc_info is not None:
        status = ScanStatus.FAILED
        persistence_error_message = (
            f"Could not persist scan results "
            f"({persistence_exc_info[0].__name__})."
        )
        error_message = _combine_error_messages(
            error_message, persistence_error_message
        )

    # --- 4. Emit terminal events after the durable write attempt ---------------
    # A log consumer may react to scan_completed immediately, so success/partial
    # events are emitted only when the corresponding database row was actually
    # finalized. Persistence and header failures get their own scan_failed phase
    # while the in-memory result remains available to the caller.
    terminal_fields: dict[str, Any] = {
        "run_id": run_id,
        "scan_name": scan_name,
        "screener_key": screener_key,
        "universe_key": universe_key,
        "status": status.value,
        "results_count": 0 if results is None else len(results),
        "persisted_results_count": (
            len(normalized_rows)
            if run_id is not None and persistence_exc_info is None
            else 0
        ),
        "rejected_result_rows": rejected_result_rows,
        "ai_evaluation_count": (
            len(ai_evaluations)
            if run_id is not None and persistence_exc_info is None
            else 0
        ),
        "loader_failures": len(loader_failures),
        "compute_failures": len(compute_failures),
        "ai_validation_failures": ai_validation_failures,
        "data_quality_checked_symbols": (
            data_quality_json["checked_symbols"] if data_quality_json else 0
        ),
        "data_quality_warning_findings": (
            data_quality_json["warning_findings"] if data_quality_json else 0
        ),
        "data_quality_fatal_findings": (
            data_quality_json["fatal_findings"] if data_quality_json else 0
        ),
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
        rejected_result_rows=rejected_result_rows,
        ai_validation_failures=ai_validation_failures,
        data_quality_json=data_quality_json,
        error_message=error_message,
    )


# Cap how many individual findings the per-run receipt persists. A full-universe
# run can surface hundreds of findings (e.g. many stale/gapped symbols); the
# aggregate counts still describe the whole set, so storing every finding row
# would only bloat the JSON column and the health table. Fatal findings are
# kept ahead of warnings so the most actionable issues always survive the cap.
MAX_PERSISTED_FINDINGS = 50


def _data_quality_receipt(
    data_loader: Any,
    *,
    expected_latest_date: dt.date | None,
) -> dict[str, Any] | None:
    """Summarize the loader's per-symbol quality reports into one persisted receipt.

    The receipt (stored as ``scan_runs.data_quality_json``) is a small, versioned
    JSON snapshot the health page and auditors read later. It has two layers:

    - **aggregate counts** (``checked_symbols``, ``usable_symbols``,
      ``warning_symbols``/``fatal_symbols``, ``warning_findings``/``fatal_findings``)
      computed over *every* report — these always describe the full run; and
    - a **capped, redacted ``findings`` sample** (fatal-first, see
      ``MAX_PERSISTED_FINDINGS``) so a 500-symbol run can't bloat the column.

    Returns ``None`` when the loader recorded no reports at all (e.g. a fully
    cached run that fetched nothing), in which case there is no receipt to store.
    ``getattr(..., None) or []`` tolerates loaders/fakes that don't expose the
    attribute.
    """
    reports = list(
        cast(
            list[CandleQualityReport],
            getattr(data_loader, "last_data_quality_reports", None) or [],
        )
    )
    if not reports:
        return None

    findings: list[dict[str, Any]] = []
    warning_symbols = 0
    fatal_symbols = 0
    warning_findings = 0
    fatal_findings = 0
    for report in reports:
        # Count a symbol once per severity it exhibits (a symbol can have both a
        # warning and a fatal finding), and count findings individually.
        has_warning = any(
            finding.severity == "warning" for finding in report.findings
        )
        has_fatal = report.has_fatal_findings
        if has_warning:
            warning_symbols += 1
        if has_fatal:
            fatal_symbols += 1
        for finding in report.findings:
            if finding.severity == "fatal":
                fatal_findings += 1
            else:
                warning_findings += 1
            findings.append(
                {
                    "symbol": report.symbol,
                    "severity": finding.severity,
                    "code": finding.code,
                    # Messages are app-generated (codes + thresholds, no prices),
                    # but redact anyway — this JSON is written to durable history.
                    "message": redact_text(finding.message),
                    "affected_rows": finding.affected_rows,
                    "latest_date": (
                        report.latest_date.isoformat()
                        if report.latest_date is not None
                        else None
                    ),
                }
            )

    total_findings = len(findings)
    # Persist fatal findings before warnings, then cap. ``sort`` is stable, so
    # the per-symbol order within each severity is preserved.
    findings.sort(key=lambda item: 0 if item["severity"] == "fatal" else 1)
    capped_findings = findings[:MAX_PERSISTED_FINDINGS]

    return {
        "schema_version": 1,
        "expected_latest_date": (
            expected_latest_date.isoformat() if expected_latest_date else None
        ),
        "checked_symbols": len(reports),
        "usable_symbols": sum(1 for report in reports if report.is_usable),
        "warning_symbols": warning_symbols,
        "fatal_symbols": fatal_symbols,
        "warning_findings": warning_findings,
        "fatal_findings": fatal_findings,
        "total_findings": total_findings,
        "findings_truncated": total_findings > len(capped_findings),
        "findings": capped_findings,
    }


def _current_exc_info() -> ExceptionInfo:
    """Return ``sys.exc_info()`` typed for use inside an ``except`` block.

    Typeshed types ``sys.exc_info()`` as possibly the ``(None, None, None)``
    triple; inside an active ``except`` it never is, so the cast is truthful.
    """
    return cast("ExceptionInfo", sys.exc_info())


def _score_results_safely(
    results: pd.DataFrame,
    *,
    run_id: int | None,
    scan_name: str | None,
    screener_key: str,
    universe_key: str,
    universe_df: pd.DataFrame,
    data_loader: Any,
    data_snapshot_date: dt.date | None,
) -> pd.DataFrame:
    """Apply RANK-002 scoring without making scoring a scan failure.

    Beginner note:
    Ranking is useful metadata, but the core scanner promise is still "show the
    shortlisted rows." If scoring has a bug or reads a corrupt cache file, this
    wrapper logs a precise warning, adds a null ``final_score`` column, and lets
    persistence/UI continue with the original results.

    This wrapper is also the boundary that protects the "cache-only" scoring
    rule. The scorer receives the same ``universe_df`` and ``data_loader`` that
    the screener used, plus the stored data snapshot date from params; it does
    not get credentials or permission to start a fresh market-data fetch.
    """
    if results is None or results.empty:
        return results

    try:
        # Load config inside the wrapper so malformed YAML is handled by the
        # scoring config loader and so tests can monkeypatch the public scorer
        # without needing to construct an entire app boot sequence.
        return score_candidates(
            results,
            context=ScoringContext(
                universe_key=universe_key,
                universe_df=universe_df,
                data_loader=data_loader,
                data_snapshot_date=data_snapshot_date,
                config=load_scoring_config(),
            ),
        )
    except Exception:
        exc_info = _current_exc_info()
        log_event(
            logger,
            EVENT_SCAN_SCORING_FAILED,
            level=logging.WARNING,
            exc_info=exc_info,
            run_id=run_id,
            scan_name=scan_name,
            screener_key=screener_key,
            universe_key=universe_key,
            phase="scoring",
            error_type=exc_info[0].__name__,
            results_count=len(results),
        )
        # Preserve the original rows and row order. A null score is explicit
        # enough for display/export without pretending a failed scoring pass
        # produced a meaningful rank.
        return _with_null_final_score(results)


def _with_null_final_score(results: pd.DataFrame) -> pd.DataFrame:
    """Return a copy with explicit null scores after scoring failure.

    The copy protects the caller's original DataFrame. That matters in
    Streamlit because the same result object can be cached in session state and
    reused across reruns.
    """
    fallback = results.copy(deep=True)
    fallback["final_score"] = None
    return fallback


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
                params=params,
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
        return None, _current_exc_info()


def _persist_run_outcome(
    *,
    run_id: int | None,
    normalized_rows: list[dict[str, Any]],
    ai_evaluations: list[Any],
    status: ScanStatus,
    error_message: str | None,
    data_quality_json: dict[str, Any] | None,
    session_factory: SessionFactory,
) -> ExceptionInfo | None:
    """Save result rows and stamp the final status for an existing run header.

    This is separate from ``_create_run_header`` so the database transaction is
    open only while we write, not while the screener computes candles. That keeps
    SQLite/Postgres locks short and makes long scans safer.

    PROV-001A also passes the screener identity, original run parameters, and
    market-data date into this final persistence step. Those values describe
    the circumstances of the scan, so the normalizer can fill a useful
    provenance envelope even when a legacy result row contains no provenance.
    Normalization is intentionally delayed until here: earlier conversion could
    change the DataFrame that Streamlit expects to receive unchanged.

    If result persistence fails, the DataFrame still returns for UI recovery, but
    the service outcome is FAILED because no durable result/evaluation rows were
    committed. The database row is marked FAILED in a separate best-effort
    transaction so callers, logs, and history agree on the terminal state.
    """
    if run_id is None:
        return None

    try:
        with session_factory() as session:
            # Fetch the header in this fresh session. The object returned by
            # _create_run_header() belonged to a different transaction and should
            # not be reused after that transaction has committed.
            run = get_scan_run(session, run_id)
            if run is None:
                raise RuntimeError("scan run header disappeared before finish")
            save_scan_results(
                session,
                run,
                normalized_rows,
            )
            save_ai_evaluations(session, run, ai_evaluations)
            finish_scan_run(
                session,
                run,
                status=status,
                error_message=error_message,
                data_quality_json=data_quality_json,
            )
    except Exception as exc:
        persistence_exc_info = _current_exc_info()
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
            run = get_scan_run(session, run_id)
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


def _result_rows(
    results: pd.DataFrame,
    *,
    screener_key: str,
    params: dict[str, Any],
    data_snapshot_date: dt.date | None,
) -> tuple[list[dict[str, Any]], int]:
    """Return normalized persistence copies of the screener's DataFrame rows.

    ``normalize_screener_row`` recursively copies nested values. Provenance and
    JSON conversions therefore affect only the dictionaries passed to storage;
    ``ScanRunResult.results`` remains the exact DataFrame returned by the
    screener for Streamlit to render.
    """
    if results is None or results.empty:
        return [], 0

    # ``to_dict("records")`` creates one ordinary mapping per DataFrame row.
    # The normalizer then deep-copies nested content and enriches only these
    # persistence records, never the DataFrame owned by the caller.
    rows: list[dict[str, Any]] = []
    skipped = 0
    for index, row in enumerate(results.to_dict("records")):
        try:
            rows.append(
                normalize_screener_row(
                    row,
                    screener_key=screener_key,
                    params=params,
                    data_snapshot_date=data_snapshot_date,
                )
            )
        except ResultContractError:
            # One unusable row (typically a NaN symbol from a screener merge
            # bug) must not erase history for every other result in the run.
            # Skip it loudly; the in-memory DataFrame shown by Streamlit is
            # unaffected, matching the per-symbol failure philosophy used by
            # the scanner and the data loader.
            skipped += 1
            logger.warning(
                "Skipping result row %d for %s; it cannot satisfy the result "
                "contract (%s).",
                index,
                screener_key,
                ResultContractError.__name__,
            )
    return rows, skipped


def _combine_error_messages(*messages: str | None) -> str | None:
    """Join already-safe status summaries without exposing exception text."""
    parts = [message for message in messages if message]
    return " ".join(parts) if parts else None


def _as_date(value: Any) -> dt.date | None:
    """Best-effort extraction of a ``date`` for the run's data-snapshot field."""
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    return None
