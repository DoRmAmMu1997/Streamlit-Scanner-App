"""SQLAlchemy operations for IPO persistence.

All SQL construction stays in ``backend.storage`` so the IPO domain façade can
remain framework-independent and the repository-boundary CI guard stays true.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, joinedload

from backend.storage.models import (
    IpoDocument,
    IpoFinancial,
    IpoIssue,
    IpoRecommendation,
    IpoScore,
    IpoSubscription,
)


def insert_ipo_issue(session: Session, values: dict[str, Any]) -> IpoIssue:
    row = IpoIssue(**values)
    session.add(row)
    session.flush()
    return row


def get_ipo_issue(session: Session, issue_id: int) -> IpoIssue | None:
    return session.get(IpoIssue, issue_id)


def list_ipo_issue_rows(session: Session) -> list[IpoIssue]:
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
    row = session.get(IpoIssue, issue_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return row


def delete_ipo_issue_row(session: Session, issue_id: int) -> bool:
    row = session.get(IpoIssue, issue_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_document(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoDocument:
    row = IpoDocument(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_document(
    session: Session, issue_id: int, document_id: int
) -> IpoDocument | None:
    stmt = select(IpoDocument).where(
        IpoDocument.id == document_id, IpoDocument.issue_id == issue_id
    )
    return session.scalar(stmt)


def list_ipo_document_rows(session: Session, issue_id: int) -> list[IpoDocument]:
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
    row = get_ipo_document(session, issue_id, document_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    session.flush()
    return row


def delete_ipo_document_row(session: Session, issue_id: int, document_id: int) -> bool:
    row = get_ipo_document(session, issue_id, document_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_financial(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoFinancial:
    row = IpoFinancial(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_financial(
    session: Session, issue_id: int, financial_id: int
) -> IpoFinancial | None:
    stmt = select(IpoFinancial).where(
        IpoFinancial.id == financial_id, IpoFinancial.issue_id == issue_id
    )
    return session.scalar(stmt)


def list_ipo_financial_rows(session: Session, issue_id: int) -> list[IpoFinancial]:
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
    row = get_ipo_financial(session, issue_id, financial_id)
    if row is None:
        return None
    for name, value in values.items():
        setattr(row, name, value)
    row.updated_at = dt.datetime.now(dt.UTC)
    session.flush()
    return row


def delete_ipo_financial_row(session: Session, issue_id: int, financial_id: int) -> bool:
    row = get_ipo_financial(session, issue_id, financial_id)
    if row is None:
        return False
    session.delete(row)
    session.flush()
    return True


def insert_ipo_subscription(
    session: Session, issue_id: int, values: dict[str, Any]
) -> IpoSubscription:
    row = IpoSubscription(issue_id=issue_id, **values)
    session.add(row)
    session.flush()
    return row


def get_ipo_subscription(
    session: Session, issue_id: int, subscription_id: int
) -> IpoSubscription | None:
    stmt = select(IpoSubscription).where(
        IpoSubscription.id == subscription_id,
        IpoSubscription.issue_id == issue_id,
    )
    return session.scalar(stmt)


def list_ipo_subscription_rows(
    session: Session, issue_id: int
) -> list[IpoSubscription]:
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
    rows = get_ipo_evaluation_rows(session, issue_id, score_id)
    if rows is None:
        return False
    score, _recommendation = rows
    session.delete(score)
    session.flush()
    return True
