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
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any, cast

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, joinedload

from backend.storage.models import (
    AIEvaluation,
    AppConfig,
    AuditLog,
    ForwardReturnStatus,
    ScanResult,
    ScanRun,
    ScanStatus,
    SignalForwardReturn,
)

if TYPE_CHECKING:
    from backend.validation.benchmarks import BenchmarkLeg
    from backend.validation.forward_return import ForwardReturnPoint

_AI_EVALUATION_OUTCOMES = frozenset({"approved", "rejected", "error"})
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")


@dataclass(frozen=True)
class ForwardReturnMetricRecord:
    """Read-only joined row for VALID-003A aggregate validation metrics."""

    run_id: int
    run_started_at: dt.datetime
    result_id: int
    screener_key: str
    universe_key: str
    symbol: str
    signal_date: dt.date | None
    horizon_days: int
    status: ForwardReturnStatus
    forward_return_pct: Decimal | None
    excess_return_pct: Decimal | None
    max_adverse_excursion_pct: Decimal | None
    max_favorable_excursion_pct: Decimal | None


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
    from backend.scanning.result_contract import normalize_secret_safe_json

    run = ScanRun(
        started_at=dt.datetime.now(dt.UTC),
        status=ScanStatus.RUNNING,
        screener_key=screener_key,
        universe_key=universe_key,
        # Params may contain dates/Decimals in future screeners. Store a
        # JSON-safe copy, not the caller's original object.
        params_json=cast(
            dict[str, Any] | None,
            normalize_secret_safe_json(dict(params)) if params else None,
        ),
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
    from backend.scanning.result_contract import normalize_secret_safe_json

    results: list[ScanResult] = []
    for row in rows:
        normalized_row = normalize_secret_safe_json(dict(row))
        if not isinstance(normalized_row, dict):
            raise ValueError("Scan result normalization must produce a JSON object.")
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
            raw_result_json=cast(dict[str, Any], normalized_row),
            provenance_json=cast(
                dict[str, Any] | None,
                normalize_secret_safe_json(provenance_value)
                if provenance_value is not None
                else None,
            ),
        )
        results.append(result)

    # Extending the relationship fills each result's run_id for us. We flush so
    # tests and callers can inspect result ids before the outer transaction
    # commits.
    run.results.extend(results)
    session.flush()
    return results


def save_ai_evaluations(
    session: Session,
    run: ScanRun,
    records: Sequence[Mapping[str, Any] | Any],
) -> list[AIEvaluation]:
    """Validate, sanitize, and persist AI callback records for one run."""
    evaluations = [_build_ai_evaluation(record) for record in records]
    run.ai_evaluations.extend(evaluations)
    session.flush()
    return evaluations


def finish_scan_run(
    session: Session,
    run: ScanRun,
    *,
    status: ScanStatus,
    error_message: str | None = None,
    data_quality_json: Mapping[str, Any] | None = None,
) -> None:
    """Set the final scan status, finished timestamp, and optional error text.

    Use ``ScanStatus.SUCCESS`` when every symbol completed, ``PARTIAL`` when the
    scan produced usable rows but some symbols failed, and ``FAILED`` when the
    scan aborted. The free-text ``error_message`` gives the future history page a
    human-readable explanation. ``data_quality_json`` is the optional DATA-001
    candle-quality receipt for this run.
    """
    # Imported lazily to avoid a circular import (result_contract imports storage).
    from backend.scanning.result_contract import normalize_secret_safe_json

    run.status = status
    run.finished_at = dt.datetime.now(dt.UTC)
    run.error_message = error_message
    # Defense in depth: the receipt is already redacted upstream, but everything
    # written to durable history goes through the shared secret-safe normalizer
    # too (it also masks any credential-shaped keys). ``cast`` only re-states the
    # type for mypy; it does not change the value.
    run.data_quality_json = cast(
        dict[str, Any] | None,
        normalize_secret_safe_json(data_quality_json)
        if data_quality_json is not None
        else None,
    )
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


# A run is "finalized" (a trustworthy shortlist worth comparing) only once it has
# completed. RUNNING is still mid-write and FAILED produced no usable shortlist.
_FINALIZED_SCAN_STATUSES = (ScanStatus.SUCCESS, ScanStatus.PARTIAL)


def get_latest_finalized_scan_runs(
    session: Session,
    *,
    screener_key: str,
    universe_key: str,
    limit: int = 2,
) -> list[ScanRun]:
    """Return newest completed comparison candidates for one screener/universe.

    JOB-003 compares the latest run against the immediately previous run. A
    RUNNING row is still being written and a FAILED row does not represent a
    trustworthy shortlist, so only SUCCESS and PARTIAL runs are eligible. The
    ordering mirrors ``get_latest_scan_runs``: newest timestamp first, then id as
    a deterministic tie-breaker for fast back-to-back runs.

    Beginner note:
    This builds a SQL ``SELECT ... WHERE ... ORDER BY ... LIMIT`` without writing
    raw SQL: ``.where(...)`` filters rows, ``.in_(...)`` matches any of the
    allowed statuses, ``.order_by(... .desc())`` sorts newest-first, and
    ``.limit(2)`` (the default) keeps just the two runs the comparison needs.
    Parameters are bound by SQLAlchemy, so the keys are never string-interpolated
    (no SQL injection).
    """
    stmt = (
        select(ScanRun)
        .where(
            ScanRun.screener_key == screener_key,
            ScanRun.universe_key == universe_key,
            ScanRun.status.in_(_FINALIZED_SCAN_STATUSES),
        )
        # Newest first; id breaks ties when two runs share a started_at timestamp.
        .order_by(ScanRun.started_at.desc(), ScanRun.id.desc())
        .limit(limit)
    )
    # ``scalars`` yields ScanRun objects (not (ScanRun,) tuples); materialize to a list.
    return list(session.scalars(stmt))


def list_finalized_scan_groups(session: Session) -> list[tuple[str, str]]:
    """Return screener/universe pairs that have at least one finalized run.

    The comparison page offers only pairs that can produce a latest run. Reading
    these options from history, rather than the live registry, keeps deleted or
    renamed screeners inspectable and prevents a broken screener module from
    taking down the read-only view.

    Beginner note:
    Selecting two columns plus ``.distinct()`` asks the database for the unique
    ``(screener_key, universe_key)`` combinations among finalized runs - exactly
    the dropdown options the page needs - in one cheap query, instead of loading
    every run and de-duplicating in Python.
    """
    stmt = (
        select(ScanRun.screener_key, ScanRun.universe_key)
        .where(ScanRun.status.in_(_FINALIZED_SCAN_STATUSES))
        .distinct()
        .order_by(ScanRun.screener_key.asc(), ScanRun.universe_key.asc())
    )
    # ``execute`` returns row tuples here (two columns); coerce each to plain str.
    return [(str(screener), str(universe)) for screener, universe in session.execute(stmt)]


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


def get_ai_evaluations(session: Session, run_id: int) -> list[AIEvaluation]:
    """Return AI evaluation receipts for one run in stable symbol/id order."""
    stmt = (
        select(AIEvaluation)
        .where(AIEvaluation.run_id == run_id)
        .order_by(AIEvaluation.symbol.asc(), AIEvaluation.id.asc())
    )
    return list(session.scalars(stmt))


# ---------------------------------------------------------------------------
# VALID-002 - forward-return validation helpers
# ---------------------------------------------------------------------------


def get_signals_needing_forward_returns(
    session: Session,
    *,
    horizons: Sequence[int],
    limit: int | None = None,
) -> list[ScanResult]:
    """Return signals with a missing or still-pending row for any horizon.

    Terminal rows (``computed`` / ``insufficient_data``) are skipped so the
    validation service can be re-run without rewriting completed measurements.
    The parent run is eager-loaded because its universe key drives instrument
    and benchmark resolution.
    """
    normalized_horizons = tuple(int(horizon) for horizon in horizons)
    if not normalized_horizons:
        return []

    needs_any_horizon = []
    for horizon in normalized_horizons:
        terminal_row_exists = exists().where(
            SignalForwardReturn.result_id == ScanResult.id,
            SignalForwardReturn.horizon_days == horizon,
            SignalForwardReturn.status != ForwardReturnStatus.PENDING,
        )
        needs_any_horizon.append(~terminal_row_exists)

    stmt = (
        select(ScanResult)
        .options(joinedload(ScanResult.run))
        .where(
            ScanResult.signal_date.is_not(None),
            or_(*needs_any_horizon),
        )
        .order_by(ScanResult.signal_date.asc(), ScanResult.id.asc())
    )
    if limit is not None:
        stmt = stmt.limit(limit)
    return list(session.scalars(stmt))


def upsert_forward_return(
    session: Session,
    *,
    result_id: int,
    point: ForwardReturnPoint,
    benchmark: BenchmarkLeg | None = None,
) -> SignalForwardReturn:
    """Insert or update one ``signal_forward_returns`` horizon row.

    The schema's unique ``(result_id, horizon_days)`` constraint is the durable
    idempotency contract; this helper mirrors that in ORM code so callers never
    append duplicate measurements on reruns.
    """
    stmt = select(SignalForwardReturn).where(
        SignalForwardReturn.result_id == result_id,
        SignalForwardReturn.horizon_days == point.horizon_days,
    )
    row = session.scalar(stmt)
    if row is None:
        row = SignalForwardReturn(
            result_id=result_id,
            horizon_days=point.horizon_days,
        )
        session.add(row)

    row.status = point.status
    row.entry_date = point.entry_date
    row.exit_date = point.exit_date
    row.entry_price = point.entry_price
    row.exit_price = point.exit_price
    row.forward_return_pct = point.forward_return_pct
    row.max_adverse_excursion_pct = point.max_adverse_excursion_pct
    row.max_favorable_excursion_pct = point.max_favorable_excursion_pct
    row.computed_at = (
        dt.datetime.now(dt.UTC)
        if point.status is not ForwardReturnStatus.PENDING
        else None
    )

    if benchmark is None:
        row.benchmark_key = None
        row.benchmark_entry_price = None
        row.benchmark_exit_price = None
        row.benchmark_return_pct = None
        row.excess_return_pct = None
    else:
        row.benchmark_key = benchmark.benchmark_key
        row.benchmark_entry_price = benchmark.entry_price
        row.benchmark_exit_price = benchmark.exit_price
        row.benchmark_return_pct = benchmark.return_pct
        row.excess_return_pct = (
            point.forward_return_pct - benchmark.return_pct
            if point.forward_return_pct is not None and benchmark.return_pct is not None
            else None
        )

    session.flush()
    return row


# ---------------------------------------------------------------------------
# VALID-003A - forward-return aggregate read helpers
# ---------------------------------------------------------------------------


def get_forward_return_metric_records(
    session: Session,
    *,
    screener_key: str | None = None,
    universe_key: str | None = None,
    horizon_days: int | None = None,
    signal_date_from: dt.date | None = None,
    signal_date_to: dt.date | None = None,
) -> list[ForwardReturnMetricRecord]:
    """Return joined forward-return rows for aggregate validation metrics.

    VALID-003A keeps raw SQL out of services and future UI code. This helper owns
    the ``scan_runs`` -> ``scan_results`` -> ``signal_forward_returns`` join and
    returns primitive DTOs that can be grouped safely after the session closes.
    Date filters are inclusive and deliberately use ``scan_results.signal_date``
    because the metrics answer "how did signals from this signal window perform?"

    Only ``SUCCESS``/``PARTIAL`` runs feed the metrics: a ``RUNNING`` run is still
    in flight and a ``FAILED`` run aborted before producing a trustworthy result
    set, so neither should colour a screener's performance numbers. ``run_started_at``
    is selected so callers can pick the most recent run when the same signal was
    re-measured across reruns (see ``summarize_validation_metrics`` de-duplication).
    """
    stmt = (
        select(
            ScanRun.id.label("run_id"),
            ScanRun.started_at.label("run_started_at"),
            ScanResult.id.label("result_id"),
            ScanRun.screener_key,
            ScanRun.universe_key,
            ScanResult.symbol,
            ScanResult.signal_date,
            SignalForwardReturn.horizon_days,
            SignalForwardReturn.status,
            SignalForwardReturn.forward_return_pct,
            SignalForwardReturn.excess_return_pct,
            SignalForwardReturn.max_adverse_excursion_pct,
            SignalForwardReturn.max_favorable_excursion_pct,
        )
        .join(ScanResult, ScanResult.run_id == ScanRun.id)
        .join(SignalForwardReturn, SignalForwardReturn.result_id == ScanResult.id)
        .where(ScanRun.status.in_((ScanStatus.SUCCESS, ScanStatus.PARTIAL)))
    )
    if screener_key is not None:
        stmt = stmt.where(ScanRun.screener_key == screener_key)
    if universe_key is not None:
        stmt = stmt.where(ScanRun.universe_key == universe_key)
    if horizon_days is not None:
        stmt = stmt.where(SignalForwardReturn.horizon_days == int(horizon_days))
    if signal_date_from is not None:
        stmt = stmt.where(ScanResult.signal_date >= signal_date_from)
    if signal_date_to is not None:
        stmt = stmt.where(ScanResult.signal_date <= signal_date_to)

    stmt = stmt.order_by(
        ScanRun.screener_key.asc(),
        ScanRun.universe_key.asc(),
        SignalForwardReturn.horizon_days.asc(),
        ScanResult.signal_date.asc(),
        ScanResult.id.asc(),
    )

    return [
        ForwardReturnMetricRecord(
            run_id=row.run_id,
            run_started_at=row.run_started_at,
            result_id=row.result_id,
            screener_key=row.screener_key,
            universe_key=row.universe_key,
            symbol=row.symbol,
            signal_date=row.signal_date,
            horizon_days=row.horizon_days,
            status=row.status,
            forward_return_pct=row.forward_return_pct,
            excess_return_pct=row.excess_return_pct,
            max_adverse_excursion_pct=row.max_adverse_excursion_pct,
            max_favorable_excursion_pct=row.max_favorable_excursion_pct,
        )
        for row in session.execute(stmt)
    ]


# ---------------------------------------------------------------------------
# OBS-003 — audit log + runtime config overrides
# ---------------------------------------------------------------------------


def create_audit_log_entry(
    session: Session,
    *,
    event: str,
    user_email: str | None = None,
    metadata: Mapping[str, Any] | None = None,
) -> AuditLog:
    """Insert one ``audit_logs`` row and return it.

    ``metadata`` is passed through ``normalize_secret_safe_json`` exactly like
    ``scan_runs.params_json`` so credential-named keys are masked, strings are
    redacted, and the stored blob is strict JSON. ``user_email`` is left as-is
    (``None`` for system actions such as the startup data refresh). ``flush``
    assigns ``entry.id`` without ending the caller's transaction.
    """
    from backend.scanning.result_contract import normalize_secret_safe_json

    entry = AuditLog(
        event=event,
        user_email=_as_optional_str(user_email),
        metadata_json=cast(
            dict[str, Any] | None,
            normalize_secret_safe_json(dict(metadata)) if metadata else None,
        ),
    )
    session.add(entry)
    session.flush()
    return entry


def get_recent_audit_logs(
    session: Session,
    limit: int = 100,
    *,
    event: str | None = None,
    user_email: str | None = None,
) -> list[AuditLog]:
    """Return the newest audit rows first, optionally filtered.

    The admin Audit log page calls this. ``limit`` keeps the query bounded as the
    trail grows. ``event`` is an exact match on the event name; ``user_email`` is
    a case-insensitive exact match (audit emails are stored lowercase, but a
    filter value typed in the UI may not be). Two rows written in the same
    millisecond keep a deterministic order via the primary-key tie-breaker.
    """
    stmt = select(AuditLog)
    if event:
        stmt = stmt.where(AuditLog.event == event)
    if user_email and user_email.strip():
        stmt = stmt.where(func.lower(AuditLog.user_email) == user_email.strip().lower())
    stmt = stmt.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(limit)
    return list(session.scalars(stmt))


def list_distinct_audit_events(session: Session) -> list[str]:
    """Return every event name present in the audit trail, sorted.

    The audit viewer's event filter uses this (not the constant list) so it only
    offers values that actually appear in history.
    """
    stmt = select(AuditLog.event).distinct().order_by(AuditLog.event.asc())
    return list(session.scalars(stmt))


def get_config_overrides(session: Session) -> dict[str, str]:
    """Return all persisted runtime-config overrides as a ``{key: value}`` dict.

    ``apply_config_overrides`` (in backend.admin) calls this on startup to seed
    ``os.environ``. Rows whose value is NULL are skipped — an absent override is
    the same as "use the environment default".
    """
    rows = session.scalars(select(AppConfig)).all()
    return {row.key: row.value for row in rows if row.value is not None}


def set_config_override(
    session: Session,
    *,
    key: str,
    value: str | None,
    updated_by: str | None,
) -> str | None:
    """Upsert one override row and return the PREVIOUS value (or None).

    Returning the old value lets the caller record a precise ``config_changed``
    audit entry (old -> new) without a second query. ``flush`` persists the row
    so the same transaction can read it back.
    """
    existing = session.get(AppConfig, key)
    previous = existing.value if existing is not None else None
    if existing is None:
        session.add(AppConfig(key=key, value=value, updated_by=updated_by))
    else:
        existing.value = value
        existing.updated_by = updated_by
        existing.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return previous


def _build_ai_evaluation(
    record: Mapping[str, Any] | Any,
) -> AIEvaluation:
    from backend.scanning.result_contract import normalize_secret_safe_json

    if isinstance(record, Mapping):
        raw = dict(record)
    elif is_dataclass(record) and not isinstance(record, type):
        raw = asdict(record)
    else:
        raise ValueError("AI evaluation record must be a mapping or dataclass.")

    normalized = normalize_secret_safe_json(raw)
    if not isinstance(normalized, dict):
        raise ValueError("AI evaluation normalization must produce a JSON object.")

    symbol = str(normalized.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("AI evaluation requires a non-blank symbol.")

    outcome = str(normalized.get("outcome") or "").strip().lower()
    if outcome not in _AI_EVALUATION_OUTCOMES:
        raise ValueError("AI evaluation outcome must be approved, rejected, or error.")

    confidence = _as_decimal(normalized.get("confidence"))
    if confidence is not None and not Decimal("0") <= confidence <= Decimal("10"):
        raise ValueError("AI evaluation confidence must be between 0 and 10.")

    verdict = _as_optional_str(
        normalized.get("verdict_label", normalized.get("verdict"))
    )
    decision_reason = _as_optional_str(normalized.get("decision_reason"))
    provenance_value = normalized.get("provenance_json", normalized.get("provenance"))
    provenance = _validated_ai_provenance(
        provenance_value,
        outcome=outcome,
        verdict=verdict,
        confidence=confidence,
        decision_reason=decision_reason,
    )
    verdict = cast(str | None, provenance["verdict"])
    confidence = _as_decimal(provenance["confidence"])
    decision_reason = cast(str | None, provenance["decision_reason"])

    verdict_value = normalized.get("validated_verdict_json", {})
    if not isinstance(verdict_value, Mapping):
        raise ValueError("validated_verdict_json must be a mapping.")
    validated_verdict = dict(verdict_value)
    _validate_verdict_json_receipt_fields(
        validated_verdict,
        symbol=symbol,
        outcome=outcome,
        verdict=verdict,
        confidence=confidence,
        decision_reason=decision_reason,
        model_name=cast(str, provenance["model_name"]),
    )
    if verdict is not None:
        validated_verdict.setdefault("verdict", verdict)
    if confidence is not None:
        validated_verdict.setdefault("confidence", str(confidence))
    if decision_reason is not None:
        validated_verdict.setdefault("decision_reason", decision_reason)

    created_at = _as_utc_datetime(normalized.get("created_at"), required=False)
    return AIEvaluation(
        symbol=symbol,
        signal_date=_as_date(normalized.get("signal_date")),
        outcome=outcome,
        verdict_label=verdict,
        confidence=confidence,
        model_name=cast(str, provenance["model_name"]),
        prompt_version=cast(str, provenance["prompt_version"]),
        validated_verdict_json=validated_verdict,
        provenance_json=provenance,
        created_at=created_at or dt.datetime.now(dt.UTC),
    )


def _validate_verdict_json_receipt_fields(
    verdict_json: Mapping[str, Any],
    *,
    symbol: str,
    outcome: str,
    verdict: str | None,
    confidence: Decimal | None,
    decision_reason: str | None,
    model_name: str,
) -> None:
    """Reject model-output fields that contradict the trusted audit receipt."""
    if "symbol" in verdict_json and str(verdict_json["symbol"]).strip() != symbol:
        raise ValueError(
            "validated_verdict_json symbol must match the evaluation symbol."
        )
    if (
        "model_used" in verdict_json
        and str(verdict_json["model_used"]).strip() != model_name
    ):
        raise ValueError(
            "validated_verdict_json model_used must match AI provenance."
        )
    if (
        "verdict" in verdict_json
        and _as_optional_str(verdict_json["verdict"]) != verdict
    ):
        raise ValueError(
            "validated_verdict_json verdict must match AI provenance."
        )
    if (
        "confidence" in verdict_json
        and _as_decimal(verdict_json["confidence"]) != confidence
    ):
        raise ValueError(
            "validated_verdict_json confidence must match AI provenance."
        )
    if (
        "decision_reason" in verdict_json
        and _as_optional_str(verdict_json["decision_reason"]) != decision_reason
    ):
        raise ValueError(
            "validated_verdict_json decision_reason must match AI provenance."
        )
    if "approved" in verdict_json:
        approved = verdict_json["approved"]
        if not isinstance(approved, bool) or approved != (outcome == "approved"):
            raise ValueError(
                "validated_verdict_json approved must match the evaluation outcome."
            )


def _validated_ai_provenance(
    value: Any,
    *,
    outcome: str,
    verdict: str | None,
    confidence: Decimal | None,
    decision_reason: str | None,
) -> dict[str, Any]:
    from backend.scanning.result_contract import sanitize_evidence_url

    if not isinstance(value, Mapping):
        raise ValueError("AI evaluation provenance must be a mapping.")
    provenance = dict(value)

    model_name = str(provenance.get("model_name") or "").strip()
    prompt_version = str(provenance.get("prompt_version") or "").strip()
    if not model_name or not prompt_version:
        raise ValueError("AI provenance requires model_name and prompt_version.")

    prompt_sha256 = _full_sha256(provenance.get("prompt_sha256"), "prompt_sha256")
    generated_at = _as_utc_datetime(provenance.get("generated_at"), required=True)
    cache_hit = provenance.get("cache_hit")
    if not isinstance(cache_hit, bool):
        raise ValueError("AI provenance cache_hit must be boolean.")

    evidence_value = provenance.get("evidence_references", [])
    if not isinstance(evidence_value, list):
        raise ValueError("AI provenance evidence_references must be a list.")
    evidence: list[dict[str, Any]] = []
    for item in evidence_value:
        if not isinstance(item, Mapping):
            raise ValueError("Each evidence reference must be a mapping.")
        source_label = str(item.get("source_label") or "").strip()
        if not source_label:
            raise ValueError("Evidence reference requires a source_label.")
        evidence.append(
            {
                "source_label": source_label,
                "sanitized_url": sanitize_evidence_url(item.get("sanitized_url")),
                "sha256": _full_sha256(item.get("sha256"), "evidence sha256"),
            }
        )

    input_context_hash = provenance.get("input_context_hash")
    normalized_context_hash = (
        _full_sha256(input_context_hash, "input_context_hash")
        if input_context_hash is not None
        else None
    )
    provenance_verdict = _as_optional_str(provenance.get("verdict")) or verdict
    provenance_confidence = _as_decimal(provenance.get("confidence"))
    if provenance_confidence is None:
        provenance_confidence = confidence
    if provenance_confidence is not None and not (
        Decimal("0") <= provenance_confidence <= Decimal("10")
    ):
        raise ValueError("AI evaluation confidence must be between 0 and 10.")
    provenance_reason = (
        _as_optional_str(provenance.get("decision_reason")) or decision_reason
    )
    if verdict is not None and provenance_verdict != verdict:
        raise ValueError("AI provenance verdict must match the evaluation verdict.")
    if confidence is not None and provenance_confidence != confidence:
        raise ValueError("AI provenance confidence must match the evaluation confidence.")
    if decision_reason is not None and provenance_reason != decision_reason:
        raise ValueError(
            "AI provenance decision_reason must match the evaluation decision_reason."
        )
    if outcome != "error" and (
        provenance_verdict is None
        or provenance_confidence is None
        or provenance_reason is None
    ):
        raise ValueError(
            "Approved and rejected AI evaluations require verdict, confidence, "
            "and decision_reason."
        )
    return {
        "model_name": model_name,
        "prompt_version": prompt_version,
        "prompt_sha256": prompt_sha256,
        "generated_at": cast(dt.datetime, generated_at).isoformat(),
        "cache_hit": cache_hit,
        "verdict": provenance_verdict,
        "confidence": (
            str(provenance_confidence)
            if provenance_confidence is not None
            else None
        ),
        "decision_reason": provenance_reason,
        "evidence_references": evidence,
        "input_context_hash": normalized_context_hash,
    }


def _full_sha256(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip().lower()
    if not _SHA256_RE.fullmatch(normalized):
        raise ValueError(f"AI provenance {field_name} must be a full SHA-256.")
    return normalized


def _as_utc_datetime(value: Any, *, required: bool) -> dt.datetime | None:
    if _is_missing(value):
        if required:
            raise ValueError("AI provenance generated_at is required.")
        return None
    if isinstance(value, dt.datetime):
        parsed = value
    else:
        try:
            parsed = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("AI timestamp must be valid ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != dt.timedelta(0):
        raise ValueError("AI timestamp must be timezone-aware UTC.")
    return parsed.astimezone(dt.UTC)


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
