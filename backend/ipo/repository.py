"""Typed transaction façade for IPO source facts and evaluations."""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable
from pathlib import Path
from typing import Any

from backend.audit import record_audit_event
from backend.config import get_settings
from backend.ipo.documents.downloader import (
    IpoDocumentDownloadError,
    IpoDocumentDownloadErrorCode,
    IpoDocumentDownloadResult,
    download_document_file,
)
from backend.ipo.models import (
    Confidence,
    FinancialPeriodType,
    IpoDocumentData,
    IpoDocumentParseStatus,
    IpoDocumentRecord,
    IpoEvaluationRecord,
    IpoFilingData,
    IpoFinancialData,
    IpoFinancialRecord,
    IpoIngestionSummary,
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
from backend.observability import (
    EVENT_IPO_DOCUMENT_DOWNLOAD_COMPLETED,
    EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED,
    log_event,
)
from backend.scanning.result_contract import normalize_secret_safe_json
from backend.storage import session_scope
from backend.storage.ipo_repository import (
    delete_ipo_document_row,
    delete_ipo_evaluation_row,
    delete_ipo_financial_row,
    delete_ipo_issue_row,
    delete_ipo_subscription_row,
    get_ipo_document,
    get_ipo_document_by_record_hash,
    get_ipo_document_by_url,
    get_ipo_evaluation_rows,
    get_ipo_financial,
    get_ipo_issue,
    get_ipo_issue_by_sebi_key,
    get_ipo_subscription,
    get_latest_ipo_evaluation_rows,
    get_latest_ipo_filing_date,
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
    list_unclaimed_ipo_issues_by_company_name,
    update_ipo_document_cache_if_source_matches,
    update_ipo_document_values,
    update_ipo_financial_row,
    update_ipo_issue_row,
    update_ipo_subscription_row,
)

SessionFactory = Any
DocumentDownloader = Callable[..., IpoDocumentDownloadResult]
AuditRecorder = Callable[..., bool]

logger = logging.getLogger(__name__)


class IpoNotFoundError(LookupError):
    """Raised when an IPO update targets a row that does not exist."""


def _utc(value: dt.datetime) -> dt.datetime:
    """Provide the utc step used by the IPO workflow."""
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _issue_values(data: IpoIssueData) -> dict[str, Any]:
    """Provide the issue values step used by the IPO workflow."""
    return {
        "company_name": data.company_name,
        "sebi_company_key": data.sebi_company_key,
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
    """Provide the issue record step used by the IPO workflow."""
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
        sebi_company_key=row.sebi_company_key,
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
    """Provide the document values step used by the IPO workflow."""
    return {
        "document_type": data.document_type,
        "document_url": data.document_url,
        "source_url": data.source_url,
        "source_confidence": data.source_confidence.value,
        "filing_date": data.filing_date,
        "record_hash": data.record_hash,
    }


def _document_record(row: Any) -> IpoDocumentRecord:
    """Detach one ORM document row into the public immutable domain record."""
    return IpoDocumentRecord(
        id=row.id,
        issue_id=row.issue_id,
        document_type=row.document_type,
        document_url=row.document_url,
        source_url=row.source_url,
        source_confidence=Confidence(row.source_confidence),
        filing_date=row.filing_date,
        record_hash=row.record_hash,
        content_sha256=row.content_sha256,
        downloaded_at=_utc(row.downloaded_at) if row.downloaded_at is not None else None,
        file_path=row.file_path,
        page_count=row.page_count,
        parse_status=IpoDocumentParseStatus(row.parse_status),
        created_at=_utc(row.created_at),
    )


def create_document(
    issue_id: int,
    data: IpoDocumentData,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoDocumentRecord:
    """Create document through the IPO storage boundary."""
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
    """Return document through the IPO storage boundary."""
    with session_factory() as session:
        row = get_ipo_document(session, issue_id, document_id)
        return _document_record(row) if row is not None else None


def list_documents(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoDocumentRecord]:
    """Return the ordered documents through the IPO storage boundary."""
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
    """Replace source facts and invalidate provenance when source identity changes."""
    with session_factory() as session:
        existing = get_ipo_document(session, issue_id, document_id)
        if existing is None:
            raise IpoNotFoundError(
                f"IPO document {document_id} was not found for issue {issue_id}."
            )
        values = _document_values(data)
        # A digest proves the bytes fetched from one particular remote identity.
        # Reusing it after the URL or document category changes would falsely
        # attribute old bytes to a new source, so reset the cache as one unit.
        if (
            existing.document_url != data.document_url
            or existing.document_type != data.document_type
        ):
            values.update(_empty_document_cache_values(IpoDocumentParseStatus.NOT_DOWNLOADED))
        row = update_ipo_document_values(session, existing, values)
        return _document_record(row)


def delete_document(
    issue_id: int,
    document_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> bool:
    """Delete document through the IPO storage boundary."""
    with session_factory() as session:
        return delete_ipo_document_row(session, issue_id, document_id)


def _empty_document_cache_values(
    status: IpoDocumentParseStatus,
) -> dict[str, Any]:
    """Return the all-null provenance tuple required by a non-pending status."""
    return {
        "content_sha256": None,
        "downloaded_at": None,
        "file_path": None,
        "page_count": None,
        "parse_status": status.value,
    }


def _record_document_download_failure(
    *,
    issue_id: int,
    document: IpoDocumentRecord,
    error: IpoDocumentDownloadError,
    audit_recorder: AuditRecorder,
    session_factory: SessionFactory,
) -> None:
    """Emit one secret-safe failure event without weakening database authority.

    Audit storage is useful secondary evidence, but it must never replace the
    already-committed document status or hide the stable downloader exception.
    Consequently an audit sink failure is deliberately swallowed after the
    structured lifecycle event has been emitted.
    """
    metadata: dict[str, Any] = {
        "issue_id": issue_id,
        "document_id": document.id,
        "document_type": document.document_type,
        "error_code": error.code.value,
    }
    log_event(
        logger,
        EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED,
        level=logging.WARNING,
        **metadata,
    )
    try:
        audit_recorder(
            event=EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED,
            metadata=metadata,
            level=logging.WARNING,
            session_factory=session_factory,
        )
    except Exception:  # noqa: BLE001 - the optional audit sink must be best effort.
        return


def _source_changed_error() -> IpoDocumentDownloadError:
    """Return the stable conflict used when source facts change during HTTP."""
    return IpoDocumentDownloadError(IpoDocumentDownloadErrorCode.SOURCE_CHANGED)


def download_document(
    issue_id: int,
    document_id: int,
    *,
    data_dir: Path | None = None,
    downloader: DocumentDownloader = download_document_file,
    audit_recorder: AuditRecorder = record_audit_event,
    session_factory: SessionFactory = session_scope,
) -> IpoDocumentDownloadResult:
    """Download one DRHP/RHP with no database transaction open during HTTP.

    The method intentionally uses two short transactions. The first detaches the
    source row, then closes before potentially slow DNS/HTTP/filesystem work. A
    second transaction records either verified provenance or a failure status.
    The file rename and database commit cannot be one atomic operation; if the
    second commit fails, the immutable hash-named file may remain as a harmless
    orphan for a future cleanup job.
    """
    with session_factory() as session:
        row = get_ipo_document(session, issue_id, document_id)
        if row is None:
            raise IpoNotFoundError(
                f"IPO document {document_id} was not found for issue {issue_id}."
            )
        document = _document_record(row)

    if document.document_type not in {"drhp", "rhp"}:
        raise IpoValidationError("IPO-003 can download only a DRHP or RHP document.")

    cache_root = Path(data_dir) if data_dir is not None else get_settings().data_dir
    try:
        result = downloader(document, data_dir=cache_root)
    except IpoDocumentDownloadError as exc:
        # Failure rows carry no path/hash. This prevents later callers from
        # treating a partial or unverified file as a successful cache entry.
        with session_factory() as session:
            updated = update_ipo_document_cache_if_source_matches(
                session,
                issue_id,
                document_id,
                expected_document_url=document.document_url,
                expected_document_type=document.document_type,
                values=_empty_document_cache_values(
                    IpoDocumentParseStatus.DOWNLOAD_FAILED
                ),
            )
            if not updated and get_ipo_document(session, issue_id, document_id) is None:
                raise IpoNotFoundError(
                    f"IPO document {document_id} disappeared during download."
                ) from exc
        failure = exc if updated else _source_changed_error()
        _record_document_download_failure(
            issue_id=issue_id,
            document=document,
            error=failure,
            audit_recorder=audit_recorder,
            session_factory=session_factory,
        )
        if failure is not exc:
            raise failure from exc
        raise

    with session_factory() as session:
        updated = update_ipo_document_cache_if_source_matches(
            session,
            issue_id,
            document_id,
            expected_document_url=document.document_url,
            expected_document_type=document.document_type,
            values={
                "content_sha256": result.content_sha256,
                "downloaded_at": result.downloaded_at,
                "file_path": result.file_path,
                "page_count": result.page_count,
                "parse_status": result.parse_status.value,
            },
        )
        if not updated and get_ipo_document(session, issue_id, document_id) is None:
            raise IpoNotFoundError(
                f"IPO document {document_id} disappeared during download."
            )
    if not updated:
        failure = _source_changed_error()
        _record_document_download_failure(
            issue_id=issue_id,
            document=document,
            error=failure,
            audit_recorder=audit_recorder,
            session_factory=session_factory,
        )
        raise failure
    log_event(
        logger,
        EVENT_IPO_DOCUMENT_DOWNLOAD_COMPLETED,
        issue_id=issue_id,
        document_id=document_id,
        document_type=document.document_type,
        cache_hit=result.cache_hit,
        bytes_written=result.bytes_written,
    )
    return result


_STATUS_ORDER = {
    IpoStatus.DRHP_FILED: 0,
    IpoStatus.RHP_FILED: 1,
    IpoStatus.OPEN: 2,
    IpoStatus.CLOSED: 3,
    IpoStatus.LISTED: 4,
}


def _ingestion_issue_values(data: IpoFilingData) -> dict[str, Any]:
    """Provide the ingestion issue values step used by the IPO workflow."""
    return {
        "company_name": data.company_name,
        "sebi_company_key": data.sebi_company_key,
        "issue_type": data.issue_type.value,
        "status": data.status.value,
        "source_url": data.source_url,
        "source_confidence": Confidence.HIGH.value,
    }


def _ingestion_document_values(data: IpoFilingData) -> dict[str, Any]:
    """Provide the ingestion document values step used by the IPO workflow."""
    return {
        "document_type": data.document_type,
        "document_url": data.document_url,
        "source_url": data.source_url,
        "source_confidence": Confidence.HIGH.value,
        "filing_date": data.filing_date,
        "record_hash": data.record_hash,
    }


def _changed_values(row: Any, desired: dict[str, Any]) -> dict[str, Any]:
    """Provide the changed values step used by the IPO workflow."""
    return {name: value for name, value in desired.items() if getattr(row, name) != value}


def ingest_filings(
    filings: tuple[IpoFilingData, ...] | list[IpoFilingData],
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoIngestionSummary:
    """Atomically create/update issues and documents for one fetched category.

    Beginner note:
    The caller opens one invocation per SEBI category. If any ownership conflict
    appears, the session context rolls the whole category back while other
    categories handled by the job remain independently committed.
    """
    counts = {
        "received": len(filings),
        "issues_created": 0,
        "issues_updated": 0,
        "documents_created": 0,
        "documents_updated": 0,
        "unchanged": 0,
    }
    with session_factory() as session:
        for filing in filings:
            issue = get_ipo_issue_by_sebi_key(session, filing.sebi_company_key)
            issue_changed = False
            if issue is None:
                legacy_matches = list_unclaimed_ipo_issues_by_company_name(
                    session, filing.company_name
                )
                if len(legacy_matches) == 1:
                    issue = legacy_matches[0]
                    update_ipo_issue_row(
                        session,
                        issue.id,
                        {"sebi_company_key": filing.sebi_company_key},
                    )
                    issue_changed = True
                    counts["issues_updated"] += 1
                else:
                    issue = insert_ipo_issue(session, _ingestion_issue_values(filing))
                    counts["issues_created"] += 1

            desired_issue: dict[str, Any] = {}
            current_status = IpoStatus(issue.status)
            if issue.source_confidence != Confidence.HIGH.value:
                desired_issue["source_confidence"] = Confidence.HIGH.value
            if issue.source_url is None:
                desired_issue["source_url"] = filing.source_url
            if _STATUS_ORDER[filing.status] > _STATUS_ORDER[current_status]:
                desired_issue.update(
                    status=filing.status.value,
                    source_url=filing.source_url,
                    source_confidence=Confidence.HIGH.value,
                )
            if issue.issue_type == IpoIssueType.UNKNOWN.value and filing.issue_type is IpoIssueType.SME:
                desired_issue["issue_type"] = IpoIssueType.SME.value
            changes = _changed_values(issue, desired_issue)
            if changes:
                update_ipo_issue_row(session, issue.id, changes)
                if not issue_changed:
                    counts["issues_updated"] += 1
                issue_changed = True

            document = get_ipo_document_by_record_hash(session, filing.record_hash)
            if document is None:
                document = get_ipo_document_by_url(session, filing.document_url)
            if document is not None and document.issue_id != issue.id:
                raise IpoValidationError(
                    "SEBI filing fingerprint or URL is owned by another IPO issue."
                )

            document_values = _ingestion_document_values(filing)
            if document is None:
                insert_ipo_document(session, issue.id, document_values)
                counts["documents_created"] += 1
            else:
                document_changes = _changed_values(document, document_values)
                if document_changes:
                    update_ipo_document_values(session, document, document_changes)
                    counts["documents_updated"] += 1
                elif not issue_changed:
                    counts["unchanged"] += 1

    return IpoIngestionSummary(**counts)


def get_latest_filing_date(
    *, session_factory: SessionFactory = session_scope
) -> dt.date | None:
    """Return the newest persisted SEBI filing date across all categories."""
    with session_factory() as session:
        return get_latest_ipo_filing_date(session)


def _financial_values(data: IpoFinancialData) -> dict[str, Any]:
    """Provide the financial values step used by the IPO workflow."""
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
    """Provide the financial record step used by the IPO workflow."""
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
    """Validate source document before persistence can continue."""
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
    """Create financial through the IPO storage boundary."""
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
    """Return financial through the IPO storage boundary."""
    with session_factory() as session:
        row = get_ipo_financial(session, issue_id, financial_id)
        return _financial_record(row) if row is not None else None


def list_financials(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoFinancialRecord]:
    """Return the ordered financials through the IPO storage boundary."""
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
    """Update financial through the IPO storage boundary."""
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
    """Delete financial through the IPO storage boundary."""
    with session_factory() as session:
        return delete_ipo_financial_row(session, issue_id, financial_id)


def _subscription_values(data: IpoSubscriptionData) -> dict[str, Any]:
    """Provide the subscription values step used by the IPO workflow."""
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
    """Provide the subscription record step used by the IPO workflow."""
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
    """Create subscription through the IPO storage boundary."""
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
    """Return subscription through the IPO storage boundary."""
    with session_factory() as session:
        row = get_ipo_subscription(session, issue_id, subscription_id)
        return _subscription_record(row) if row is not None else None


def list_subscriptions(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoSubscriptionRecord]:
    """Return the ordered subscriptions through the IPO storage boundary."""
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
    """Update subscription through the IPO storage boundary."""
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
    """Delete subscription through the IPO storage boundary."""
    with session_factory() as session:
        return delete_ipo_subscription_row(session, issue_id, subscription_id)


def _evaluation_record(score_row: Any, recommendation_row: Any) -> IpoEvaluationRecord:
    """Provide the evaluation record step used by the IPO workflow."""
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
    """Return evaluation through the IPO storage boundary."""
    with session_factory() as session:
        rows = get_ipo_evaluation_rows(session, issue_id, score_id)
        return _evaluation_record(*rows) if rows is not None else None


def list_evaluations(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoEvaluationRecord]:
    """Return the ordered evaluations through the IPO storage boundary."""
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
