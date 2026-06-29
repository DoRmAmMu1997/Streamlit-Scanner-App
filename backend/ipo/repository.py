"""Typed transaction façade for IPO source facts and evaluations."""

from __future__ import annotations

import datetime as dt
from typing import Any

from backend.ipo.models import (
    Confidence,
    FinancialPeriodType,
    IpoDocumentData,
    IpoDocumentRecord,
    IpoEvaluationRecord,
    IpoFinancialData,
    IpoFinancialRecord,
    IpoIssueData,
    IpoIssueRecord,
    IpoIssueType,
    IpoRecommendationResult,
    IpoScoreInput,
    IpoStatus,
    IpoSubscriptionData,
    IpoSubscriptionRecord,
    IpoValidationError,
    Recommendation,
)
from backend.ipo.scorecard import score_ipo
from backend.ipo.verdict import build_recommendation
from backend.scanning.result_contract import normalize_secret_safe_json
from backend.storage import session_scope
from backend.storage.ipo_repository import (
    delete_ipo_document_row,
    delete_ipo_evaluation_row,
    delete_ipo_financial_row,
    delete_ipo_issue_row,
    delete_ipo_subscription_row,
    get_ipo_document,
    get_ipo_evaluation_rows,
    get_ipo_financial,
    get_ipo_issue,
    get_ipo_subscription,
    get_latest_ipo_evaluation_rows,
    insert_ipo_document,
    insert_ipo_evaluation,
    insert_ipo_financial,
    insert_ipo_issue,
    insert_ipo_subscription,
    list_ipo_document_rows,
    list_ipo_evaluation_rows,
    list_ipo_financial_rows,
    list_ipo_issue_rows,
    list_ipo_subscription_rows,
    update_ipo_document_row,
    update_ipo_financial_row,
    update_ipo_issue_row,
    update_ipo_subscription_row,
)

SessionFactory = Any


class IpoNotFoundError(LookupError):
    """Raised when an IPO update targets a row that does not exist."""


def _utc(value: dt.datetime) -> dt.datetime:
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _issue_values(data: IpoIssueData) -> dict[str, Any]:
    return {
        "company_name": data.company_name,
        "issue_type": data.issue_type.value,
        "status": data.status.value,
        "open_date": data.open_date,
        "close_date": data.close_date,
        "price_band_low": data.price_band_low,
        "price_band_high": data.price_band_high,
        "lot_size": data.lot_size,
        "fresh_issue_amount": data.fresh_issue_amount,
        "ofs_amount": data.ofs_amount,
        "source_url": data.source_url,
        "source_confidence": data.source_confidence.value,
    }


def _issue_record(row: Any) -> IpoIssueRecord:
    return IpoIssueRecord(
        id=row.id,
        company_name=row.company_name,
        issue_type=IpoIssueType(row.issue_type),
        status=IpoStatus(row.status),
        open_date=row.open_date,
        close_date=row.close_date,
        price_band_low=row.price_band_low,
        price_band_high=row.price_band_high,
        lot_size=row.lot_size,
        fresh_issue_amount=row.fresh_issue_amount,
        ofs_amount=row.ofs_amount,
        source_url=row.source_url,
        source_confidence=Confidence(row.source_confidence),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def create_issue(
    data: IpoIssueData, *, session_factory: SessionFactory = session_scope
) -> IpoIssueRecord:
    """Create one issue and return a detached typed record."""
    with session_factory() as session:
        return _issue_record(insert_ipo_issue(session, _issue_values(data)))


def get_issue(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> IpoIssueRecord | None:
    """Return one issue or ``None`` when absent."""
    with session_factory() as session:
        row = get_ipo_issue(session, issue_id)
        return _issue_record(row) if row is not None else None


def list_issues(*, session_factory: SessionFactory = session_scope) -> list[IpoIssueRecord]:
    """List issues by newest open date, then company name and id."""
    with session_factory() as session:
        return [_issue_record(row) for row in list_ipo_issue_rows(session)]


def update_issue(
    issue_id: int,
    data: IpoIssueData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoIssueRecord:
    """Replace mutable facts for one issue; raise when the id is absent."""
    with session_factory() as session:
        row = update_ipo_issue_row(session, issue_id, _issue_values(data))
        if row is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return _issue_record(row)


def delete_issue(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> bool:
    """Delete an issue and all children; return false when already absent."""
    with session_factory() as session:
        return delete_ipo_issue_row(session, issue_id)


def _document_values(data: IpoDocumentData) -> dict[str, Any]:
    return {
        "document_type": data.document_type,
        "document_url": data.document_url,
        "source_url": data.source_url,
        "source_confidence": data.source_confidence.value,
    }


def _document_record(row: Any) -> IpoDocumentRecord:
    return IpoDocumentRecord(
        id=row.id,
        issue_id=row.issue_id,
        document_type=row.document_type,
        document_url=row.document_url,
        source_url=row.source_url,
        source_confidence=Confidence(row.source_confidence),
        created_at=_utc(row.created_at),
    )


def create_document(
    issue_id: int,
    data: IpoDocumentData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoDocumentRecord:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return _document_record(insert_ipo_document(session, issue_id, _document_values(data)))


def get_document(
    issue_id: int,
    document_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoDocumentRecord | None:
    with session_factory() as session:
        row = get_ipo_document(session, issue_id, document_id)
        return _document_record(row) if row is not None else None


def list_documents(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoDocumentRecord]:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [_document_record(row) for row in list_ipo_document_rows(session, issue_id)]


def update_document(
    issue_id: int,
    document_id: int,
    data: IpoDocumentData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoDocumentRecord:
    with session_factory() as session:
        row = update_ipo_document_row(
            session, issue_id, document_id, _document_values(data)
        )
        if row is None:
            raise IpoNotFoundError(
                f"IPO document {document_id} was not found for issue {issue_id}."
            )
        return _document_record(row)


def delete_document(
    issue_id: int,
    document_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> bool:
    with session_factory() as session:
        return delete_ipo_document_row(session, issue_id, document_id)


def _financial_values(data: IpoFinancialData) -> dict[str, Any]:
    normalized = normalize_secret_safe_json(dict(data.metrics))
    if not isinstance(normalized, dict):
        raise IpoValidationError("Normalized financial metrics must remain an object.")
    return {
        "period_end": data.period_end,
        "period_type": data.period_type.value,
        "metrics_json": normalized,
        "source_document_id": data.source_document_id,
        "source_url": data.source_url,
        "source_confidence": data.source_confidence.value,
    }


def _financial_record(row: Any) -> IpoFinancialRecord:
    return IpoFinancialRecord(
        id=row.id,
        issue_id=row.issue_id,
        period_end=row.period_end,
        period_type=FinancialPeriodType(row.period_type),
        metrics=row.metrics_json,
        source_document_id=row.source_document_id,
        source_url=row.source_url,
        source_confidence=Confidence(row.source_confidence),
        created_at=_utc(row.created_at),
        updated_at=_utc(row.updated_at),
    )


def _validate_source_document(session: Any, issue_id: int, source_document_id: int | None) -> None:
    if source_document_id is None:
        return
    if get_ipo_document(session, issue_id, source_document_id) is None:
        raise IpoValidationError(
            f"Source document {source_document_id} does not belong to IPO issue {issue_id}."
        )


def create_financial(
    issue_id: int,
    data: IpoFinancialData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoFinancialRecord:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        _validate_source_document(session, issue_id, data.source_document_id)
        return _financial_record(insert_ipo_financial(session, issue_id, _financial_values(data)))


def get_financial(
    issue_id: int,
    financial_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoFinancialRecord | None:
    with session_factory() as session:
        row = get_ipo_financial(session, issue_id, financial_id)
        return _financial_record(row) if row is not None else None


def list_financials(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoFinancialRecord]:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [_financial_record(row) for row in list_ipo_financial_rows(session, issue_id)]


def update_financial(
    issue_id: int,
    financial_id: int,
    data: IpoFinancialData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoFinancialRecord:
    with session_factory() as session:
        _validate_source_document(session, issue_id, data.source_document_id)
        row = update_ipo_financial_row(
            session, issue_id, financial_id, _financial_values(data)
        )
        if row is None:
            raise IpoNotFoundError(
                f"IPO financial {financial_id} was not found for issue {issue_id}."
            )
        return _financial_record(row)


def delete_financial(
    issue_id: int,
    financial_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> bool:
    with session_factory() as session:
        return delete_ipo_financial_row(session, issue_id, financial_id)


def _subscription_values(data: IpoSubscriptionData) -> dict[str, Any]:
    return {
        "captured_at": data.captured_at,
        "qib_multiple": data.qib_multiple,
        "nii_multiple": data.nii_multiple,
        "retail_multiple": data.retail_multiple,
        "total_multiple": data.total_multiple,
        "source_url": data.source_url,
        "source_confidence": data.source_confidence.value,
    }


def _subscription_record(row: Any) -> IpoSubscriptionRecord:
    return IpoSubscriptionRecord(
        id=row.id,
        issue_id=row.issue_id,
        captured_at=_utc(row.captured_at),
        qib_multiple=row.qib_multiple,
        nii_multiple=row.nii_multiple,
        retail_multiple=row.retail_multiple,
        total_multiple=row.total_multiple,
        source_url=row.source_url,
        source_confidence=Confidence(row.source_confidence),
        created_at=_utc(row.created_at),
    )


def create_subscription(
    issue_id: int,
    data: IpoSubscriptionData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoSubscriptionRecord:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return _subscription_record(
            insert_ipo_subscription(session, issue_id, _subscription_values(data))
        )


def get_subscription(
    issue_id: int,
    subscription_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoSubscriptionRecord | None:
    with session_factory() as session:
        row = get_ipo_subscription(session, issue_id, subscription_id)
        return _subscription_record(row) if row is not None else None


def list_subscriptions(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoSubscriptionRecord]:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [
            _subscription_record(row)
            for row in list_ipo_subscription_rows(session, issue_id)
        ]


def update_subscription(
    issue_id: int,
    subscription_id: int,
    data: IpoSubscriptionData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoSubscriptionRecord:
    with session_factory() as session:
        row = update_ipo_subscription_row(
            session, issue_id, subscription_id, _subscription_values(data)
        )
        if row is None:
            raise IpoNotFoundError(
                f"IPO subscription {subscription_id} was not found for issue {issue_id}."
            )
        return _subscription_record(row)


def delete_subscription(
    issue_id: int,
    subscription_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> bool:
    with session_factory() as session:
        return delete_ipo_subscription_row(session, issue_id, subscription_id)


def _evaluation_record(score_row: Any, recommendation_row: Any) -> IpoEvaluationRecord:
    result = IpoRecommendationResult(
        company_name=score_row.issue.company_name,
        score=score_row.total_score,
        recommendation=Recommendation(recommendation_row.recommendation),
        recommendation_type=recommendation_row.recommendation_type,
        confidence=Confidence(recommendation_row.confidence),
        reasons=tuple(recommendation_row.reasons_json),
        missing_data=tuple(recommendation_row.missing_data_json),
        source_documents=tuple(recommendation_row.source_documents_json),
    )
    return IpoEvaluationRecord(
        issue_id=score_row.issue_id,
        score_id=score_row.id,
        recommendation_id=recommendation_row.id,
        model_version=score_row.model_version,
        scored_at=_utc(score_row.scored_at),
        result=result,
    )


def evaluate_issue(
    issue_id: int,
    score_input: IpoScoreInput,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoEvaluationRecord:
    """Compute and atomically persist one immutable score/verdict pair."""
    score_result = score_ipo(score_input)
    recommendation = build_recommendation(score_result)

    with session_factory() as session:
        issue = get_ipo_issue(session, issue_id)
        if issue is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        if issue.company_name.casefold() != score_input.company_name.casefold():
            raise IpoValidationError(
                "score_input.company_name must match the persisted IPO issue company_name."
            )
        registered_urls = {
            row.document_url for row in list_ipo_document_rows(session, issue_id)
        }
        unregistered = [
            url for url in score_input.source_documents if url not in registered_urls
        ]
        if unregistered:
            raise IpoValidationError(
                "Every source document must be registered to the IPO issue; "
                f"not registered: {', '.join(unregistered)}."
            )

        score_values = {
            "business_quality": score_input.business_quality.score,
            "financial_growth": score_input.financial_growth.score,
            "return_ratios": score_input.return_ratios.score,
            "valuation": score_input.valuation.score,
            "qib_subscription": score_input.qib_subscription.score,
            "promoter_quality": score_input.promoter_quality.score,
            "gmp_sentiment": score_input.gmp_sentiment.score,
            "total_score": score_result.score,
            "contributions_json": normalize_secret_safe_json(
                dict(score_result.contributions)
            ),
            "missing_data_json": list(score_result.missing_data),
            "reasons_json": list(score_result.reasons),
            "model_version": "ipo-001-v1",
        }
        recommendation_values = {
            "recommendation": recommendation.recommendation.value,
            "recommendation_type": recommendation.recommendation_type,
            "confidence": recommendation.confidence.value,
            "reasons_json": list(recommendation.reasons),
            "missing_data_json": list(recommendation.missing_data),
            "source_documents_json": list(recommendation.source_documents),
        }
        score_row, recommendation_row = insert_ipo_evaluation(
            session, issue_id, score_values, recommendation_values
        )
        return _evaluation_record(score_row, recommendation_row)


def get_evaluation(
    issue_id: int,
    score_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoEvaluationRecord | None:
    with session_factory() as session:
        rows = get_ipo_evaluation_rows(session, issue_id, score_id)
        return _evaluation_record(*rows) if rows is not None else None


def list_evaluations(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoEvaluationRecord]:
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [
            _evaluation_record(score, recommendation)
            for score, recommendation in list_ipo_evaluation_rows(session, issue_id)
        ]


def get_latest_recommendation(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> IpoRecommendationResult | None:
    """Return the newest recommendation for an issue, or ``None`` if unscored.

    Reads only the most recent evaluation pair (``LIMIT 1``) rather than loading
    the full append-only history. A missing issue still raises ``IpoNotFoundError``
    so callers can distinguish "no such issue" from "issue exists but unscored".
    """
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        rows = get_latest_ipo_evaluation_rows(session, issue_id)
        return _evaluation_record(*rows).result if rows is not None else None


def delete_evaluation(
    issue_id: int,
    score_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> bool:
    """Delete one immutable evaluation pair; direct edits remain unavailable."""
    with session_factory() as session:
        return delete_ipo_evaluation_row(session, issue_id, score_id)
