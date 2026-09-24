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

from sqlalchemy import and_, case, exists, func, insert, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage.models import (
    AIEvaluation,
    AppConfig,
    AuditLog,
    CandleRepairRun,
    ForwardReturnStatus,
    ScanResult,
    ScanRun,
    ScanStatus,
    SignalForwardReturn,
    UniverseHealthSnapshot,
    UserRole,
)

if TYPE_CHECKING:
    from backend.validation.benchmarks import BenchmarkLeg
    from backend.validation.forward_return import ForwardReturnPoint

_AI_EVALUATION_OUTCOMES = frozenset({"approved", "rejected", "error"})
_UNIVERSE_OBSERVATION_STATUSES = frozenset(
    {"valid", "missing", "unreadable", "legacy_unknown"}
)
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


@dataclass(frozen=True)
class BenchmarkForwardReturnWork:
    """Stored stock facts needed to retry only one benchmark horizon.

    ``computed_at`` is when the stock leg became terminal; the worker stops
    retrying an unavailable benchmark once that is older than its grace period.
    """

    horizon_days: int
    entry_date: dt.date | None
    exit_date: dt.date | None
    forward_return_pct: Decimal | None
    computed_at: dt.datetime | None = None


@dataclass(frozen=True)
class ForwardReturnWorkItem:
    """Detached work for one signal selected by the validation worker.

    Beginner note:
    ORM objects remain connected to their database session. This value object
    copies only stable scalar facts so the service can close its read
    transaction before loading universe files or calling a market-data provider.
    ``stock_horizons`` need the symbol history; ``benchmark_horizons`` already
    have terminal stock facts and therefore must never refetch or rewrite them.
    """

    result_id: int
    symbol: str
    signal_date: dt.date
    universe_key: str
    stock_horizons: tuple[int, ...]
    benchmark_horizons: tuple[BenchmarkForwardReturnWork, ...]


def get_scan_run(session: Session, run_id: int) -> ScanRun | None:
    """Return one scan run by primary key, or ``None`` when it does not exist.

    Beginner note:
    ``Session.get`` is a database lookup even though it does not look like a SQL
    statement. Keeping that call here means services can own a transaction and
    still avoid knowing which ORM class or lookup primitive backs a scan run.
    """
    return session.get(ScanRun, run_id)


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


def get_scan_runs(session: Session, run_ids: Sequence[int]) -> list[ScanRun]:
    """Return the ``ScanRun`` rows for the given ids (ALERT-001 summary reads).

    The daily-scan notification totals the universe size (``symbols_scanned``)
    across a job's runs. Unknown ids are simply absent from the result.
    """
    ids = [int(run_id) for run_id in run_ids]
    if not ids:
        return []
    stmt = select(ScanRun).where(ScanRun.id.in_(ids))
    return list(session.scalars(stmt))


def _finite_decimal(value: Any) -> Decimal | None:
    """Parse only finite numeric values for score ordering.

    Stored raw JSON is intentionally flexible, so ``confidence`` may be a
    number, a numeric string, ``None``, or junk. The repository should make bad
    values sort as unscored instead of letting ``Decimal('NaN')`` or a string
    parsing error make the notification job fail.
    """
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed.is_finite() else None


def _rank_score_and_source(result: ScanResult) -> tuple[Decimal | None, str | None]:
    """Return the score used for ALERT-001 ranking and its source.

    ``final_score`` is the future canonical ranking model output, so it always
    outranks the generic confidence fallback. The fallback only makes today's
    alerts more useful while RANK-002 is not yet merged.
    """
    final_score = _finite_decimal(result.final_score)
    if final_score is not None:
        return final_score, "final_score"
    raw_result = result.raw_result_json
    if isinstance(raw_result, Mapping):
        confidence = _finite_decimal(raw_result.get("confidence"))
        if confidence is not None:
            return confidence, "confidence"
    return None, None


def _top_result_sort_key(result: ScanResult) -> tuple[int, Decimal, str, int]:
    """Sort final-scored, confidence-scored, then unscored rows deterministically."""
    score, source = _rank_score_and_source(result)
    if source == "final_score":
        source_order = 0
    elif source == "confidence":
        source_order = 1
    else:
        source_order = 2
    # For scored rows, negating the Decimal gives descending numeric order while
    # still using Python's normal ascending tuple sort. Unscored rows share the
    # same zero score key and then fall back to symbol/id for stable output.
    score_order = -score if score is not None else Decimal("0")
    result_id = int(result.id or 0)
    return (source_order, score_order, str(result.symbol), result_id)


def get_top_ranked_results(
    session: Session, run_ids: Sequence[int], *, limit: int = 10
) -> list[ScanResult]:
    """Return the top shortlisted rows across runs for ALERT-001 notifications.

    Ranking rule:
    1. rows with ``final_score`` (canonical RANK-* score), highest first;
    2. rows without ``final_score`` but with numeric raw ``confidence``;
    3. completely unscored rows, stable by symbol/id.

    The fallback is done in Python because ``raw_result_json`` is portable JSON:
    SQLite and Postgres expose different JSON casting syntax, while Python gives
    one deterministic rule for both local and deployed databases.
    """
    ids = [int(run_id) for run_id in run_ids]
    if not ids or limit <= 0:
        return []
    stmt = (
        select(ScanResult)
        .where(ScanResult.run_id.in_(ids))
        .order_by(ScanResult.symbol.asc(), ScanResult.id.asc())
    )
    rows = list(session.scalars(stmt))
    rows.sort(key=_top_result_sort_key)
    return rows[:limit]


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


def get_forward_return_work_items(
    session: Session,
    *,
    horizons: Sequence[int],
    limit: int | None = None,
) -> list[ForwardReturnWorkItem]:
    """Return fairly ordered detached work for unresolved stock or benchmark legs.

    Missing horizons use ``ScanResult.created_at`` as their effective attempt
    time; persisted unresolved rows use ``last_attempted_at`` when available.
    Sorting by the oldest effective time rotates bounded batches fairly, with
    signal date and id as deterministic ties.

    Args:
        session: Caller-owned short read transaction; no commit is performed.
        horizons: Requested horizon counts already validated by the worker.
            Repeated counts are collapsed in caller order; empty means no work.
        limit: Maximum distinct signals, or None for all eligible signals.

    Returns:
        Frozen detached work items ordered by oldest effective unresolved
        attempt, signal date and ID. Each signal appears once with separate
        stock and benchmark-only horizons, usable after its session closes.

    Beginner note:
    The limit counts signals, not horizon rows. One chosen signal carries all of
    its requested unresolved horizons, so a batch never processes the same
    signal twice or partially hides work behind a row-level SQL limit.

    Selection, fairness ordering and the limit all run in SQL, and only the
    chosen signals' scalar columns are read afterwards. Scan history grows
    every day while a batch stays a few hundred signals, so loading every
    stored result (with its raw JSON) to discard it in Python does not scale.
    """
    normalized_horizons = tuple(dict.fromkeys(int(horizon) for horizon in horizons))
    if not normalized_horizons:
        return []

    sfr = SignalForwardReturn
    # A stored row still needs work while its stock leg is pending, or while a
    # terminal stock leg waits for its benchmark (benchmark-only retry).
    unresolved = or_(sfr.status == ForwardReturnStatus.PENDING, sfr.benchmark_retry_pending.is_(True))
    attempt_time = func.coalesce(sfr.last_attempted_at, ScanResult.created_at)
    per_signal = (
        select(
            sfr.result_id.label("result_id"),
            func.count(sfr.id).label("row_count"),
            func.count(case((unresolved, 1))).label("unresolved_count"),
            func.min(case((unresolved, attempt_time))).label("oldest_attempt"),
        )
        .join(ScanResult, ScanResult.id == sfr.result_id)
        .where(sfr.horizon_days.in_(normalized_horizons))
        .group_by(sfr.result_id)
        .subquery()
    )
    # (result_id, horizon_days) is unique, so fewer rows than requested
    # horizons means at least one horizon was never attempted at all.
    has_missing = func.coalesce(per_signal.c.row_count, 0) < len(normalized_horizons)
    oldest = per_signal.c.oldest_attempt
    # A never-attempted horizon counts from the signal's creation time; the
    # effective time is the earlier of that and the oldest unresolved attempt.
    effective_attempt = case(
        (and_(has_missing, or_(oldest.is_(None), ScanResult.created_at <= oldest)), ScanResult.created_at),
        else_=oldest,
    )
    selection = (
        select(ScanResult.id, ScanResult.symbol, ScanResult.signal_date, ScanRun.universe_key)
        .join(ScanRun, ScanRun.id == ScanResult.run_id)
        .outerjoin(per_signal, per_signal.c.result_id == ScanResult.id)
        .where(ScanResult.signal_date.is_not(None), or_(has_missing, per_signal.c.unresolved_count > 0))
        .order_by(effective_attempt, ScanResult.signal_date, ScanResult.id)
    )
    if limit is not None:
        selection = selection.limit(limit)
    chosen = session.execute(selection).all()
    if not chosen:
        return []

    stored = session.execute(
        select(
            sfr.result_id, sfr.horizon_days, sfr.status, sfr.benchmark_retry_pending,
            sfr.entry_date, sfr.exit_date, sfr.forward_return_pct, sfr.computed_at,
        ).where(sfr.result_id.in_([signal.id for signal in chosen]), sfr.horizon_days.in_(normalized_horizons))
    ).all()
    rows_by_signal: dict[int, dict[int, Any]] = {}
    for stored_row in stored:
        rows_by_signal.setdefault(stored_row.result_id, {})[stored_row.horizon_days] = stored_row

    work_items: list[ForwardReturnWorkItem] = []
    for signal in chosen:
        rows = rows_by_signal.get(signal.id, {})
        stock_horizons: list[int] = []
        benchmark_horizons: list[BenchmarkForwardReturnWork] = []
        for horizon in normalized_horizons:
            row = rows.get(horizon)
            if row is None or row.status is ForwardReturnStatus.PENDING:
                stock_horizons.append(horizon)
            elif row.benchmark_retry_pending:
                benchmark_horizons.append(
                    BenchmarkForwardReturnWork(
                        horizon_days=horizon,
                        entry_date=row.entry_date,
                        exit_date=row.exit_date,
                        forward_return_pct=row.forward_return_pct,
                        computed_at=row.computed_at,
                    )
                )
        # A concurrent worker may finish a signal between the two reads.
        if not stock_horizons and not benchmark_horizons:
            continue
        work_items.append(
            ForwardReturnWorkItem(
                result_id=signal.id,
                symbol=signal.symbol,
                signal_date=cast(dt.date, signal.signal_date),
                universe_key=signal.universe_key,
                stock_horizons=tuple(stock_horizons),
                benchmark_horizons=tuple(benchmark_horizons),
            )
        )
    return work_items


def mark_forward_return_attempted(
    session: Session,
    *,
    result_id: int,
    horizons: Sequence[int],
    attempted_at: dt.datetime | None = None,
) -> None:
    """Record that a signal's unresolved horizons were attempted, without any facts.

    Args:
        session: Caller-owned short write transaction; this helper never commits.
        result_id: Stored signal whose processing failed.
        horizons: The horizons that were being worked on.
        attempted_at: UTC attempt time, defaulting to now.

    Beginner note:
    When a signal's processing fails, its measurement transaction rolls back,
    so without this receipt its attempt time never changes and oldest-first
    selection hands the same failing signal to every future batch, starving
    the queue. Missing horizons get an empty PENDING row; existing pending or
    benchmark-retry rows only get a new ``last_attempted_at``. Terminal stock
    facts and benchmark values are never read or written here.
    """
    attempted = attempted_at or dt.datetime.now(dt.UTC)
    wanted = tuple(dict.fromkeys(int(horizon) for horizon in horizons))
    existing = set(
        session.scalars(
            select(SignalForwardReturn.horizon_days).where(
                SignalForwardReturn.result_id == result_id,
                SignalForwardReturn.horizon_days.in_(wanted),
            )
        )
    )
    for horizon in wanted:
        if horizon in existing:
            continue
        try:
            with session.begin_nested():
                session.execute(
                    insert(SignalForwardReturn).values(
                        result_id=result_id,
                        horizon_days=horizon,
                        status=ForwardReturnStatus.PENDING,
                        last_attempted_at=attempted,
                        created_at=attempted,
                    )
                )
        except IntegrityError:
            # A concurrent writer created the row; the update below still
            # bumps it if it remains unresolved.
            pass
    session.execute(
        update(SignalForwardReturn)
        .where(
            SignalForwardReturn.result_id == result_id,
            SignalForwardReturn.horizon_days.in_(wanted),
            or_(
                SignalForwardReturn.status == ForwardReturnStatus.PENDING,
                SignalForwardReturn.benchmark_retry_pending.is_(True),
            ),
        )
        .values(last_attempted_at=attempted)
    )


def upsert_forward_return(
    session: Session,
    *,
    result_id: int,
    point: ForwardReturnPoint,
    benchmark: BenchmarkLeg | None = None,
    benchmark_retry_pending: bool = False,
    attempted_at: dt.datetime | None = None,
) -> SignalForwardReturn:
    """Insert missing work or conditionally update a still-pending stock receipt.

    Args:
        session: Caller-owned transaction; this helper never commits it.
        result_id: Stored signal identity.
        point: Proposed stock measurement or unresolved status.
        benchmark: Optional aligned index measurement.
        benchmark_retry_pending: Whether configured index data still needs work.
        attempted_at: UTC attempt time, defaulting to now.

    Returns:
        The durable row, including a concurrent terminal winner if one exists.

    Beginner note:
        A selection-time Python check is insufficient: another worker can finish
        while this worker fetches candles. The UPDATE itself requires PENDING.
        Unique insert conflicts roll back only a savepoint, then conditionally
        retry that same UPDATE. Completed dates, prices, returns, excursions,
        benchmark facts and computed_at can therefore never be erased by retries.
    """
    attempted = attempted_at or dt.datetime.now(dt.UTC)
    key_predicates = (
        SignalForwardReturn.result_id == result_id,
        SignalForwardReturn.horizon_days == point.horizon_days,
    )
    values: dict[str, object] = {
        "status": point.status,
        "entry_date": point.entry_date,
        "exit_date": point.exit_date,
        "entry_price": point.entry_price,
        "exit_price": point.exit_price,
        "forward_return_pct": point.forward_return_pct,
        "max_adverse_excursion_pct": point.max_adverse_excursion_pct,
        "max_favorable_excursion_pct": point.max_favorable_excursion_pct,
        "computed_at": (
            attempted
            if point.status is not ForwardReturnStatus.PENDING
            else None
        ),
        "last_attempted_at": attempted,
        "benchmark_retry_pending": benchmark_retry_pending,
    }

    if benchmark is None:
        values.update(
            benchmark_key=None,
            benchmark_entry_price=None,
            benchmark_exit_price=None,
            benchmark_return_pct=None,
            excess_return_pct=None,
        )
    else:
        values.update(
            benchmark_key=benchmark.benchmark_key,
            benchmark_entry_price=benchmark.entry_price,
            benchmark_exit_price=benchmark.exit_price,
            benchmark_return_pct=benchmark.return_pct,
            excess_return_pct=(
                point.forward_return_pct - benchmark.return_pct
                if point.forward_return_pct is not None and benchmark.return_pct is not None
                else None
            ),
        )

    # The status predicate is the concurrency guard. Even if another worker
    # terminalizes the row after selection, this UPDATE cannot touch any stock
    # receipt field once status is no longer pending.
    updated = session.execute(
        update(SignalForwardReturn)
        .where(*key_predicates, SignalForwardReturn.status == ForwardReturnStatus.PENDING)
        .values(**values)
    )
    if cast(CursorResult[Any], updated).rowcount == 0:
        existing = session.scalar(select(SignalForwardReturn).where(*key_predicates))
        if existing is None:
            try:
                with session.begin_nested():
                    session.execute(
                        insert(SignalForwardReturn).values(
                            result_id=result_id,
                            horizon_days=point.horizon_days,
                            created_at=attempted,
                            **values,
                        )
                    )
            except IntegrityError:
                # A concurrent insert won the unique key. Update only if its row
                # is still pending; a terminal winner remains immutable.
                session.execute(
                    update(SignalForwardReturn)
                    .where(
                        *key_predicates,
                        SignalForwardReturn.status == ForwardReturnStatus.PENDING,
                    )
                    .values(**values)
                )

    session.flush()
    row = session.scalar(select(SignalForwardReturn).where(*key_predicates))
    if row is None:  # pragma: no cover - defensive invariant after insert/update
        raise RuntimeError("forward-return persistence produced no row")
    return row


def update_forward_return_benchmark(
    session: Session,
    *,
    result_id: int,
    horizon_days: int,
    benchmark: BenchmarkLeg | None,
    retry_pending: bool,
    attempted_at: dt.datetime | None = None,
) -> bool:
    """Update only benchmark retry fields on an existing terminal stock row.

    Args:
        session: Caller-owned write transaction; this helper never commits.
        result_id: Stored signal identity.
        horizon_days: Existing computed horizon being retried.
        benchmark: Aligned result, or None for intentionally absent configuration.
        retry_pending: True for unavailable configured work; False after success
            or when no benchmark is configured.
        attempted_at: UTC retry time; defaults to the current time.

    Returns:
        True if a computed row with an active retry and missing benchmark return
        was updated; False for a no-op, including a concurrent successful winner.
        Stock status, dates, prices, return, excursions and computed_at are never
        modified, regardless of the proposed benchmark outcome.

    Beginner note:
    This statement intentionally omits every stock column. A benchmark provider
    can fail today and recover tomorrow without changing the historical entry,
    exit, return, excursions, status, or ``computed_at`` already proven for the
    stock. The terminal predicate also prevents attaching a benchmark to a stock
    leg that has not finished. The missing-return and retry predicates protect
    a benchmark already completed by another worker after work selection.
    """
    values: dict[str, object] = {
        "last_attempted_at": attempted_at or dt.datetime.now(dt.UTC),
        "benchmark_retry_pending": retry_pending,
    }
    if benchmark is None:
        values.update(
            benchmark_key=None,
            benchmark_entry_price=None,
            benchmark_exit_price=None,
            benchmark_return_pct=None,
            excess_return_pct=None,
        )
    else:
        row_return = session.scalar(
            select(SignalForwardReturn.forward_return_pct).where(
                SignalForwardReturn.result_id == result_id,
                SignalForwardReturn.horizon_days == horizon_days,
                SignalForwardReturn.status == ForwardReturnStatus.COMPUTED,
                SignalForwardReturn.benchmark_retry_pending.is_(True),
                SignalForwardReturn.benchmark_return_pct.is_(None),
            )
        )
        values.update(
            benchmark_key=benchmark.benchmark_key,
            benchmark_entry_price=benchmark.entry_price,
            benchmark_exit_price=benchmark.exit_price,
            benchmark_return_pct=benchmark.return_pct,
            excess_return_pct=(
                row_return - benchmark.return_pct
                if row_return is not None and benchmark.return_pct is not None
                else None
            ),
        )
    result = session.execute(
        update(SignalForwardReturn)
        .where(
            SignalForwardReturn.result_id == result_id,
            SignalForwardReturn.horizon_days == horizon_days,
            SignalForwardReturn.status == ForwardReturnStatus.COMPUTED,
            SignalForwardReturn.benchmark_retry_pending.is_(True),
            SignalForwardReturn.benchmark_return_pct.is_(None),
        )
        .values(**values)
    )
    return bool(cast(CursorResult[Any], result).rowcount)


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


def create_candle_repair_run(
    session: Session,
    *,
    trigger: str,
) -> CandleRepairRun:
    """Open a DATA-002 repair-run header row and return it.

    Called *before* the pass starts, so a crash mid-repair still leaves a row with
    ``finished_at IS NULL`` — which is exactly the evidence an operator wants when
    the morning cleanup died halfway through. ``flush`` assigns ``run.id`` without
    ending the caller's transaction (the caller owns it, per REFACTOR-002).
    """
    run = CandleRepairRun(trigger=str(trigger))
    session.add(run)
    session.flush()
    return run


def finish_candle_repair_run(
    session: Session,
    run: CandleRepairRun,
    *,
    symbols_checked: int,
    symbols_repaired: int,
    symbols_unrepairable: int,
    rows_removed: int,
    refetch_count: int,
    receipt: Mapping[str, Any] | None = None,
) -> None:
    """Stamp the finishing counts and receipt onto an open repair run.

    ``receipt`` goes through ``normalize_secret_safe_json`` for the same reason
    ``audit_logs.metadata_json`` does: the builder already redacts, and this is the
    defence-in-depth hop that guarantees whatever lands in the JSON column is
    strict, secret-free JSON.
    """
    from backend.scanning.result_contract import normalize_secret_safe_json

    run.finished_at = dt.datetime.now(dt.UTC)
    run.symbols_checked = int(symbols_checked)
    run.symbols_repaired = int(symbols_repaired)
    run.symbols_unrepairable = int(symbols_unrepairable)
    run.rows_removed = int(rows_removed)
    run.refetch_count = int(refetch_count)
    run.receipt_json = cast(
        dict[str, Any] | None,
        normalize_secret_safe_json(dict(receipt)) if receipt else None,
    )
    session.add(run)
    session.flush()


def get_latest_candle_repair_run(session: Session) -> CandleRepairRun | None:
    """Return the newest repair pass, or None when none has run yet.

    Admin health calls this. The primary-key tie-breaker keeps the order
    deterministic when two passes start in the same millisecond.
    """
    stmt = (
        select(CandleRepairRun)
        .order_by(CandleRepairRun.started_at.desc(), CandleRepairRun.id.desc())
        .limit(1)
    )
    return session.scalars(stmt).first()


def record_universe_health_snapshots(
    session: Session,
    snapshots: Sequence[Mapping[str, Any]],
) -> list[UniverseHealthSnapshot]:
    """Append one OBS-004 mapping-health row per universe and return them.

    Beginner note:
    Rows are appended, never updated. The comparison only ever reads the newest
    row per universe, and keeping the history means an operator can answer "when
    did this symbol drop out?" - which is the question that always follows the
    alert. ``flush`` assigns the ids without ending the caller's transaction
    (the caller owns it, per REFACTOR-002).

    Each mapping needs ``universe_key``, ``total_rows``, ``mapped_rows`` and
    ``unmapped_rows``; ``unmapped_symbols`` is optional and stored as JSON.
    Failed reads are persisted with an explicit status for diagnosis, while the
    read helper below deliberately excludes them from baseline authority.
    """
    invalid_statuses = {
        str(snapshot.get("observation_status", "valid"))
        for snapshot in snapshots
    } - _UNIVERSE_OBSERVATION_STATUSES
    if invalid_statuses:
        raise ValueError(f"Unsupported universe observation status: {sorted(invalid_statuses)!r}")

    rows = [
        UniverseHealthSnapshot(
            universe_key=str(snapshot["universe_key"]),
            observation_status=str(snapshot.get("observation_status", "valid")),
            total_rows=int(snapshot.get("total_rows", 0)),
            mapped_rows=int(snapshot.get("mapped_rows", 0)),
            unmapped_rows=int(snapshot.get("unmapped_rows", 0)),
            unmapped_symbols_json=(
                {
                    "symbols": list(snapshot["unmapped_symbols"]),
                    "truncated": bool(snapshot.get("unmapped_symbols_truncated", False)),
                    "membership_complete": bool(snapshot.get("membership_complete", False)),
                }
                if snapshot.get("unmapped_symbols") is not None
                else None
            ),
        )
        for snapshot in snapshots
    ]
    session.add_all(rows)
    session.flush()
    return rows


def get_latest_universe_health_snapshots(
    session: Session,
) -> dict[str, UniverseHealthSnapshot]:
    """Return the newest valid mapping-health row per universe.

    Beginner note:
    This is the baseline the daily job compares today's counts against. A window
    function ranks valid rows inside each universe in the database, so Python
    receives one row per key even when years of append-only history exist. The
    primary-key tie-breaker keeps the winner deterministic when two checks share
    a timestamp.

    Beginner note:
    Missing, unreadable, and migrated ``legacy_unknown`` rows remain in history
    as operational evidence, but do not replace a previously valid baseline.
    Otherwise a temporary read failure recorded as zero could make the next good
    read look like a false mapping regression.

    An empty result means the check has never run - the caller must treat that as
    "no baseline", not as "zero unmapped", or the very first run would alert on
    every pre-existing unmapped symbol.
    """
    ranked = (
        select(
            UniverseHealthSnapshot.id.label("snapshot_id"),
            func.row_number()
            .over(
                partition_by=UniverseHealthSnapshot.universe_key,
                order_by=(
                    UniverseHealthSnapshot.captured_at.desc(),
                    UniverseHealthSnapshot.id.desc(),
                ),
            )
            .label("baseline_rank"),
        )
        .where(UniverseHealthSnapshot.observation_status == "valid")
        .subquery()
    )
    stmt = (
        select(UniverseHealthSnapshot)
        .join(ranked, UniverseHealthSnapshot.id == ranked.c.snapshot_id)
        .where(ranked.c.baseline_rank == 1)
        .order_by(
            UniverseHealthSnapshot.captured_at.desc(),
            UniverseHealthSnapshot.id.desc(),
        )
    )
    return {row.universe_key: row for row in session.scalars(stmt)}


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


# ---------------------------------------------------------------------------
# AUTH-003 — durable role assignments (user_roles)
# ---------------------------------------------------------------------------
# Email is the primary key, so every helper normalizes to the same lowercase form
# the auth gate uses. That keeps a single row per person even if a caller passes
# "Boss@Example.COM" — no case-variant duplicates can sneak in.


def get_user_role(session: Session, email: str) -> str | None:
    """Return the stored role name for ``email``, or ``None`` when unassigned."""
    row = session.get(UserRole, email.strip().lower())
    return row.role if row is not None else None


def set_user_role(
    session: Session,
    *,
    email: str,
    role: str,
    assigned_by: str | None,
) -> str | None:
    """Upsert one role assignment and return the PREVIOUS role (or ``None``).

    Returning the old value lets the caller record a precise ``role_changed``
    audit entry (old -> new) without a second query. Mirrors ``set_config_override``.
    The ``role`` string is constrained by the model's CHECK; the admin service
    validates it against the ``Role`` enum before calling here.
    """
    normalized = email.strip().lower()
    existing = session.get(UserRole, normalized)
    previous = existing.role if existing is not None else None
    if existing is None:
        session.add(UserRole(email=normalized, role=role, assigned_by=assigned_by))
    else:
        existing.role = role
        existing.assigned_by = assigned_by
        existing.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return previous


def delete_user_role(session: Session, email: str) -> str | None:
    """Delete a role assignment and return the PREVIOUS role (``None`` if absent).

    Because a ``user_roles`` row also authorizes sign-in (AUTH-003 entry widening),
    deleting a row both removes the role and revokes table-granted access — the
    revoke path the admin Roles page exposes.
    """
    existing = session.get(UserRole, email.strip().lower())
    if existing is None:
        return None
    previous = existing.role
    session.delete(existing)
    session.flush()
    return previous


def list_user_roles(session: Session) -> list[UserRole]:
    """Return all role assignments, sorted by email for a stable admin table."""
    stmt = select(UserRole).order_by(UserRole.email.asc())
    return list(session.scalars(stmt))


def count_user_role_admins(session: Session) -> int:
    """Return how many rows currently assign the ``admin`` role.

    The admin Roles page combines this with the env ``ADMIN_EMAILS`` floor to
    refuse a change that would leave zero effective admins (last-admin guard).
    """
    stmt = select(func.count()).select_from(UserRole).where(UserRole.role == "admin")
    return int(session.scalar(stmt) or 0)


def list_user_role_admins_for_update(session: Session) -> list[UserRole]:
    """Lock and return current table-admin rows in deterministic email order.

    Postgres honors ``FOR UPDATE`` and makes concurrent demotion/revocation
    transactions serialize on the same rows. SQLite ignores the clause, but its
    write transaction/snapshot rules still prevent both stale writers from
    committing; keeping one query shape preserves the repository abstraction.
    """
    stmt = (
        select(UserRole)
        .where(UserRole.role == "admin")
        .order_by(UserRole.email.asc())
        .with_for_update()
    )
    return list(session.scalars(stmt))


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
