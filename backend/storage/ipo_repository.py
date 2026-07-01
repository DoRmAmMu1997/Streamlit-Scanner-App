"""SQLAlchemy operations for IPO persistence.

All SQL construction stays in ``backend.storage`` so the IPO domain façade can
remain framework-independent and the repository-boundary CI guard stays true.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, cast

from sqlalchemy import func, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session, joinedload, selectinload

from backend.storage.models import (
    IpoDocument,
    IpoFinancial,
    IpoIssue,
    IpoManualExtraction,
    IpoManualFinancialPeriod,
    IpoManualPeerValuation,
    IpoRecommendation,
    IpoScore,
    IpoSubscription,
)


def insert_ipo_issue(session: Session, values: dict[str, Any]) -> IpoIssue:
    """Stage one validated issue row and flush so its generated id is usable."""
    row = IpoIssue(**values)
    session.add(row)
    session.flush()
    return row


def get_ipo_issue(session: Session, issue_id: int) -> IpoIssue | None:
    """Load one issue by primary key without committing the caller's session."""
    return session.get(IpoIssue, issue_id)


def get_ipo_issue_by_sebi_key(session: Session, company_key: str) -> IpoIssue | None:
    """Return the single issue claimed by one normalized SEBI company key."""
    return session.scalar(select(IpoIssue).where(IpoIssue.sebi_company_key == company_key))


def list_unclaimed_ipo_issues_by_company_name(
    session: Session, company_name: str
) -> list[IpoIssue]:
    """Find legacy rows eligible for one conservative, case-insensitive claim."""
    stmt = select(IpoIssue).where(
        IpoIssue.sebi_company_key.is_(None),
        func.lower(IpoIssue.company_name) == company_name.casefold(),
    )
    return list(session.scalars(stmt))


def list_ipo_issue_rows(session: Session) -> list[IpoIssue]:
    """Return issues in the stable newest-date/company/id presentation order."""
    stmt = select(IpoIssue).order_by(
        IpoIssue.open_date.is_(None),
        IpoIssue.open_date.desc(),
        IpoIssue.company_name.asc(),
        IpoIssue.id.asc(),
    )
    return list(session.scalars(stmt))


def update_ipo_issue_row(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoIssue | None:
    """Apply supplied issue columns, refresh update time, and flush if present."""
    row = session.get(IpoIssue, issue_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return row


def delete_ipo_issue_row(session: Session, issue_id: int) -> bool:
    """Stage an issue deletion whose database cascades remove owned children."""
    row = session.get(IpoIssue, issue_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_document(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoDocument:
    """Stage one issue-owned source document and expose its generated id."""
    row = IpoDocument(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_document(
    session: Session, issue_id: int, document_id: int
) -> IpoDocument | None:
    """Load a document only when both its parent issue and row id match."""
    stmt = select(IpoDocument).where(
        IpoDocument.id == document_id, IpoDocument.issue_id == issue_id
    )
    return session.scalar(stmt)


def get_ipo_document_by_record_hash(
    session: Session, record_hash: str
) -> IpoDocument | None:
    """Find the globally unique IPO-002 filing event fingerprint, if present."""
    return session.scalar(
        select(IpoDocument).where(IpoDocument.record_hash == record_hash)
    )


def get_ipo_document_by_url(session: Session, document_url: str) -> IpoDocument | None:
    """Find the single document already owning a canonical detail URL."""
    return session.scalar(
        select(IpoDocument).where(IpoDocument.document_url == document_url)
    )


def update_ipo_document_values(
    session: Session, document: IpoDocument, values: dict[str, Any]
) -> IpoDocument:
    """Mutate an already-owned document row and flush in the caller transaction."""
    for name, value in values.items():
        setattr(document, name, value)
    session.flush()
    return document


def get_latest_ipo_filing_date(session: Session) -> dt.date | None:
    """Return the global filing-date watermark without loading document rows."""
    return session.scalar(select(func.max(IpoDocument.filing_date)))


def list_ipo_document_rows(session: Session, issue_id: int) -> list[IpoDocument]:
    """List one issue's documents deterministically by type, URL, and id."""
    stmt = (
        select(IpoDocument)
        .where(IpoDocument.issue_id == issue_id)
        .order_by(IpoDocument.document_type.asc(), IpoDocument.document_url.asc(), IpoDocument.id.asc())
    )
    return list(session.scalars(stmt))


def update_ipo_document_row(
    session: Session,
    issue_id: int,
    document_id: int,
    values: dict[str, Any],
) -> IpoDocument | None:
    """Update a parent-scoped document or return ``None`` when ownership fails."""
    row = get_ipo_document(session, issue_id, document_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    session.flush()
    return row


def update_ipo_document_cache_if_source_matches(
    session: Session,
    issue_id: int,
    document_id: int,
    *,
    expected_document_url: str,
    expected_document_type: str,
    values: dict[str, Any],
) -> bool:
    """Atomically update cache metadata only for the source that was downloaded.

    The downloader deliberately releases its read transaction during network
    I/O. This compare-and-set closes the resulting time-of-check/time-of-use
    window: a source correction that commits while bytes are in flight makes
    the ``WHERE`` predicate false, so stale bytes cannot be attributed to the
    revised document.
    """
    stmt = (
        update(IpoDocument)
        .where(
            IpoDocument.id == document_id,
            IpoDocument.issue_id == issue_id,
            IpoDocument.document_url == expected_document_url,
            IpoDocument.document_type == expected_document_type,
        )
        .values(**values)
        .execution_options(synchronize_session=False)
    )
    # SQLAlchemy's Session.execute annotation is the broad Result[Any], while a
    # DML UPDATE concretely returns CursorResult and therefore exposes rowcount.
    result = cast(CursorResult[Any], session.execute(stmt))
    return result.rowcount == 1


def delete_ipo_document_row(session: Session, issue_id: int, document_id: int) -> bool:
    """Stage a parent-scoped metadata deletion without touching shared files."""
    row = get_ipo_document(session, issue_id, document_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_financial(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoFinancial:
    """Stage one normalized period under its owning issue and flush its id."""
    row = IpoFinancial(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_financial(
    session: Session, issue_id: int, financial_id: int
) -> IpoFinancial | None:
    """Load a financial period only through the parent issue ownership key."""
    stmt = select(IpoFinancial).where(
        IpoFinancial.id == financial_id, IpoFinancial.issue_id == issue_id
    )
    return session.scalar(stmt)


def list_ipo_financial_rows(session: Session, issue_id: int) -> list[IpoFinancial]:
    """List newest periods first, with stable type/id tie breaking."""
    stmt = (
        select(IpoFinancial)
        .where(IpoFinancial.issue_id == issue_id)
        .order_by(
            IpoFinancial.period_end.desc(),
            IpoFinancial.period_type.asc(),
            IpoFinancial.id.asc(),
        )
    )
    return list(session.scalars(stmt))


def update_ipo_financial_row(
    session: Session,
    issue_id: int,
    financial_id: int,
    values: dict[str, Any],
) -> IpoFinancial | None:
    """Replace selected fields on a parent-scoped financial period and flush."""
    row = get_ipo_financial(session, issue_id, financial_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return row


def delete_ipo_financial_row(session: Session, issue_id: int, financial_id: int) -> bool:
    """Stage deletion of one issue-owned period and report whether it existed."""
    row = get_ipo_financial(session, issue_id, financial_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_manual_extraction(
    session: Session,
    issue_id: int,
    header_values: dict[str, Any],
    period_values: list[dict[str, Any]],
    peer_values: list[dict[str, Any]],
) -> IpoManualExtraction:
    """Stage one complete immutable revision and all of its owned rows.

    Beginner note:
    Header, periods, and peers are attached to one SQLAlchemy unit of work and
    flushed together. The caller's session context therefore either commits the
    complete revision or rolls every row back; a half-written form cannot exist.
    """
    row = IpoManualExtraction(issue_id=issue_id, **header_values)
    row.periods = [
        IpoManualFinancialPeriod(**values) for values in period_values
    ]
    row.peers = [IpoManualPeerValuation(**values) for values in peer_values]
    session.add(row)
    session.flush()
    return row


def _manual_extraction_options() -> tuple[Any, ...]:
    """Return eager-load options needed before detached records are built.

    Beginner note:
    The domain layer reads ``row.periods`` and ``row.peers`` after the session has
    closed. ``selectinload`` fetches both child collections up front (one extra query
    each) so that later access does not trigger a lazy load on a now-detached row,
    which SQLAlchemy would raise as a ``DetachedInstanceError``.
    """
    return (
        selectinload(IpoManualExtraction.periods),
        selectinload(IpoManualExtraction.peers),
    )


def get_ipo_manual_extraction(
    session: Session, issue_id: int, extraction_id: int
) -> IpoManualExtraction | None:
    """Load one complete revision only through its parent issue ownership key."""
    stmt = (
        select(IpoManualExtraction)
        .where(
            IpoManualExtraction.id == extraction_id,
            IpoManualExtraction.issue_id == issue_id,
        )
        .options(*_manual_extraction_options())
    )
    return session.scalar(stmt)


def list_ipo_manual_extraction_rows(
    session: Session, issue_id: int
) -> list[IpoManualExtraction]:
    """List immutable revisions newest-first with id as the deterministic tie break."""
    stmt = (
        select(IpoManualExtraction)
        .where(IpoManualExtraction.issue_id == issue_id)
        .order_by(
            IpoManualExtraction.submitted_at.desc(),
            IpoManualExtraction.id.desc(),
        )
        .options(*_manual_extraction_options())
    )
    return list(session.scalars(stmt))


def get_latest_ipo_manual_extraction(
    session: Session, issue_id: int
) -> IpoManualExtraction | None:
    """Load only the newest complete revision for the scoring-data bridge.

    Uses ``LIMIT 1`` with the same ``submitted_at DESC, id DESC`` ordering as
    :func:`list_ipo_manual_extraction_rows`, so "latest" is exactly that list's first
    row -- without materializing the whole append-only history to keep one record.
    """
    stmt = (
        select(IpoManualExtraction)
        .where(IpoManualExtraction.issue_id == issue_id)
        .order_by(
            IpoManualExtraction.submitted_at.desc(),
            IpoManualExtraction.id.desc(),
        )
        .options(*_manual_extraction_options())
        .limit(1)
    )
    return session.scalar(stmt)


def insert_ipo_subscription(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoSubscription:
    """Stage one timestamped demand snapshot under its parent issue."""
    row = IpoSubscription(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_subscription(
    session: Session, issue_id: int, subscription_id: int
) -> IpoSubscription | None:
    """Load a subscription snapshot only through its issue ownership scope."""
    stmt = select(IpoSubscription).where(
        IpoSubscription.id == subscription_id,
        IpoSubscription.issue_id == issue_id,
    )
    return session.scalar(stmt)


def list_ipo_subscription_rows(
    session: Session, issue_id: int
) -> list[IpoSubscription]:
    """List demand snapshots newest-first with id as a stable tie break."""
    stmt = (
        select(IpoSubscription)
        .where(IpoSubscription.issue_id == issue_id)
        .order_by(IpoSubscription.captured_at.desc(), IpoSubscription.id.desc())
    )
    return list(session.scalars(stmt))


def update_ipo_subscription_row(
    session: Session,
    issue_id: int,
    subscription_id: int,
    values: dict[str, Any],
) -> IpoSubscription | None:
    """Replace selected fields on a parent-scoped demand snapshot and flush."""
    row = get_ipo_subscription(session, issue_id, subscription_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    session.flush()
    return row


def delete_ipo_subscription_row(
    session: Session, issue_id: int, subscription_id: int
) -> bool:
    """Stage deletion of one issue-owned snapshot and remain idempotent."""
    row = get_ipo_subscription(session, issue_id, subscription_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_evaluation(
    session: Session,
    issue_id: int,
    score_values: dict[str, Any],
    recommendation_values: dict[str, Any],
) -> tuple[IpoScore, IpoRecommendation]:
    """Stage an immutable score and its one-to-one verdict as one unit of work."""
    score = IpoScore(issue_id=issue_id, **score_values)
    recommendation = IpoRecommendation(score=score, **recommendation_values)
    session.add_all([score, recommendation])
    session.flush()
    return score, recommendation


def get_ipo_evaluation_rows(
    session: Session, issue_id: int, score_id: int
) -> tuple[IpoScore, IpoRecommendation] | None:
    # ``joinedload(IpoScore.issue)`` eager-loads the parent issue so the domain
    # layer can read ``score.issue.company_name`` without a follow-up SELECT.
    # The issue is many-to-one, so this adds no row fan-out to the result.
    """Load one complete evaluation pair, rejecting orphaned partial history."""
    stmt = (
        select(IpoScore, IpoRecommendation)
        .join(IpoRecommendation, IpoRecommendation.score_id == IpoScore.id)
        .where(IpoScore.issue_id == issue_id, IpoScore.id == score_id)
        .options(joinedload(IpoScore.issue))
    )
    row = session.execute(stmt).one_or_none()
    return (row[0], row[1]) if row is not None else None


def list_ipo_evaluation_rows(
    session: Session, issue_id: int
) -> list[tuple[IpoScore, IpoRecommendation]]:
    """List complete evaluation pairs newest-first for one issue."""
    stmt = (
        select(IpoScore, IpoRecommendation)
        .join(IpoRecommendation, IpoRecommendation.score_id == IpoScore.id)
        .where(IpoScore.issue_id == issue_id)
        .order_by(IpoScore.scored_at.desc(), IpoScore.id.desc())
        .options(joinedload(IpoScore.issue))
    )
    return [(row[0], row[1]) for row in session.execute(stmt)]


def get_latest_ipo_evaluation_rows(
    session: Session, issue_id: int
) -> tuple[IpoScore, IpoRecommendation] | None:
    """Return only the newest score/recommendation pair for one issue.

    Evaluation history is append-only and can grow without bound, so the
    "latest recommendation" read uses ``LIMIT 1`` with the same deterministic
    ordering as :func:`list_ipo_evaluation_rows` instead of materializing the
    whole history just to keep its first row.
    """
    stmt = (
        select(IpoScore, IpoRecommendation)
        .join(IpoRecommendation, IpoRecommendation.score_id == IpoScore.id)
        .where(IpoScore.issue_id == issue_id)
        .order_by(IpoScore.scored_at.desc(), IpoScore.id.desc())
        .options(joinedload(IpoScore.issue))
        .limit(1)
    )
    row = session.execute(stmt).one_or_none()
    return (row[0], row[1]) if row is not None else None


def delete_ipo_evaluation_row(session: Session, issue_id: int, score_id: int) -> bool:
    """Delete one score so the database cascade removes its paired verdict."""
    rows = get_ipo_evaluation_rows(session, issue_id, score_id)
    if rows is None:
        return False
    score, _recommendation = rows
    session.delete(score)
    session.flush()
    return True
