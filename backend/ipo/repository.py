"""Own IPO transactions while exposing only detached, typed domain records.

Storage helpers below this module return SQLAlchemy rows. Public callers never
receive those session-bound objects: adapters convert validated DTOs into column
values and convert ORM rows back into frozen records before the session closes.
This is also the orchestration home for operations spanning scoring, downloads,
auditing, or multiple tables.

Beginner note:
The caller owns a transaction through ``session_factory`` and this module owns
the workflow around it. Keeping SQL construction in ``backend.storage`` means a
new UI, CLI, or test can reuse the same ownership and provenance checks without
learning SQLAlchemy or accidentally returning a session-bound ORM object.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Callable, Mapping
from contextlib import suppress
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from backend.audit import record_audit_event
from backend.config import get_settings
from backend.ipo.documents.downloader import (
    IpoDocumentDownloadError,
    IpoDocumentDownloadErrorCode,
    IpoDocumentDownloadResult,
    download_document_file,
    verify_cached_document_file,
)
from backend.ipo.financials.ratio_engine import IpoRatioAnalysis, calculate_ipo_ratios
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualExtractionRecord,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    Confidence,
    FinancialPeriodType,
    IpoCautionFlag,
    IpoCautionFlagReport,
    IpoCautionFlagStatus,
    IpoDocumentData,
    IpoDocumentParseStatus,
    IpoDocumentRecord,
    IpoEnrichmentSignalData,
    IpoEnrichmentSignalRecord,
    IpoEnrichmentSignalType,
    IpoEvaluationRecord,
    IpoExtractionProposalRecord,
    IpoExtractionProposalStatus,
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
from backend.ipo.scoring.recommendation import build_recommendation
from backend.ipo.scoring.score_model import score_ipo
from backend.observability import (
    EVENT_IPO_DOCUMENT_DOWNLOAD_COMPLETED,
    EVENT_IPO_DOCUMENT_DOWNLOAD_FAILED,
    EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED,
    EVENT_IPO_MANUAL_EXTRACTION_SUBMITTED,
    log_event,
)
from backend.scanning.result_contract import normalize_secret_safe_json
from backend.security import redact_text
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
    get_ipo_extraction_proposal,
    get_ipo_financial,
    get_ipo_issue,
    get_ipo_issue_by_sebi_key,
    get_ipo_manual_extraction,
    get_ipo_subscription,
    get_latest_ipo_evaluation_rows,
    get_latest_ipo_filing_date,
    get_latest_ipo_manual_extraction,
    get_latest_ipo_subscription,
    get_pending_ipo_extraction_proposal_for_document,
    insert_ipo_document,
    insert_ipo_enrichment_signals,
    insert_ipo_evaluation,
    insert_ipo_extraction_proposal,
    insert_ipo_financial,
    insert_ipo_issue,
    insert_ipo_manual_extraction,
    insert_ipo_subscription,
    list_ipo_document_rows,
    list_ipo_enrichment_signal_rows,
    list_ipo_evaluation_rows,
    list_ipo_extraction_proposal_rows,
    list_ipo_financial_rows,
    list_ipo_issue_rows,
    list_ipo_manual_extraction_rows,
    list_ipo_subscription_rows,
    list_unclaimed_ipo_issues_by_company_name,
    mark_ipo_extraction_proposal_reviewed,
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
    """Distinguish an absent parent/child row from invalid submitted data."""


def _utc(value: dt.datetime) -> dt.datetime:
    """Return a timezone-aware UTC timestamp from either database dialect.

    SQLite may return a timezone-aware column as a naive value, whereas
    PostgreSQL preserves its offset. Treating a naive persisted value as UTC
    keeps detached records consistent without applying the host timezone.
    """
    return value.replace(tzinfo=dt.UTC) if value.tzinfo is None else value.astimezone(dt.UTC)


def _issue_values(data: IpoIssueData) -> dict[str, Any]:
    """Translate a validated issue DTO into primitive ORM column values."""
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
    """Detach one issue ORM row and restore enums, money, and UTC types."""
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
    """Map source metadata without accepting caller-supplied cache provenance.

    Hash/path/status fields are intentionally absent: only the trusted downloader
    may produce them after validating the actual response bytes.
    """
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
    """Register source metadata under an existing issue and return it detached.

    The parent check produces a domain-level not-found error instead of leaking
    a database foreign-key exception to callers.
    """
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
    """Return one document only when both its issue and document ids match."""
    with session_factory() as session:
        row = get_ipo_document(session, issue_id, document_id)
        return _document_record(row) if row is not None else None


def list_documents(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoDocumentRecord]:
    """List a known issue's documents in stable type, URL, and id order."""
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
    """Delete one issue-owned metadata row without removing shared cache bytes."""
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


def _manual_email(value: str) -> str:
    """Normalize the authenticated actor email without accepting UI overrides.

    Beginner note:
    This value comes from the signed-in session, never the form. We casefold it so the
    same person is one audit identity regardless of capitalization, and apply only a
    minimal sanity check (non-empty, within the RFC 5321 320-char limit, exactly one
    ``@``) -- full email validation is the auth layer's job, not this adapter's.
    """
    email = str(value).strip().casefold()
    if not email or len(email) > 320 or email.count("@") != 1:
        raise IpoValidationError("entered_by_email must be a valid authenticated email.")
    return email


def _manual_header_values(
    data: IpoManualExtractionData,
    *,
    document: IpoDocumentRecord,
    entered_by_email: str,
    submitted_at: dt.datetime,
) -> dict[str, Any]:
    """Translate one validated form payload into immutable header columns.

    Beginner note:
    Keeping this mapping explicit prevents dataclass fields from being mass-assigned
    into ORM rows. Only reviewed source facts cross the domain/storage boundary.
    """
    return {
        "source_document_id": document.id,
        "source_document_url": document.document_url,
        "source_record_hash": document.record_hash,
        "source_content_sha256": document.content_sha256,
        "financial_amount_unit": data.financial_amount_unit.value,
        "issue_amount_unit": data.issue_amount_unit.value,
        "equity_share_unit": data.equity_share_unit.value,
        "net_worth": data.net_worth,
        "net_worth_page": data.net_worth_page,
        "total_debt": data.total_debt,
        "total_debt_page": data.total_debt_page,
        "cash": data.cash,
        "cash_page": data.cash_page,
        "cash_flow_from_operations": data.cash_flow_from_operations,
        "cash_flow_from_operations_page": data.cash_flow_from_operations_page,
        "equity_shares": data.equity_shares,
        "equity_shares_page": data.equity_shares_page,
        "eps": data.eps,
        "eps_page": data.eps_page,
        "nav_book_value": data.nav_book_value,
        "nav_book_value_page": data.nav_book_value_page,
        "objects_of_issue": data.objects_of_issue,
        "objects_of_issue_page": data.objects_of_issue_page,
        "fresh_issue_amount": data.fresh_issue_amount,
        "fresh_issue_amount_page": data.fresh_issue_amount_page,
        "ofs_amount": data.ofs_amount,
        "ofs_amount_page": data.ofs_amount_page,
        "promoter_holding_pre_issue": data.promoter_holding_pre_issue,
        "promoter_holding_pre_issue_page": data.promoter_holding_pre_issue_page,
        "promoter_holding_post_issue": data.promoter_holding_post_issue,
        "promoter_holding_post_issue_page": data.promoter_holding_post_issue_page,
        "total_assets": data.total_assets,
        "total_assets_page": data.total_assets_page,
        "current_liabilities": data.current_liabilities,
        "current_liabilities_page": data.current_liabilities_page,
        "post_issue_equity_shares": data.post_issue_equity_shares,
        "post_issue_equity_shares_page": data.post_issue_equity_shares_page,
        "entered_by_email": entered_by_email,
        "submitted_at": submitted_at,
    }


def _manual_period_values(data: IpoManualExtractionData) -> list[dict[str, Any]]:
    """Build the exactly three annual child rows in chronological order.

    ``data.periods`` is already sorted oldest-to-newest by the domain contract, so
    numbering them 1..3 with ``enumerate`` records a stable FY1/FY2/FY3 slot.

    Beginner note:
    IPO-005 PBT and finance cost travel with the same period row and their own
    pages; they are raw evidence, never precomputed EBIT or coverage values.
    """
    return [
        {
            "ordinal": ordinal,
            "period_end": period.period_end,
            "revenue": period.revenue,
            "revenue_page": period.revenue_page,
            "ebitda": period.ebitda,
            "ebitda_page": period.ebitda_page,
            "pat": period.pat,
            "pat_page": period.pat_page,
            "profit_before_tax": period.profit_before_tax,
            "profit_before_tax_page": period.profit_before_tax_page,
            "finance_cost": period.finance_cost,
            "finance_cost_page": period.finance_cost_page,
        }
        for ordinal, period in enumerate(data.periods, start=1)
    ]


def _manual_peer_values(data: IpoManualExtractionData) -> list[dict[str, Any]]:
    """Serialize allowlisted peer metrics as exact decimal strings for JSON.

    Beginner note:
    JSON has no decimal type, and storing a ratio as a float would reintroduce the
    binary rounding the domain worked to avoid. ``format(value, "f")`` writes the exact
    decimal as text (e.g. ``"21.4000"``); the detached record parses it straight back
    into ``Decimal`` on read.
    """
    return [
        {
            "company_name": peer.company_name,
            "company_key": peer.company_key,
            "source_page": peer.source_page,
            "metrics_json": {
                IpoPeerMetric(metric).value: format(value, "f")
                for metric, value in peer.metrics.items()
            },
        }
        for peer in data.peers
    ]


def _manual_record(row: Any) -> IpoManualExtractionRecord:
    """Detach an ORM revision and restore enums, decimals, periods, and peers.

    Beginner note:
    The returned frozen object is safe after the session closes. Nullable IPO-005
    fields are retained as ``None`` for legacy revisions rather than coerced to zero.
    """
    periods = tuple(
        IpoManualPeriodData(
            period_end=period.period_end,
            revenue=period.revenue,
            revenue_page=period.revenue_page,
            ebitda=period.ebitda,
            ebitda_page=period.ebitda_page,
            pat=period.pat,
            pat_page=period.pat_page,
            profit_before_tax=period.profit_before_tax,
            profit_before_tax_page=period.profit_before_tax_page,
            finance_cost=period.finance_cost,
            finance_cost_page=period.finance_cost_page,
        )
        for period in sorted(row.periods, key=lambda value: value.ordinal)
    )
    peers = tuple(
        IpoPeerValuationData(
            company_name=peer.company_name,
            source_page=peer.source_page,
            metrics={
                IpoPeerMetric(metric): value
                for metric, value in peer.metrics_json.items()
            },
        )
        for peer in sorted(row.peers, key=lambda value: (value.company_key, value.id))
    )
    return IpoManualExtractionRecord(
        id=row.id,
        issue_id=row.issue_id,
        source_document_id=row.source_document_id,
        source_document_url=row.source_document_url,
        source_record_hash=row.source_record_hash,
        source_content_sha256=row.source_content_sha256,
        financial_amount_unit=IpoAmountUnit(row.financial_amount_unit),
        issue_amount_unit=IpoAmountUnit(row.issue_amount_unit),
        equity_share_unit=IpoShareUnit(row.equity_share_unit),
        periods=periods,
        net_worth=row.net_worth,
        net_worth_page=row.net_worth_page,
        total_debt=row.total_debt,
        total_debt_page=row.total_debt_page,
        cash=row.cash,
        cash_page=row.cash_page,
        cash_flow_from_operations=row.cash_flow_from_operations,
        cash_flow_from_operations_page=row.cash_flow_from_operations_page,
        equity_shares=row.equity_shares,
        equity_shares_page=row.equity_shares_page,
        eps=row.eps,
        eps_page=row.eps_page,
        nav_book_value=row.nav_book_value,
        nav_book_value_page=row.nav_book_value_page,
        objects_of_issue=row.objects_of_issue,
        objects_of_issue_page=row.objects_of_issue_page,
        fresh_issue_amount=row.fresh_issue_amount,
        fresh_issue_amount_page=row.fresh_issue_amount_page,
        ofs_amount=row.ofs_amount,
        ofs_amount_page=row.ofs_amount_page,
        promoter_holding_pre_issue=row.promoter_holding_pre_issue,
        promoter_holding_pre_issue_page=row.promoter_holding_pre_issue_page,
        promoter_holding_post_issue=row.promoter_holding_post_issue,
        promoter_holding_post_issue_page=row.promoter_holding_post_issue_page,
        peers=peers,
        entered_by_email=row.entered_by_email,
        submitted_at=_utc(row.submitted_at),
        total_assets=row.total_assets,
        total_assets_page=row.total_assets_page,
        current_liabilities=row.current_liabilities,
        current_liabilities_page=row.current_liabilities_page,
        post_issue_equity_shares=row.post_issue_equity_shares,
        post_issue_equity_shares_page=row.post_issue_equity_shares_page,
    )


def submit_manual_extraction(
    issue_id: int,
    data: IpoManualExtractionData,
    *,
    entered_by_email: str,
    data_dir: Path | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    audit_recorder: AuditRecorder = record_audit_event,
    session_factory: SessionFactory = session_scope,
) -> IpoManualExtractionRecord:
    """Verify cached source bytes, then atomically append one complete revision.

    Beginner note:
    The first transaction reads and detaches source metadata. Hashing the PDF
    happens after that transaction closes, so a large file never holds a DB
    connection or lock. The second short transaction compares the source facts
    again before inserting all rows, closing the time-of-check/time-of-use gap.
    """
    actor = _manual_email(entered_by_email)
    submitted_at = now()
    if not isinstance(submitted_at, dt.datetime) or submitted_at.tzinfo is None:
        raise IpoValidationError("The manual-extraction clock must return a timezone-aware datetime.")
    submitted_at = submitted_at.astimezone(dt.UTC)

    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        row = get_ipo_document(session, issue_id, data.source_document_id)
        if row is None:
            raise IpoValidationError(
                f"Source document {data.source_document_id} does not belong to IPO issue {issue_id}."
            )
        document = _document_record(row)

    if document.document_type not in {"drhp", "rhp"}:
        raise IpoValidationError("Manual extraction accepts only a cached DRHP or RHP document.")
    cache_root = Path(data_dir) if data_dir is not None else get_settings().data_dir
    try:
        verified = verify_cached_document_file(document, data_dir=cache_root)
    except IpoDocumentDownloadError as exc:
        raise IpoValidationError("Manual extraction requires a verified cached PDF.") from exc

    with session_factory() as session:
        current = get_ipo_document(session, issue_id, data.source_document_id)
        if current is None:
            raise IpoValidationError("The selected IPO source document changed before submission.")
        if (
            current.document_type != document.document_type
            or current.document_url != document.document_url
            or current.content_sha256 != verified.content_sha256
            or current.file_path != verified.file_path
        ):
            raise IpoValidationError("The selected IPO source document changed before submission.")
        inserted = insert_ipo_manual_extraction(
            session,
            issue_id,
            _manual_header_values(
                data,
                document=document,
                entered_by_email=actor,
                submitted_at=submitted_at,
            ),
            _manual_period_values(data),
            _manual_peer_values(data),
        )
        record = _manual_record(inserted)

    metadata = {
        "issue_id": issue_id,
        "extraction_id": record.id,
        "document_id": data.source_document_id,
        "period_count": len(record.periods),
        "peer_count": len(record.peers),
    }
    log_event(
        logger,
        EVENT_IPO_MANUAL_EXTRACTION_SUBMITTED,
        issue_id=issue_id,
        extraction_id=record.id,
        document_id=data.source_document_id,
        period_count=len(record.periods),
        peer_count=len(record.peers),
    )
    # The committed extraction row is authoritative. Audit storage is useful
    # secondary evidence, but its outage must not make the UI report that the
    # already-successful submission failed.
    with suppress(Exception):
        audit_recorder(
            event=EVENT_IPO_MANUAL_EXTRACTION_SUBMITTED,
            user_email=actor,
            metadata=metadata,
            session_factory=session_factory,
        )
    return record


def get_manual_extraction(
    issue_id: int,
    extraction_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoManualExtractionRecord | None:
    """Return one detached immutable revision scoped to its owning issue."""
    with session_factory() as session:
        row = get_ipo_manual_extraction(session, issue_id, extraction_id)
        return _manual_record(row) if row is not None else None


def list_manual_extractions(
    issue_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> list[IpoManualExtractionRecord]:
    """List a known issue's revisions newest-first without exposing ORM rows."""
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [
            _manual_record(row)
            for row in list_ipo_manual_extraction_rows(session, issue_id)
        ]


def get_latest_manual_profile(
    issue_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoManualExtractionRecord | None:
    """Return the latest canonical raw-data profile without deriving scores."""
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        row = get_latest_ipo_manual_extraction(session, issue_id)
        return _manual_record(row) if row is not None else None


def get_latest_ipo_ratios(
    issue_id: int,
    *,
    session_factory: SessionFactory = session_scope,
) -> IpoRatioAnalysis | None:
    """Calculate ratios from one consistent issue/latest-revision snapshot.

    Beginner note:
    Both rows are detached inside the same short read transaction, then the pure
    engine runs after the session closes. The database therefore stores source
    evidence only, while this repeatable read model cannot hold locks during math.
    """
    with session_factory() as session:
        issue_row = get_ipo_issue(session, issue_id)
        if issue_row is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        profile_row = get_latest_ipo_manual_extraction(session, issue_id)
        issue = _issue_record(issue_row)
        profile = _manual_record(profile_row) if profile_row is not None else None
    if profile is None:
        return None
    return calculate_ipo_ratios(
        profile,
        price_band_high=issue.price_band_high,
        issue_updated_at=issue.updated_at,
    )


_STATUS_ORDER = {
    IpoStatus.DRHP_FILED: 0,
    IpoStatus.RHP_FILED: 1,
    IpoStatus.OPEN: 2,
    IpoStatus.CLOSED: 3,
    IpoStatus.LISTED: 4,
}


def _ingestion_issue_values(data: IpoFilingData) -> dict[str, Any]:
    """Build authoritative issue columns from one normalized official filing."""
    return {
        "company_name": data.company_name,
        "sebi_company_key": data.sebi_company_key,
        "issue_type": data.issue_type.value,
        "status": data.status.value,
        "source_url": data.source_url,
        "source_confidence": Confidence.HIGH.value,
    }


def _ingestion_document_values(data: IpoFilingData) -> dict[str, Any]:
    """Build metadata-only document columns; IPO-002 never supplies PDF bytes."""
    return {
        "document_type": data.document_type,
        "document_url": data.document_url,
        "source_url": data.source_url,
        "source_confidence": Confidence.HIGH.value,
        "filing_date": data.filing_date,
        "record_hash": data.record_hash,
    }


def _changed_values(row: Any, desired: dict[str, Any]) -> dict[str, Any]:
    """Return only differing columns so an idempotent scan performs no update."""
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
    """Return the newest filing date used as the next scan's overlap watermark.

    ``None`` means the database has no inventoried filings, so the job uses its
    documented 30-day bootstrap window instead.
    """
    with session_factory() as session:
        return get_latest_ipo_filing_date(session)


def _financial_values(data: IpoFinancialData) -> dict[str, Any]:
    """Normalize flexible period metrics into secret-safe JSON column values."""
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
    """Detach one financial ORM row and restore its typed period metadata."""
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
    """Reject financial provenance that points outside its parent IPO issue.

    ``None`` is valid for legacy/manual facts. A supplied id must resolve under
    the same issue, preventing another company's prospectus from being credited
    as the source of these financial metrics.
    """
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
    """Create one period after validating its issue and optional source owner."""
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
    """Return a detached financial period scoped to its parent issue."""
    with session_factory() as session:
        row = get_ipo_financial(session, issue_id, financial_id)
        return _financial_record(row) if row is not None else None


def list_financials(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoFinancialRecord]:
    """List newest financial periods first for a known parent issue."""
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
    """Replace one period's mutable facts after rechecking provenance ownership."""
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
    """Delete an issue-owned financial period, returning false when absent."""
    with session_factory() as session:
        return delete_ipo_financial_row(session, issue_id, financial_id)


def _subscription_values(data: IpoSubscriptionData) -> dict[str, Any]:
    """Translate one validated demand snapshot into primitive column values."""
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
    """Detach a subscription row and normalize its capture timestamp to UTC."""
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
    """Append one timestamped subscription snapshot to an existing issue."""
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
    """Return a detached subscription snapshot scoped to its issue."""
    with session_factory() as session:
        row = get_ipo_subscription(session, issue_id, subscription_id)
        return _subscription_record(row) if row is not None else None


def list_subscriptions(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoSubscriptionRecord]:
    """List demand snapshots newest-first so callers see current demand first."""
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
    """Replace one issue-owned demand snapshot or raise typed not-found."""
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
    """Delete one issue-owned snapshot and remain idempotent when absent."""
    with session_factory() as session:
        return delete_ipo_subscription_row(session, issue_id, subscription_id)


def _enrichment_signal_record(row: Any) -> IpoEnrichmentSignalRecord:
    """Reassemble one enrichment ORM row into a detached typed record."""
    return IpoEnrichmentSignalRecord(
        id=row.id,
        issue_id=row.issue_id,
        signal_type=IpoEnrichmentSignalType(row.signal_type),
        captured_at=_utc(row.captured_at),
        query_text=row.query_text,
        payload=tuple(dict(entry) for entry in row.payload_json),
        parsed_value=row.parsed_value,
        quarantined=bool(row.quarantined),
        confidence=Confidence(row.confidence),
        source_policy=row.source_policy,
        created_at=_utc(row.created_at),
    )


def record_enrichment_signals(
    issue_id: int,
    signals: list[IpoEnrichmentSignalData],
    *,
    session_factory: SessionFactory = session_scope,
) -> list[IpoEnrichmentSignalRecord]:
    """Persist one already-quarantined enrichment batch for a known issue.

    Beginner note:
        The collector validates and quarantine-scans everything before this
        function runs, so persistence is a plain typed hand-off: verify the
        parent issue exists, stage the batch in one transaction, and return
        detached records. Payloads still pass through the secret-safe JSON
        normalizer as a last line of defense for every stored sink.
    """
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        values_list = [
            {
                "signal_type": signal.signal_type.value,
                "captured_at": signal.captured_at,
                "query_text": signal.query_text,
                "payload_json": normalize_secret_safe_json(
                    [dict(entry) for entry in signal.payload]
                ),
                "parsed_value": signal.parsed_value,
                "quarantined": signal.quarantined,
                "confidence": signal.confidence.value,
                "source_policy": signal.source_policy,
            }
            for signal in signals
        ]
        rows = insert_ipo_enrichment_signals(session, issue_id, values_list)
        return [_enrichment_signal_record(row) for row in rows]


def list_enrichment_signals(
    issue_id: int,
    *,
    signal_type: IpoEnrichmentSignalType | None = None,
    since: dt.datetime | None = None,
    session_factory: SessionFactory = session_scope,
) -> list[IpoEnrichmentSignalRecord]:
    """List one issue's enrichment signals newest-first with optional filters.

    ``since`` bounds staleness in SQL (the GMP factor only trusts recent
    observations) instead of loading dead history into memory.
    """
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        rows = list_ipo_enrichment_signal_rows(
            session,
            issue_id,
            signal_type=signal_type.value if signal_type is not None else None,
            since=since,
        )
        return [_enrichment_signal_record(row) for row in rows]


# Singleton value fields shared by the proposal payload and the manual data
# contract. Every name below appears in the payload with a paired
# ``<name>_page`` citation, exactly like a hand-entered submission.
_PROPOSAL_VALUE_FIELDS = (
    "net_worth",
    "total_debt",
    "cash",
    "cash_flow_from_operations",
    "equity_shares",
    "eps",
    "nav_book_value",
    "fresh_issue_amount",
    "ofs_amount",
    "promoter_holding_pre_issue",
    "promoter_holding_post_issue",
    "total_assets",
    "current_liabilities",
    "post_issue_equity_shares",
)


def _proposal_payload_to_manual_data(
    payload: Mapping[str, Any], source_document_id: int
) -> IpoManualExtractionData:
    """Reconstruct the strict manual-extraction contract from a proposal payload.

    Beginner note:
        This is the fail-closed heart of the review flow. The payload is data
        under review, so nothing in it is trusted: every value is re-parsed
        into ``Decimal``/``date`` and the resulting ``IpoManualExtractionData``
        runs the exact ``__post_init__`` validation a hand-entered submission
        runs. A corrupted or tampered payload therefore raises here and can
        never become an immutable revision.
    """
    try:
        periods = tuple(
            IpoManualPeriodData(
                period_end=dt.date.fromisoformat(str(entry["period_end"])),
                revenue=Decimal(str(entry["revenue"])),
                revenue_page=int(entry["revenue_page"]),
                ebitda=Decimal(str(entry["ebitda"])),
                ebitda_page=int(entry["ebitda_page"]),
                pat=Decimal(str(entry["pat"])),
                pat_page=int(entry["pat_page"]),
                profit_before_tax=Decimal(str(entry["profit_before_tax"])),
                profit_before_tax_page=int(entry["profit_before_tax_page"]),
                finance_cost=Decimal(str(entry["finance_cost"])),
                finance_cost_page=int(entry["finance_cost_page"]),
            )
            for entry in payload["periods"]
        )
        peers = tuple(
            IpoPeerValuationData(
                company_name=str(entry["company_name"]),
                source_page=int(entry["source_page"]),
                metrics={
                    str(metric): Decimal(str(value))
                    for metric, value in dict(entry["metrics"]).items()
                },
            )
            for entry in payload["peers"]
        )
        values: dict[str, Any] = {}
        for name in _PROPOSAL_VALUE_FIELDS:
            values[name] = Decimal(str(payload[name]))
            values[f"{name}_page"] = int(payload[f"{name}_page"])
        return IpoManualExtractionData(
            source_document_id=source_document_id,
            financial_amount_unit=IpoAmountUnit(str(payload["financial_amount_unit"])),
            issue_amount_unit=IpoAmountUnit(str(payload["issue_amount_unit"])),
            equity_share_unit=IpoShareUnit(str(payload["equity_share_unit"])),
            periods=periods,
            objects_of_issue=str(payload["objects_of_issue"]),
            objects_of_issue_page=int(payload["objects_of_issue_page"]),
            peers=peers,
            **values,
        )
    except IpoValidationError:
        raise
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        # Only the exception class name survives: a malformed payload could
        # contain arbitrary text and must not leak into errors or logs.
        raise IpoValidationError(
            f"Proposal payload is malformed ({type(exc).__name__}); it cannot "
            "become a manual-extraction revision."
        ) from exc


def _extraction_proposal_record(row: Any) -> IpoExtractionProposalRecord:
    """Reassemble one proposal ORM row into a detached typed record."""
    return IpoExtractionProposalRecord(
        id=row.id,
        issue_id=row.issue_id,
        document_id=row.document_id,
        company_name=row.issue.company_name,
        document_url=row.document.document_url,
        status=IpoExtractionProposalStatus(row.status),
        payload=dict(row.payload_json),
        confidence=Confidence(row.confidence),
        needs_review_reasons=tuple(row.needs_review_reasons_json),
        model_version=row.model_version,
        agent_model=row.agent_model,
        source_content_sha256=row.source_content_sha256,
        page_count=row.page_count,
        created_at=_utc(row.created_at),
        reviewed_by_email=row.reviewed_by_email,
        reviewed_at=_utc(row.reviewed_at) if row.reviewed_at is not None else None,
        review_note=row.review_note,
        manual_extraction_id=row.manual_extraction_id,
    )


def submit_extraction_proposal(
    issue_id: int,
    document_id: int,
    *,
    payload: Mapping[str, Any],
    confidence: Confidence,
    needs_review_reasons: tuple[str, ...],
    model_version: str,
    agent_model: str,
    source_content_sha256: str,
    page_count: int,
    session_factory: SessionFactory = session_scope,
) -> IpoExtractionProposalRecord:
    """Queue one AI-proposed extraction for human review.

    Beginner note:
        The payload is validated for *shape* here (it must reconstruct into
        the strict manual contract) before anything is stored, so the review
        queue can never hold a proposal that would be impossible to approve.
        One pending proposal per document keeps the queue free of duplicates.
    """
    _proposal_payload_to_manual_data(payload, document_id)
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        if get_ipo_document(session, issue_id, document_id) is None:
            raise IpoValidationError(
                f"Source document {document_id} does not belong to IPO issue {issue_id}."
            )
        if get_pending_ipo_extraction_proposal_for_document(session, document_id) is not None:
            raise IpoValidationError(
                f"Document {document_id} already has a pending extraction proposal."
            )
        row = insert_ipo_extraction_proposal(
            session,
            issue_id,
            document_id,
            {
                "status": IpoExtractionProposalStatus.PENDING.value,
                "payload_json": normalize_secret_safe_json(dict(payload)),
                "confidence": _parse_confidence(confidence).value,
                "needs_review_reasons_json": [str(reason) for reason in needs_review_reasons],
                "model_version": str(model_version),
                "agent_model": str(agent_model),
                "source_content_sha256": str(source_content_sha256),
                "page_count": int(page_count),
            },
        )
        return _extraction_proposal_record(row)


def _parse_confidence(value: Confidence | str) -> Confidence:
    """Accept an enum or its string value and return one strict member."""
    return value if isinstance(value, Confidence) else Confidence(str(value))


def list_extraction_proposals(
    *,
    issue_id: int | None = None,
    status: IpoExtractionProposalStatus | None = None,
    session_factory: SessionFactory = session_scope,
) -> list[IpoExtractionProposalRecord]:
    """List proposals newest-first, optionally narrowed by issue or status."""
    with session_factory() as session:
        rows = list_ipo_extraction_proposal_rows(
            session,
            issue_id=issue_id,
            status=status.value if status is not None else None,
        )
        return [_extraction_proposal_record(row) for row in rows]


def approve_extraction_proposal(
    proposal_id: int,
    *,
    reviewed_by_email: str,
    data_dir: Path | None = None,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    audit_recorder: AuditRecorder = record_audit_event,
    session_factory: SessionFactory = session_scope,
) -> IpoManualExtractionRecord:
    """Convert one pending proposal into an immutable manual-extraction revision.

    Beginner note:
        Approval is an attestation: the reviewer becomes ``entered_by_email``
        on the resulting revision, exactly as if they had typed the values
        themselves. The conversion replays the full manual-submission path —
        strict payload validation plus re-verification of the cached PDF bytes
        — so scoring can never tell (and never needs to know) that an agent
        drafted the numbers. If another reviewer decided the same proposal
        concurrently, the marking step fails loudly; the freshly appended
        revision remains as append-only history and is reported in the error.
    """
    reviewer = _manual_email(reviewed_by_email)
    with session_factory() as session:
        row = get_ipo_extraction_proposal(session, proposal_id)
        if row is None:
            raise IpoNotFoundError(f"Extraction proposal {proposal_id} was not found.")
        record = _extraction_proposal_record(row)
    if record.status is not IpoExtractionProposalStatus.PENDING:
        raise IpoValidationError(
            f"Extraction proposal {proposal_id} was already {record.status.value}."
        )

    data = _proposal_payload_to_manual_data(record.payload, record.document_id)
    revision = submit_manual_extraction(
        record.issue_id,
        data,
        entered_by_email=reviewer,
        data_dir=data_dir,
        now=now,
        audit_recorder=audit_recorder,
        session_factory=session_factory,
    )

    with session_factory() as session:
        marked = mark_ipo_extraction_proposal_reviewed(
            session,
            proposal_id,
            {
                "status": IpoExtractionProposalStatus.APPROVED.value,
                "reviewed_by_email": reviewer,
                "reviewed_at": now().astimezone(dt.UTC),
                "manual_extraction_id": revision.id,
            },
        )
        if marked is None:
            raise IpoValidationError(
                f"Extraction proposal {proposal_id} was reviewed concurrently; "
                f"manual revision {revision.id} was still appended and remains "
                "in the immutable history."
            )
    log_event(
        logger,
        EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED,
        proposal_id=proposal_id,
        issue_id=record.issue_id,
        decision=IpoExtractionProposalStatus.APPROVED.value,
        manual_extraction_id=revision.id,
    )
    audit_recorder(
        event=EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED,
        user_email=reviewer,
        metadata={
            "proposal_id": proposal_id,
            "issue_id": record.issue_id,
            "decision": IpoExtractionProposalStatus.APPROVED.value,
            "manual_extraction_id": revision.id,
        },
        session_factory=session_factory,
    )
    return revision


def reject_extraction_proposal(
    proposal_id: int,
    *,
    reviewed_by_email: str,
    reason: str,
    now: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
    audit_recorder: AuditRecorder = record_audit_event,
    session_factory: SessionFactory = session_scope,
) -> IpoExtractionProposalRecord:
    """Reject one pending proposal, keeping it as attributable audit history."""
    reviewer = _manual_email(reviewed_by_email)
    note = str(reason).strip()
    if not note:
        raise IpoValidationError("A rejection requires a non-empty reason.")
    with session_factory() as session:
        marked = mark_ipo_extraction_proposal_reviewed(
            session,
            proposal_id,
            {
                "status": IpoExtractionProposalStatus.REJECTED.value,
                "reviewed_by_email": reviewer,
                "reviewed_at": now().astimezone(dt.UTC),
                "review_note": str(redact_text(note)),
            },
        )
        if marked is None:
            raise IpoValidationError(
                f"Extraction proposal {proposal_id} is not pending review."
            )
        record = _extraction_proposal_record(marked)
    log_event(
        logger,
        EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED,
        proposal_id=proposal_id,
        issue_id=record.issue_id,
        decision=IpoExtractionProposalStatus.REJECTED.value,
    )
    audit_recorder(
        event=EVENT_IPO_EXTRACTION_PROPOSAL_REVIEWED,
        user_email=reviewer,
        metadata={
            "proposal_id": proposal_id,
            "issue_id": record.issue_id,
            "decision": IpoExtractionProposalStatus.REJECTED.value,
        },
        session_factory=session_factory,
    )
    return record


def _evaluation_record(score_row: Any, recommendation_row: Any) -> IpoEvaluationRecord:
    """Reassemble two immutable ORM rows into one detached public evaluation."""
    result = IpoRecommendationResult(
        company_name=score_row.issue.company_name,
        score=score_row.total_score,
        recommendation=Recommendation(recommendation_row.recommendation),
        recommendation_type=recommendation_row.recommendation_type,
        confidence=Confidence(recommendation_row.confidence),
        reasons=tuple(recommendation_row.reasons_json),
        missing_data=tuple(recommendation_row.missing_data_json),
        source_documents=tuple(recommendation_row.source_documents_json),
        # Legacy ipo-001-v1 rows carry the server-default empty list here, so
        # this rebuild works identically for pre- and post-IPO-006 history.
        caution_flags=tuple(
            IpoCautionFlag(
                name=entry["name"],
                status=IpoCautionFlagStatus(entry["status"]),
                evidence=entry["evidence"],
            )
            for entry in recommendation_row.caution_flags_json
        ),
    )
    return IpoEvaluationRecord(
        issue_id=score_row.issue_id,
        score_id=score_row.id,
        recommendation_id=recommendation_row.id,
        model_version=score_row.model_version,
        scored_at=_utc(score_row.scored_at),
        result=result,
        inputs_fingerprint=score_row.inputs_fingerprint,
    )


def evaluate_issue(
    issue_id: int,
    score_input: IpoScoreInput,
    *,
    caution_flags: IpoCautionFlagReport | None = None,
    inputs_fingerprint: str | None = None,
    model_version: str = "ipo-001-v1",
    session_factory: SessionFactory = session_scope,
) -> IpoEvaluationRecord:
    """Compute and atomically persist one immutable score/verdict pair.

    Beginner note:
        The three IPO-006 keyword arguments are optional so IPO-001 callers
        keep their exact behavior. The scoring service passes a caution-flag
        report (enforced inside ``build_recommendation``), the SHA-256
        fingerprint of the evidence it consumed (the screener's idempotency
        anchor), and its own model version; all three are persisted with the
        immutable pair.
    """
    score_result = score_ipo(score_input)
    recommendation = build_recommendation(score_result, caution_flags=caution_flags)

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
            "model_version": model_version,
            "inputs_fingerprint": inputs_fingerprint,
        }
        recommendation_values = {
            "recommendation": recommendation.recommendation.value,
            "recommendation_type": recommendation.recommendation_type,
            "confidence": recommendation.confidence.value,
            "reasons_json": list(recommendation.reasons),
            "missing_data_json": list(recommendation.missing_data),
            "source_documents_json": list(recommendation.source_documents),
            "caution_flags_json": [
                {"name": flag.name, "status": flag.status.value, "evidence": flag.evidence}
                for flag in recommendation.caution_flags
            ],
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
    """Load one immutable score/recommendation pair by issue and score id."""
    with session_factory() as session:
        rows = get_ipo_evaluation_rows(session, issue_id, score_id)
        return _evaluation_record(*rows) if rows is not None else None


def list_evaluations(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> list[IpoEvaluationRecord]:
    """List a known issue's append-only evaluation history newest-first."""
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        return [
            _evaluation_record(score, recommendation)
            for score, recommendation in list_ipo_evaluation_rows(session, issue_id)
        ]


def get_latest_evaluation(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> IpoEvaluationRecord | None:
    """Return the newest complete evaluation record for one issue, if any.

    The IPO-006 scoring service compares its freshly computed inputs
    fingerprint against this record to decide whether a re-score would be a
    byte-identical no-op, which is what makes ``run_ipo_screener`` idempotent.
    """
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        rows = get_latest_ipo_evaluation_rows(session, issue_id)
        return _evaluation_record(*rows) if rows is not None else None


def get_latest_subscription(
    issue_id: int, *, session_factory: SessionFactory = session_scope
) -> IpoSubscriptionRecord | None:
    """Return only the newest demand snapshot for one issue, if any."""
    with session_factory() as session:
        if get_ipo_issue(session, issue_id) is None:
            raise IpoNotFoundError(f"IPO issue {issue_id} was not found.")
        row = get_latest_ipo_subscription(session, issue_id)
        return _subscription_record(row) if row is not None else None


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
