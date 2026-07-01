"""Headless IPO-002 command for inventorying official SEBI filing listings.

Run the incremental default window with:

    python -m backend.jobs.scan_ipo_filings

No code in this job downloads or parses a prospectus PDF. The source adapter
returns filing metadata; the repository atomically stores one category at a time.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TextIO

from backend.audit import record_audit_event
from backend.ipo.models import IpoIngestionSummary, SebiFilingCategory
from backend.ipo.repository import get_latest_filing_date, ingest_filings
from backend.ipo.sources.sebi import build_filing_data, fetch_sebi_filings
from backend.observability import (
    EVENT_IPO_FILING_CATEGORY_COMPLETED,
    EVENT_IPO_FILING_CATEGORY_FAILED,
    EVENT_IPO_FILING_SCAN_COMPLETED,
    EVENT_IPO_FILING_SCAN_STARTED,
    configure_logging,
    log_event,
)
from backend.storage.database import ensure_database_schema, session_scope

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class IpoFilingCategoryOutcome:
    """Result of fetching and persisting one independently atomic category."""

    category: SebiFilingCategory
    fetched: int = 0
    summary: IpoIngestionSummary | None = None
    error_type: str | None = None


@dataclass(frozen=True)
class IpoFilingJobOutcome:
    """Overall command result, including partial-category failure state."""

    from_date: dt.date | None = None
    to_date: dt.date = field(default_factory=dt.date.today)
    categories: tuple[IpoFilingCategoryOutcome, ...] = ()
    fatal: bool = False

    @property
    def exit_code(self) -> int:
        """Return a nonzero process status when any category or fatal setup failed."""
        return int(self.fatal or any(item.error_type for item in self.categories))


def _print_category(out: TextIO, outcome: IpoFilingCategoryOutcome) -> None:
    """Provide the print category step used by the IPO workflow."""
    if outcome.error_type:
        print(
            f"[ipo-filings] category={outcome.category.value} FAILED "
            f"error_type={outcome.error_type}",
            file=out,
            flush=True,
        )
        return
    summary = outcome.summary or IpoIngestionSummary()
    print(
        f"[ipo-filings] category={outcome.category.value} fetched={outcome.fetched} "
        f"issues_created={summary.issues_created} issues_updated={summary.issues_updated} "
        f"documents_created={summary.documents_created} "
        f"documents_updated={summary.documents_updated} unchanged={summary.unchanged}",
        file=out,
        flush=True,
    )


def run_scan_ipo_filings(
    *,
    from_date: dt.date | None = None,
    to_date: dt.date | None = None,
    full_history: bool = False,
    today: dt.date | None = None,
    ensure_schema: Callable[[], object] = ensure_database_schema,
    latest_filing_date: Callable[..., dt.date | None] = get_latest_filing_date,
    fetcher: Callable[..., Any] = fetch_sebi_filings,
    ingestion: Callable[..., IpoIngestionSummary] = ingest_filings,
    audit_recorder: Callable[..., bool] = record_audit_event,
    session_factory: Any = session_scope,
    output: TextIO | None = None,
) -> IpoFilingJobOutcome:
    """Fetch all fixed categories and persist each successful one independently."""
    out = output or sys.stdout
    upper_bound = to_date or today or dt.date.today()
    try:
        if ensure_schema() is False:
            raise RuntimeError("database schema bootstrap failed")
        if full_history:
            lower_bound = None
        elif from_date is not None:
            lower_bound = from_date
        else:
            watermark = latest_filing_date(session_factory=session_factory)
            lower_bound = (watermark - dt.timedelta(days=7)) if watermark else (
                upper_bound - dt.timedelta(days=30)
            )
        if lower_bound is not None and lower_bound > upper_bound:
            raise ValueError("from_date cannot be after to_date")
    except Exception as exc:  # noqa: BLE001 - command boundary becomes exit code
        error_type = type(exc).__name__
        print(f"[ipo-filings] FAILED error_type={error_type}", file=out, flush=True)
        log_event(
            logger,
            EVENT_IPO_FILING_SCAN_COMPLETED,
            level=logging.ERROR,
            fatal=True,
            error_type=error_type,
        )
        return IpoFilingJobOutcome(
            from_date=from_date,
            to_date=upper_bound,
            fatal=True,
        )

    log_event(
        logger,
        EVENT_IPO_FILING_SCAN_STARTED,
        from_date=lower_bound.isoformat() if lower_bound else None,
        to_date=upper_bound.isoformat(),
        full_history=full_history,
    )
    outcomes: list[IpoFilingCategoryOutcome] = []
    for category in SebiFilingCategory:
        try:
            parsed = tuple(fetcher(category, lower_bound, upper_bound))
            normalized = tuple(build_filing_data(filing) for filing in parsed)
            summary = ingestion(normalized, session_factory=session_factory)
            category_outcome = IpoFilingCategoryOutcome(
                category=category,
                fetched=len(parsed),
                summary=summary,
            )
            log_event(
                logger,
                EVENT_IPO_FILING_CATEGORY_COMPLETED,
                category=category.value,
                fetched=len(parsed),
                documents_created=summary.documents_created,
                documents_updated=summary.documents_updated,
                unchanged=summary.unchanged,
            )
        except Exception as exc:  # noqa: BLE001 - one category must not block the rest
            # Exception messages can contain hostile HTML or URLs. Persist/log only
            # the stable class name and bounded request context, never ``str(exc)``.
            error_type = type(exc).__name__
            category_outcome = IpoFilingCategoryOutcome(
                category=category,
                error_type=error_type,
            )
            metadata = {
                "category": category.value,
                "error_type": error_type,
                "from_date": lower_bound.isoformat() if lower_bound else None,
                "to_date": upper_bound.isoformat(),
            }
            log_event(
                logger,
                EVENT_IPO_FILING_CATEGORY_FAILED,
                level=logging.ERROR,
                category=category.value,
                error_type=error_type,
                from_date=lower_bound.isoformat() if lower_bound else None,
                to_date=upper_bound.isoformat(),
            )
            audit_recorder(
                event=EVENT_IPO_FILING_CATEGORY_FAILED,
                user_email=None,
                metadata=metadata,
                level=logging.ERROR,
                session_factory=session_factory,
            )
        outcomes.append(category_outcome)
        _print_category(out, category_outcome)

    result = IpoFilingJobOutcome(
        from_date=lower_bound,
        to_date=upper_bound,
        categories=tuple(outcomes),
    )
    log_event(
        logger,
        EVENT_IPO_FILING_SCAN_COMPLETED,
        failed_categories=sum(item.error_type is not None for item in outcomes),
        successful_categories=sum(item.error_type is None for item in outcomes),
        exit_code=result.exit_code,
    )
    return result


def _parse_iso_date(value: str) -> dt.date:
    """Parse and validate iso date into its typed representation."""
    try:
        return dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO date YYYY-MM-DD") from exc


def main(
    argv: Sequence[str] | None = None,
    *,
    job_runner: Callable[..., IpoFilingJobOutcome] = run_scan_ipo_filings,
) -> int:
    """Parse CLI dates/history mode and return the scheduler-facing exit code."""
    parser = argparse.ArgumentParser(
        description="Inventory official SEBI DRHP, RHP, and final-offer filing listings."
    )
    lower_bound = parser.add_mutually_exclusive_group()
    lower_bound.add_argument("--from-date", type=_parse_iso_date, default=None)
    lower_bound.add_argument("--full-history", action="store_true")
    parser.add_argument("--to-date", type=_parse_iso_date, default=None)
    args = parser.parse_args(argv)

    configure_logging()
    outcome = job_runner(
        from_date=args.from_date,
        to_date=args.to_date,
        full_history=args.full_history,
    )
    return int(outcome.exit_code)


if __name__ == "__main__":  # pragma: no cover - exercised through main() tests
    raise SystemExit(main())
