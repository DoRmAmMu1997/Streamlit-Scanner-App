"""IPO pipeline end-to-end scenario test (TEST-006).

Every IPO stage already has a focused suite (ingestion, downloader, manual
extraction, ratio engine, scorecard, verdict) — but each one starts from a
hand-built fixture, so nothing proved the SEAMS: that what ingestion persists
is exactly what the downloader needs, that the downloaded cache is exactly
what manual extraction verifies, and that the extraction snapshot is exactly
what ratios and the scorecard consume. This module walks one company through
the whole pipeline on real repository functions and a real database, faking
only true externals (SEBI HTTP, DNS, clocks, the audit sink).

Beginner note:
The strongest assertion here is provenance continuity: the SHA-256 digest of
the faked PDF bytes must surface, unchanged, in the download result, the
document row, the manual-extraction revision, and the ratio analysis. If any
stage silently re-read different bytes (or skipped verification), that chain
would break.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import socket
from collections.abc import Iterator
from decimal import Decimal

import pytest

from backend.ipo.documents.downloader import download_document_file
from backend.ipo.financials.ratio_engine import IpoRatioName, IpoRatioStatus
from backend.ipo.manual_extraction import (
    IpoAmountUnit,
    IpoManualExtractionData,
    IpoManualPeriodData,
    IpoPeerMetric,
    IpoPeerValuationData,
    IpoShareUnit,
)
from backend.ipo.models import (
    Confidence,
    FactorAssessment,
    IpoDocumentParseStatus,
    IpoIssueData,
    IpoScoreInput,
    IpoStatus,
    SebiFiling,
    SebiFilingCategory,
)
from backend.ipo.repository import (
    download_document,
    evaluate_issue,
    get_latest_ipo_ratios,
    get_latest_recommendation,
    ingest_filings,
    list_documents,
    list_issues,
    submit_manual_extraction,
    update_issue,
)
from backend.jobs.scan_ipo_filings import run_scan_ipo_filings

PDF_BYTES = b"%PDF-1.7\nend-to-end fixture prospectus\n%%EOF\n"
DETAIL_URL = "https://www.sebi.gov.in/filings/public-issues/example-rhp.html"
PDF_URL = "https://www.sebi.gov.in/sebi_data/attachdocs/example.pdf"
LISTING_URL = (
    "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes&sid=3&smid=11&ssid=15"
)


class _FakeResponse:
    """Streaming response double mirroring the downloader suite's shape."""

    def __init__(self, body: bytes, *, content_type: str) -> None:
        """Store deterministic bytes and headers; no live I/O ever happens."""
        self.body = body
        self.status_code = 200
        self.headers = {"Content-Type": content_type}

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield bounded chunks just like ``requests.Response.iter_content``."""
        for offset in range(0, len(self.body), chunk_size):
            yield self.body[offset : offset + chunk_size]

    def close(self) -> None:
        """Accept the downloader's connection cleanup without side effects."""


class _FakeSession:
    """FIFO HTTP double: detail page first, then the prospectus PDF."""

    def __init__(self, outcomes: list[_FakeResponse]) -> None:
        """Queue the programmed responses and record every requested URL."""
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(self, url: str, **_kwargs: object) -> _FakeResponse:
        """Return the next programmed response for the recorded URL."""
        self.calls.append(url)
        return self.outcomes.pop(0)

    def close(self) -> None:
        """Accept ownership cleanup when the downloader closes the session."""


def _public_resolver(host: str, port: int, **_kwargs: object) -> list[tuple[object, ...]]:
    """Answer DNS with one public address so no live lookup ever runs."""
    assert host in {"sebi.gov.in", "www.sebi.gov.in"}
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", port))]


def _rhp_filing() -> SebiFiling:
    """Build the one official-looking RHP row the faked SEBI listing returns."""
    return SebiFiling(
        category=SebiFilingCategory.RHP,
        title="Example Limited - Red Herring Prospectus",
        filing_date=dt.date(2026, 6, 30),
        document_url=DETAIL_URL,
        source_url=LISTING_URL,
    )


def _manual_payload(*, source_document_id: int) -> IpoManualExtractionData:
    """Build one complete admin-entered revision for the downloaded RHP.

    Beginner note:
    The numbers are chosen so the ratio expectations below are hand-checkable:
    FY2026 PAT of 10 crore over 50 lakh shares gives EPS 20 INR, net worth of
    80 crore gives ROE 12.5%, and the 242 INR upper band gives P/E 12.1.
    """
    periods = tuple(
        IpoManualPeriodData(
            period_end=dt.date(year, 3, 31),
            revenue=Decimal(str(100 + year - 2024)),
            revenue_page=100,
            ebitda=Decimal("20"),
            ebitda_page=101,
            pat=Decimal("10"),
            pat_page=102,
            profit_before_tax=Decimal("12"),
            profit_before_tax_page=103,
            finance_cost=Decimal("2"),
            finance_cost_page=104,
        )
        for year in (2024, 2025, 2026)
    )
    return IpoManualExtractionData(
        source_document_id=source_document_id,
        financial_amount_unit=IpoAmountUnit.CRORE_INR,
        issue_amount_unit=IpoAmountUnit.CRORE_INR,
        equity_share_unit=IpoShareUnit.LAKH_SHARES,
        periods=periods,
        net_worth=Decimal("80"),
        net_worth_page=130,
        total_debt=Decimal("12"),
        total_debt_page=131,
        cash=Decimal("5"),
        cash_page=132,
        cash_flow_from_operations=Decimal("14"),
        cash_flow_from_operations_page=133,
        equity_shares=Decimal("50"),
        equity_shares_page=134,
        eps=Decimal("20"),
        eps_page=135,
        nav_book_value=Decimal("160"),
        nav_book_value_page=136,
        objects_of_issue="Build a plant and repay borrowings.",
        objects_of_issue_page=137,
        fresh_issue_amount=Decimal("300"),
        fresh_issue_amount_page=138,
        ofs_amount=Decimal("0"),
        ofs_amount_page=139,
        promoter_holding_pre_issue=Decimal("75.25"),
        promoter_holding_pre_issue_page=140,
        promoter_holding_post_issue=Decimal("56.44"),
        promoter_holding_post_issue_page=141,
        total_assets=Decimal("150"),
        total_assets_page=142,
        current_liabilities=Decimal("45"),
        current_liabilities_page=143,
        post_issue_equity_shares=Decimal("60"),
        post_issue_equity_shares_page=144,
        peers=(
            IpoPeerValuationData(
                company_name="Peer One Ltd",
                source_page=210,
                metrics={
                    IpoPeerMetric.EPS: Decimal("8.25"),
                    IpoPeerMetric.PE: Decimal("21.40"),
                },
            ),
        ),
    )


def _score_input(*, source_document_url: str) -> IpoScoreInput:
    """Assess the seven factors strongly enough to earn a Recommended verdict."""

    def factor(score: int, reason: str) -> FactorAssessment:
        """Wrap one factor score with a short evidence-style reason."""
        return FactorAssessment(score=Decimal(score), reason=reason)

    return IpoScoreInput(
        company_name="Example Limited",
        business_quality=factor(90, "Strong business quality"),
        financial_growth=factor(80, "Consistent growth"),
        return_ratios=factor(75, "Healthy return ratios"),
        valuation=factor(70, "Reasonable vs peers"),
        qib_subscription=factor(85, "Strong QIB demand"),
        promoter_quality=factor(90, "Experienced promoters"),
        gmp_sentiment=factor(60, "Measured sentiment"),
        source_documents=(source_document_url,),
    )


@pytest.fixture(params=["session_factory", "file_session_factory"])
def pipeline_session_factory(request):
    """Run the scenario on both conftest engines.

    Beginner note:
    The in-memory engine is the fast default everywhere else; the file-backed
    engine adds the production-like SQLite pragmas. An end-to-end test is
    exactly where an engine-specific difference (e.g. timezone round-trips)
    would surface, so the whole walk runs on both.
    """
    return request.getfixturevalue(request.param)


def test_ipo_pipeline_ingest_download_extract_score_verdict(
    pipeline_session_factory, tmp_path
) -> None:
    """Walk one RHP from SEBI listing to Recommended verdict on real seams."""
    # ------------------------------------------------------------------
    # Stage 1 — ingestion: the real job loop + real ingest_filings, with
    # only the SEBI HTTP fetch replaced by a programmed listing.
    # ------------------------------------------------------------------
    def fetcher(category, _from_date, _to_date):
        """Return one RHP row for the RHP category and nothing for siblings."""
        if category is SebiFilingCategory.RHP:
            return (_rhp_filing(),)
        return ()

    outcome = run_scan_ipo_filings(
        today=dt.date(2026, 7, 1),
        ensure_schema=lambda: True,
        latest_filing_date=lambda **_kwargs: None,
        fetcher=fetcher,
        ingestion=ingest_filings,
        session_factory=pipeline_session_factory,
        output=io.StringIO(),
    )
    assert outcome.exit_code == 0

    issues = list_issues(session_factory=pipeline_session_factory)
    assert len(issues) == 1
    issue = issues[0]
    # The company identity is DERIVED (title normalization), not copied.
    assert issue.company_name == "Example Limited"
    assert issue.status is IpoStatus.RHP_FILED
    assert issue.sebi_company_key == "example limited"
    assert issue.source_confidence is Confidence.HIGH

    documents = list_documents(issue.id, session_factory=pipeline_session_factory)
    assert len(documents) == 1
    document = documents[0]
    assert document.document_type == "rhp"
    assert document.document_url == DETAIL_URL
    assert document.parse_status is IpoDocumentParseStatus.NOT_DOWNLOADED

    # ------------------------------------------------------------------
    # Stage 2 — download: the real two-transaction repository orchestration
    # around the real downloader, with HTTP and DNS doubled.
    # ------------------------------------------------------------------
    detail_html = (
        f'<iframe src="../../../web/?file={PDF_URL}"></iframe>'
        '<a href="/abridged.pdf">Abridged Prospectus</a>'
    ).encode()
    session = _FakeSession(
        [
            _FakeResponse(detail_html, content_type="text/html; charset=UTF-8"),
            _FakeResponse(PDF_BYTES, content_type="application/pdf"),
        ]
    )

    def downloader(record, *, data_dir):
        """Bind the real file downloader to the faked HTTP/DNS externals."""
        return download_document_file(
            record,
            data_dir=data_dir,
            session=session,
            resolver=_public_resolver,
            sleeper=lambda _delay: None,
            now=lambda: dt.datetime(2026, 7, 1, 10, tzinfo=dt.UTC),
        )

    audits: list[dict[str, object]] = []

    def audit_recorder(**kwargs) -> bool:
        """Capture lifecycle audit calls without touching the audit table."""
        audits.append(kwargs)
        return True

    result = download_document(
        issue.id,
        document.id,
        data_dir=tmp_path,
        downloader=downloader,
        audit_recorder=audit_recorder,
        session_factory=pipeline_session_factory,
    )

    digest = hashlib.sha256(PDF_BYTES).hexdigest()
    assert result.content_sha256 == digest
    assert session.calls == [DETAIL_URL, PDF_URL]
    assert (tmp_path / result.file_path).read_bytes() == PDF_BYTES

    cached = list_documents(issue.id, session_factory=pipeline_session_factory)[0]
    assert cached.content_sha256 == digest
    assert cached.file_path == f"ipo/documents/{digest}.pdf"
    assert cached.parse_status is IpoDocumentParseStatus.PENDING

    # ------------------------------------------------------------------
    # Stage 3 — manual extraction: verified against the stage-2 cache.
    # ------------------------------------------------------------------
    revision = submit_manual_extraction(
        issue.id,
        _manual_payload(source_document_id=document.id),
        entered_by_email=" Analyst@Example.com ",
        data_dir=tmp_path,
        now=lambda: dt.datetime(2026, 7, 1, 11, tzinfo=dt.UTC),
        audit_recorder=audit_recorder,
        session_factory=pipeline_session_factory,
    )
    # Provenance continuity: the revision is pinned to the bytes stage 2 wrote.
    assert revision.source_content_sha256 == digest
    assert revision.entered_by_email == "analyst@example.com"
    assert len(revision.periods) == 3
    assert [event["event"] for event in audits] == ["ipo_manual_extraction_submitted"]

    # ------------------------------------------------------------------
    # Stage 4 — ratios: an admin records the price band (ingestion cannot
    # know it), then the pure engine runs on the latest revision snapshot.
    # ------------------------------------------------------------------
    update_issue(
        issue.id,
        IpoIssueData(
            company_name=issue.company_name,
            issue_type=issue.issue_type,
            status=issue.status,
            source_confidence=issue.source_confidence,
            source_url=issue.source_url,
            sebi_company_key=issue.sebi_company_key,
            price_band_low=Decimal("230"),
            price_band_high=Decimal("242"),
        ),
        session_factory=pipeline_session_factory,
    )

    analysis = get_latest_ipo_ratios(issue.id, session_factory=pipeline_session_factory)
    assert analysis is not None
    assert analysis.extraction_id == revision.id
    assert analysis.source_content_sha256 == digest  # provenance chain again
    assert analysis.price_band_high == Decimal("242")
    assert len(analysis.ratios) == len(IpoRatioName)
    # Hand-checkable spot values (see _manual_payload's beginner note).
    eps = analysis.ratios[IpoRatioName.EPS]
    assert eps.status is IpoRatioStatus.COMPUTED and eps.value == Decimal("20")
    roe = analysis.ratios[IpoRatioName.ROE]
    assert roe.status is IpoRatioStatus.COMPUTED and roe.value == Decimal("12.5")
    pe = analysis.ratios[IpoRatioName.PRICE_TO_EARNINGS]
    assert pe.status is IpoRatioStatus.COMPUTED and pe.value == Decimal("12.1")
    # The reported EPS was deliberately entered to match the computed one.
    assert analysis.eps_reconciliation.materially_different is False

    # ------------------------------------------------------------------
    # Stage 5 — score + verdict: the evaluation only accepts source
    # documents that stage 1 actually registered for this issue.
    # ------------------------------------------------------------------
    evaluation = evaluate_issue(
        issue.id,
        _score_input(source_document_url=DETAIL_URL),
        session_factory=pipeline_session_factory,
    )
    assert evaluation.result.recommendation.value == "Recommended"
    assert evaluation.result.source_documents == (DETAIL_URL,)

    latest = get_latest_recommendation(issue.id, session_factory=pipeline_session_factory)
    assert latest == evaluation.result
