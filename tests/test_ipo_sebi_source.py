"""IPO-002 SEBI source parsing and hardened HTTP tests."""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator

import pytest
import requests

from backend.ipo.models import IpoIssueType, IpoStatus, SebiFilingCategory
from backend.ipo.sources.sebi import (
    AJAX_URL,
    MAX_PAGES,
    SebiParseError,
    SebiSourceError,
    build_filing_data,
    fetch_sebi_filings,
    normalize_company_identity,
    parse_listing_page,
)


def _page(*rows: str, total_pages: int = 1, next_value: int = 1) -> str:
    """Build the reusable page fixture used by the scenarios below."""
    return (
        "<table>"
        + "".join(rows)
        + "</table>#@#"
        + f'<input id="totalPage" value="{total_pages}">'
        + f'<input id="nextValue" value="{next_value}">'
    )


def _row(date: str, title: str, detail: str = "/filings/example.html") -> str:
    """Build the reusable row fixture used by the scenarios below."""
    return (
        f"<tr><td>{date}</td><td>"
        f'<a href="{detail}">{title}<br>'
        '<a href="/pdf/abridged.pdf">Abridged Prospectus</a>'
        "</a></td></tr>"
    )


class FakeResponse:
    """Build the reusable FakeResponse fixture used by the scenarios below."""

    def __init__(
        self,
        body: str = "",
        *,
        status_code: int = 200,
        url: str = AJAX_URL,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Initialize the deterministic FakeResponse test double without live I/O."""
        self.body = body.encode()
        self.status_code = status_code
        self.url = url
        self.headers = headers or {"Content-Type": "text/html; charset=UTF-8"}
        self.closed = False

    def iter_content(self, chunk_size: int) -> Iterator[bytes]:
        """Yield the prepared body once, matching requests' streaming interface."""
        del chunk_size
        yield self.body

    def close(self) -> None:
        """Record response closure so resource-lifetime assertions stay explicit."""
        self.closed = True


class FakeSession:
    """Build the reusable FakeSession fixture used by the scenarios below."""

    def __init__(self, outcomes: list[FakeResponse | Exception]) -> None:
        """Initialize the deterministic FakeSession test double without live I/O."""
        self.outcomes = outcomes
        self.calls: list[tuple[str, str, dict[str, object]]] = []

    def request(self, method: str, url: str, **kwargs: object) -> FakeResponse:
        """Return FIFO outcomes while recording the exact hardened request."""
        self.calls.append((method, url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.mark.parametrize(
    ("title", "expected_name", "expected_key", "expected_type"),
    [
        (
            "Example Ltd. - Draft Red Herring Prospectus",
            "Example Ltd",
            "example limited",
            IpoIssueType.UNKNOWN,
        ),
        (
            "EXAMPLE LIMITED (SME) - RHP",
            "EXAMPLE LIMITED",
            "example limited",
            IpoIssueType.SME,
        ),
        (
            "Corrigendum to DRHP of A & B Private Limited",
            "A & B Private Limited",
            "a and b private limited",
            IpoIssueType.UNKNOWN,
        ),
        (
            "Co-Op Industries Ltd - DRHP",
            "Co Op Industries Ltd",
            "co op industries limited",
            IpoIssueType.UNKNOWN,
        ),
        (
            "Example Limited - Addendum to DRHP",
            "Example Limited",
            "example limited",
            IpoIssueType.UNKNOWN,
        ),
    ],
)
def test_company_identity_normalizes_markers_suffixes_and_explicit_sme(
    title: str,
    expected_name: str,
    expected_key: str,
    expected_type: IpoIssueType,
) -> None:
    """Pin company identity normalizes markers suffixes and explicit sme as an executable IPO regression contract."""
    assert normalize_company_identity(title) == (
        expected_name,
        expected_key,
        expected_type,
    )


@pytest.mark.parametrize("category", list(SebiFilingCategory))
def test_parse_listing_page_uses_outer_detail_anchor_for_every_category(
    category: SebiFilingCategory,
) -> None:
    """Pin parse listing page uses outer detail anchor for every category as an executable IPO regression contract."""
    parsed = parse_listing_page(
        _page(_row("Jun 26, 2026", "Example Limited - Prospectus")),
        category=category,
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?doListing=yes",
    )

    assert len(parsed.filings) == 1
    assert parsed.filings[0].filing_date == dt.date(2026, 6, 26)
    assert parsed.filings[0].document_url == "https://www.sebi.gov.in/filings/example.html"
    assert "abridged" not in parsed.filings[0].document_url
    assert parsed.total_pages == 1


def test_build_filing_data_maps_category_and_produces_stable_fingerprint() -> None:
    """Pin build filing data maps category and produces stable fingerprint as an executable IPO regression contract."""
    parsed = parse_listing_page(
        _page(_row("Jun 26, 2026", "Example Ltd - RHP")),
        category=SebiFilingCategory.RHP,
        source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?smid=11",
    )

    first = build_filing_data(parsed.filings[0])
    second = build_filing_data(parsed.filings[0])

    assert first.status is IpoStatus.RHP_FILED
    assert first.document_type == "rhp"
    assert first.record_hash == second.record_hash
    assert len(first.record_hash) == 64


@pytest.mark.parametrize(
    "body",
    [
        _page("<tr><td>not-a-date</td><td>Broken row</td></tr>"),
        _page(_row("Jun 26, 2026", "Example", "https://evil.example/filing")),
    ],
)
def test_nonempty_malformed_pages_fail_closed(body: str) -> None:
    """Pin nonempty malformed pages fail closed as an executable IPO regression contract."""
    with pytest.raises(SebiParseError):
        parse_listing_page(
            body,
            category=SebiFilingCategory.DRHP,
            source_url="https://www.sebi.gov.in/sebiweb/home/HomeAction.do?smid=10",
        )


def test_fetch_paginates_filters_dates_and_uses_expected_ajax_payload() -> None:
    """Pin fetch paginates filters dates and uses expected ajax payload as an executable IPO regression contract."""
    first = FakeResponse(
        _page(
            _row("Jun 30, 2026", "Newest Limited - DRHP", "/filings/newest.html"),
            total_pages=2,
            next_value=2,
        )
    )
    second = FakeResponse(
        _page(_row("Jun 20, 2026", "Old Limited - DRHP", "/filings/old.html"))
    )
    session = FakeSession([first, second])
    sleeps: list[float] = []

    filings = fetch_sebi_filings(
        SebiFilingCategory.DRHP,
        dt.date(2026, 6, 25),
        dt.date(2026, 6, 30),
        session=session,
        sleeper=sleeps.append,
    )

    assert [filing.title for filing in filings] == ["Newest Limited - DRHP"]
    assert len(session.calls) == 2
    assert session.calls[0][0] == "POST"
    assert session.calls[0][1] == AJAX_URL
    first_payload = session.calls[0][2]["data"]
    assert isinstance(first_payload, dict)
    assert first_payload["smid"] == "10"
    assert first_payload["fromDate"] == "25-06-2026"
    assert first_payload["toDate"] == "30-06-2026"
    assert sleeps == [0.5]
    assert first.closed and second.closed


def test_fetch_retries_timeout_and_429_then_closes_every_response() -> None:
    """Pin fetch retries timeout and 429 then closes every response as an executable IPO regression contract."""
    throttled = FakeResponse(status_code=429)
    session = FakeSession(
        [
            requests.Timeout("secret response body"),
            throttled,
            FakeResponse(_page()),
        ]
    )
    sleeps: list[float] = []

    assert fetch_sebi_filings(
        SebiFilingCategory.RHP,
        dt.date(2026, 6, 1),
        dt.date(2026, 6, 30),
        session=session,
        sleeper=sleeps.append,
    ) == ()

    assert sleeps == [2.0, 5.0]
    assert throttled.closed
    assert all(call[2]["timeout"] == (5.0, 20.0) for call in session.calls)


def test_fetch_rejects_cross_host_redirect_and_closes_response() -> None:
    """Pin fetch rejects cross host redirect and closes response as an executable IPO regression contract."""
    redirect = FakeResponse(
        status_code=302,
        headers={"Location": "https://evil.example/steal"},
    )

    with pytest.raises(SebiSourceError, match="redirect"):
        fetch_sebi_filings(
            SebiFilingCategory.FINAL_OFFER,
            dt.date(2026, 6, 1),
            dt.date(2026, 6, 30),
            session=FakeSession([redirect]),
            sleeper=lambda _seconds: None,
        )

    assert redirect.closed


def test_fetch_rejects_non_html_oversized_and_excessive_pagination() -> None:
    """Pin fetch rejects non html oversized and excessive pagination as an executable IPO regression contract."""
    non_html = FakeResponse(headers={"Content-Type": "application/pdf"})
    with pytest.raises(SebiSourceError, match="content type"):
        fetch_sebi_filings(
            SebiFilingCategory.DRHP,
            None,
            dt.date(2026, 6, 30),
            session=FakeSession([non_html]),
            sleeper=lambda _seconds: None,
        )
    assert non_html.closed

    oversized = FakeResponse("x" * (2 * 1024 * 1024 + 1))
    with pytest.raises(SebiSourceError, match="2 MiB"):
        fetch_sebi_filings(
            SebiFilingCategory.DRHP,
            None,
            dt.date(2026, 6, 30),
            session=FakeSession([oversized]),
            sleeper=lambda _seconds: None,
        )
    assert oversized.closed

    too_many = FakeResponse(_page(total_pages=MAX_PAGES + 1))
    with pytest.raises(SebiSourceError, match="page cap"):
        fetch_sebi_filings(
            SebiFilingCategory.DRHP,
            None,
            dt.date(2026, 6, 30),
            session=FakeSession([too_many]),
            sleeper=lambda _seconds: None,
        )
    assert too_many.closed
