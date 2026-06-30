"""IPO-002 headless SEBI filing scan job tests."""

from __future__ import annotations

import datetime as dt
import importlib
import io

from sqlalchemy import select

from backend.ipo.models import IpoIngestionSummary, SebiFiling, SebiFilingCategory
from backend.storage import AuditLog


def _filing(category: SebiFilingCategory) -> SebiFiling:
    return SebiFiling(
        category=category,
        title="Example Limited - Prospectus",
        filing_date=dt.date(2026, 6, 29),
        document_url=f"https://www.sebi.gov.in/filings/{category.value}.html",
        source_url=(
            "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?"
            f"doListing=yes&sid=3&smid={10 + list(SebiFilingCategory).index(category)}&ssid=15"
        ),
    )


def test_default_window_overlaps_watermark_by_seven_days(file_session_factory) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")
    fetch_calls: list[tuple[object, object, object]] = []
    ingested: list[tuple[object, ...]] = []

    def fetcher(category, from_date, to_date):
        fetch_calls.append((category, from_date, to_date))
        return (_filing(category),)

    def ingestion(filings, *, session_factory):
        del session_factory
        ingested.append(tuple(filings))
        return IpoIngestionSummary(received=1, issues_created=1, documents_created=1)

    outcome = job.run_scan_ipo_filings(
        today=dt.date(2026, 6, 30),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: dt.date(2026, 6, 20),
        fetcher=fetcher,
        ingestion=ingestion,
        session_factory=file_session_factory,
        output=io.StringIO(),
    )

    assert outcome.exit_code == 0
    assert len(fetch_calls) == 3
    assert all(call[1:] == (dt.date(2026, 6, 13), dt.date(2026, 6, 30)) for call in fetch_calls)
    assert len(ingested) == 3


def test_empty_database_defaults_to_thirty_day_window(file_session_factory) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")
    windows: list[tuple[dt.date | None, dt.date]] = []

    def fetcher(_category, from_date, to_date):
        windows.append((from_date, to_date))
        return ()

    job.run_scan_ipo_filings(
        today=dt.date(2026, 6, 30),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: None,
        fetcher=fetcher,
        ingestion=lambda _filings, **_kwargs: IpoIngestionSummary(),
        session_factory=file_session_factory,
        output=io.StringIO(),
    )

    assert windows == [(dt.date(2026, 5, 31), dt.date(2026, 6, 30))] * 3


def test_full_history_has_no_lower_bound(file_session_factory) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")
    windows: list[dt.date | None] = []

    def fetcher(_category, from_date, _to_date):
        windows.append(from_date)
        return ()

    job.run_scan_ipo_filings(
        full_history=True,
        today=dt.date(2026, 6, 30),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: dt.date(2026, 6, 29),
        fetcher=fetcher,
        ingestion=lambda _filings, **_kwargs: IpoIngestionSummary(),
        session_factory=file_session_factory,
        output=io.StringIO(),
    )

    assert windows == [None, None, None]


def test_failed_category_is_audited_redacted_and_does_not_block_others(
    file_session_factory,
) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")
    output = io.StringIO()
    persisted: list[SebiFilingCategory] = []
    audits: list[dict[str, object]] = []

    def fetcher(category, _from_date, _to_date):
        if category is SebiFilingCategory.RHP:
            raise RuntimeError("access_token=do-not-leak hostile response body")
        return (_filing(category),)

    def ingestion(filings, **_kwargs):
        persisted.append(SebiFilingCategory(filings[0].document_type))
        return IpoIngestionSummary(received=1, documents_created=1)

    def audit_recorder(**kwargs):
        audits.append(kwargs)
        return True

    outcome = job.run_scan_ipo_filings(
        today=dt.date(2026, 6, 30),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: None,
        fetcher=fetcher,
        ingestion=ingestion,
        audit_recorder=audit_recorder,
        session_factory=file_session_factory,
        output=output,
    )

    assert outcome.exit_code == 1
    assert persisted == [SebiFilingCategory.DRHP, SebiFilingCategory.FINAL_OFFER]
    assert len(audits) == 1
    assert audits[0]["event"] == "ipo_filing_category_failed"
    assert audits[0]["metadata"]["category"] == "rhp"
    assert audits[0]["metadata"]["error_type"] == "RuntimeError"
    rendered = output.getvalue() + repr(audits)
    assert "do-not-leak" not in rendered
    assert "hostile response body" not in rendered


def test_main_parses_dates_and_full_history(monkeypatch) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")
    captured: list[dict[str, object]] = []

    def runner(**kwargs):
        captured.append(kwargs)
        return job.IpoFilingJobOutcome(
            from_date=kwargs.get("from_date"),
            to_date=kwargs.get("to_date") or dt.date(2026, 6, 30),
        )

    monkeypatch.setattr(job, "configure_logging", lambda: None)
    assert job.main(
        ["--from-date", "2026-06-01", "--to-date", "2026-06-30"],
        job_runner=runner,
    ) == 0
    assert captured[0]["from_date"] == dt.date(2026, 6, 1)
    assert captured[0]["to_date"] == dt.date(2026, 6, 30)

    assert job.main(["--full-history"], job_runner=runner) == 0
    assert captured[1]["full_history"] is True


def test_failed_category_writes_durable_secret_safe_system_audit(file_session_factory) -> None:
    job = importlib.import_module("backend.jobs.scan_ipo_filings")

    def fetcher(category, _from_date, _to_date):
        if category is SebiFilingCategory.DRHP:
            raise RuntimeError("password=never-store-this hostile html")
        return ()

    outcome = job.run_scan_ipo_filings(
        today=dt.date(2026, 6, 30),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: None,
        fetcher=fetcher,
        ingestion=lambda _filings, **_kwargs: IpoIngestionSummary(),
        session_factory=file_session_factory,
        output=io.StringIO(),
    )

    assert outcome.exit_code == 1
    with file_session_factory() as session:
        audit = session.scalar(select(AuditLog))
        assert audit is not None
        assert audit.event == "ipo_filing_category_failed"
        assert audit.user_email is None
        assert audit.metadata_json == {
            "category": "drhp",
            "error_type": "RuntimeError",
            "from_date": "2026-05-31",
            "to_date": "2026-06-30",
        }
        assert "never-store-this" not in repr(audit.metadata_json)
