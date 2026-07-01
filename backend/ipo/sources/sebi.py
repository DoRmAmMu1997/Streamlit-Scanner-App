"""Hardened client and parser for official SEBI public-issue listings.

Beginner note:
This module stops at the filing-detail page. It inventories metadata exposed by
SEBI but deliberately never downloads or parses the linked prospectus PDFs.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
import time
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup

from backend.ipo.models import (
    IpoFilingData,
    IpoIssueType,
    IpoStatus,
    IpoValidationError,
    SebiFiling,
    SebiFilingCategory,
)

AJAX_URL = "https://www.sebi.gov.in/sebiweb/ajax/home/getnewslistinfo.jsp"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_PAGES = 200
CONNECT_TIMEOUT_SECONDS = 5.0
READ_TIMEOUT_SECONDS = 20.0
POLITE_DELAY_SECONDS = 0.5
RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)
MAX_REDIRECTS = 3
ALLOWED_HOSTS = frozenset({"sebi.gov.in", "www.sebi.gov.in"})

_CATEGORY_SETTINGS: dict[SebiFilingCategory, tuple[str, str]] = {
    SebiFilingCategory.DRHP: ("10", "15"),
    SebiFilingCategory.RHP: ("11", "15"),
    SebiFilingCategory.FINAL_OFFER: ("12", "15"),
}


class SebiSourceError(RuntimeError):
    """Raised when the bounded SEBI fetch cannot produce a trusted response."""


class SebiParseError(SebiSourceError):
    """Raised when a non-empty listing page would lose filing records."""


@dataclass(frozen=True)
class ParsedListingPage:
    """Parsed rows plus pagination metadata from one SEBI AJAX response."""

    filings: tuple[SebiFiling, ...]
    total_pages: int
    next_value: int


def category_listing_url(category: SebiFilingCategory) -> str:
    """Return the fixed official listing URL for one filing category."""
    category = SebiFilingCategory(category)
    smid, ssid = _CATEGORY_SETTINGS[category]
    return (
        "https://www.sebi.gov.in/sebiweb/home/HomeAction.do?"
        f"doListing=yes&sid=3&smid={smid}&ssid={ssid}"
    )


def _canonical_sebi_url(value: str, *, base_url: str | None = None) -> str:
    """Provide the canonical sebi url step used by the IPO workflow."""
    candidate = urljoin(base_url or AJAX_URL, value.strip())
    parsed = urlsplit(candidate)
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme.casefold() != "https"
        or host not in ALLOWED_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise SebiSourceError("SEBI URL or redirect did not match the HTTPS host allowlist.")
    return urlunsplit(("https", host, parsed.path or "/", parsed.query, ""))


_SME_TOKEN = re.compile(r"(?:^|[\s(\[/{_-])SME(?:$|[\s)\]/}_-])", re.IGNORECASE)
_FILING_MARKERS = re.compile(
    r"\b(?:draft\s+red\s+herring\s+prospectus|red\s+herring\s+prospectus|"
    r"final\s+offer\s+document|offer\s+document|prospectus|addendum|corrigendum|drhp|rhp)\b",
    re.IGNORECASE,
)


def normalize_company_identity(title: str) -> tuple[str, str, IpoIssueType]:
    """Derive a conservative display name, stable SEBI key, and issue segment."""
    normalized = unicodedata.normalize("NFKC", str(title)).strip()
    issue_type = IpoIssueType.SME if _SME_TOKEN.search(normalized) else IpoIssueType.UNKNOWN
    display = _SME_TOKEN.sub(" ", normalized)
    display = _FILING_MARKERS.sub(" ", display)
    display = re.sub(r"\s*[-–—:|]+\s*", " ", display)
    display = re.sub(r"^(?:(?:to|of|for)\s+)+", "", display, flags=re.IGNORECASE)
    display = re.sub(r"\s+", " ", display).strip(" .,:;_-()[]{}")
    display = re.sub(r"^(?:(?:to|of|for)\s+)+", "", display, flags=re.IGNORECASE)
    display = re.sub(r"\s+(?:(?:to|of|for)\s*)+$", "", display, flags=re.IGNORECASE)
    if not display:
        raise IpoValidationError("SEBI filing title did not contain a company name.")

    key_text = unicodedata.normalize("NFKC", display).casefold().replace("&", " and ")
    tokens = re.sub(r"[^\w]+", " ", key_text, flags=re.UNICODE).split()
    # Corporate abbreviations are normalized only in suffix position. A global
    # replacement would corrupt legitimate names such as "Co-Op Industries".
    if tokens:
        final_suffixes = {
            "co": "company",
            "corp": "corporation",
            "ltd": "limited",
            "pvt": "private",
        }
        tokens[-1] = final_suffixes.get(tokens[-1], tokens[-1])
        if len(tokens) >= 2 and tokens[-2] == "pvt" and tokens[-1] == "limited":
            tokens[-2] = "private"
    key = " ".join(tokens)
    if not key:
        raise IpoValidationError("SEBI filing title did not produce a company key.")
    return display, key, issue_type


def _extract_title(anchor: Any) -> str:
    # Parse a copy so removing nested PDF anchors cannot mutate the caller's tree.
    """Provide the extract title step used by the IPO workflow."""
    copy = BeautifulSoup(str(anchor), "html.parser").find("a")
    if copy is None:
        return ""
    for nested in copy.find_all("a"):
        nested.decompose()
    return copy.get_text(" ", strip=True)


def _pagination_value(soup: BeautifulSoup, name: str, default: int) -> int:
    """Provide the pagination value step used by the IPO workflow."""
    element = soup.find(id=lambda value: isinstance(value, str) and value.casefold() == name.casefold())
    raw = element.get("value") if element is not None else None
    if raw is None:
        pattern = re.compile(rf"{re.escape(name)}[^0-9]{{0,40}}([0-9]+)", re.IGNORECASE)
        match = pattern.search(str(soup))
        raw = match.group(1) if match else None
    try:
        parsed = int(str(raw)) if raw is not None else default
    except ValueError as exc:
        raise SebiParseError(f"Invalid SEBI {name} pagination value.") from exc
    if parsed < 1:
        raise SebiParseError(f"Invalid SEBI {name} pagination value.")
    return parsed


def parse_listing_page(
    body: str,
    *,
    category: SebiFilingCategory,
    source_url: str,
) -> ParsedListingPage:
    """Parse one AJAX page and fail closed if any filing-like row is malformed."""
    category = SebiFilingCategory(category)
    source_url = _canonical_sebi_url(source_url)
    html, _separator, metadata = body.partition("#@#")
    soup = BeautifulSoup(html, "html.parser")
    pagination_soup = BeautifulSoup(metadata or html, "html.parser")
    filings: list[SebiFiling] = []
    malformed_rows = 0

    for row in soup.find_all("tr"):
        cells = row.find_all("td", recursive=False)
        if len(cells) < 2:
            continue
        date_text = cells[0].get_text(" ", strip=True)
        anchors = [
            anchor
            for anchor in cells[1].find_all("a")
            if anchor.get("href")
            and not str(anchor.get("href")).casefold().split("?", 1)[0].endswith(".pdf")
        ]
        if not date_text and not anchors:
            continue
        try:
            filing_date = dt.datetime.strptime(date_text, "%b %d, %Y").date()
            if not anchors:
                raise ValueError("missing filing detail anchor")
            title = _extract_title(anchors[0])
            if not title:
                raise ValueError("missing filing title")
            document_url = _canonical_sebi_url(str(anchors[0]["href"]), base_url=source_url)
            filings.append(
                SebiFiling(
                    category=category,
                    title=title,
                    filing_date=filing_date,
                    document_url=document_url,
                    source_url=source_url,
                )
            )
        except (KeyError, TypeError, ValueError, IpoValidationError, SebiSourceError):
            malformed_rows += 1

    if malformed_rows:
        raise SebiParseError(
            f"SEBI listing page contained {malformed_rows} malformed filing row(s)."
        )

    return ParsedListingPage(
        filings=tuple(filings),
        total_pages=_pagination_value(pagination_soup, "totalPage", 1),
        next_value=_pagination_value(pagination_soup, "nextValue", 1),
    )


def build_filing_data(filing: SebiFiling) -> IpoFilingData:
    """Convert a parsed SEBI row into its canonical persistence contract."""
    company_name, company_key, issue_type = normalize_company_identity(filing.title)
    status_by_category = {
        SebiFilingCategory.DRHP: IpoStatus.DRHP_FILED,
        SebiFilingCategory.RHP: IpoStatus.RHP_FILED,
        SebiFilingCategory.FINAL_OFFER: IpoStatus.CLOSED,
    }
    document_url = _canonical_sebi_url(filing.document_url)
    canonical = {
        "company_key": company_key,
        "document_type": filing.category.value,
        "document_url": document_url,
        "filing_date": filing.filing_date.isoformat(),
    }
    fingerprint = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return IpoFilingData(
        company_name=company_name,
        sebi_company_key=company_key,
        issue_type=issue_type,
        status=status_by_category[filing.category],
        document_type=filing.category.value,
        filing_date=filing.filing_date,
        document_url=document_url,
        source_url=filing.source_url,
        record_hash=fingerprint,
    )


def _read_bounded_html(response: Any) -> str:
    """Provide the read bounded html step used by the IPO workflow."""
    content_type = str(response.headers.get("Content-Type", "")).casefold()
    if "text/html" not in content_type:
        raise SebiSourceError("SEBI response had an unexpected content type.")
    content_length = response.headers.get("Content-Length")
    if content_length is not None:
        try:
            if int(content_length) > MAX_RESPONSE_BYTES:
                raise SebiSourceError("SEBI response exceeded the 2 MiB limit.")
        except ValueError as exc:
            raise SebiSourceError("SEBI response had an invalid Content-Length.") from exc
    chunks: list[bytes] = []
    size = 0
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if not chunk:
            continue
        size += len(chunk)
        if size > MAX_RESPONSE_BYTES:
            raise SebiSourceError("SEBI response exceeded the 2 MiB limit.")
        chunks.append(chunk)
    return b"".join(chunks).decode("utf-8", errors="replace")


def _request_following_redirects(
    session: Any,
    *,
    method: str,
    url: str,
    data: dict[str, str] | None,
) -> Any:
    """Provide the request following redirects step used by the IPO workflow."""
    current_method = method
    current_url = _canonical_sebi_url(url)
    current_data = data
    for redirect_count in range(MAX_REDIRECTS + 1):
        response = session.request(
            current_method,
            current_url,
            data=current_data,
            timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
            allow_redirects=False,
            stream=True,
            headers={"User-Agent": "Streamlit-Scanner-App/IPO-002"},
        )
        if response.status_code not in {301, 302, 303, 307, 308}:
            return response
        try:
            location = response.headers.get("Location")
            if not location:
                raise SebiSourceError("SEBI redirect omitted its destination.")
            if redirect_count >= MAX_REDIRECTS:
                raise SebiSourceError("SEBI redirect limit was exceeded.")
            current_url = _canonical_sebi_url(str(location), base_url=current_url)
        finally:
            response.close()
        if response.status_code in {301, 302, 303}:
            current_method = "GET"
            current_data = None
    raise SebiSourceError("SEBI redirect limit was exceeded.")


def _fetch_page(
    session: Any,
    payload: dict[str, str],
    sleeper: Callable[[float], None],
) -> str:
    """Provide the fetch page step used by the IPO workflow."""
    for attempt in range(len(RETRY_DELAYS_SECONDS) + 1):
        response = None
        try:
            response = _request_following_redirects(
                session,
                method="POST",
                url=AJAX_URL,
                data=payload,
            )
            if response.status_code == 429 or 500 <= response.status_code <= 599:
                if attempt == len(RETRY_DELAYS_SECONDS):
                    raise SebiSourceError("SEBI remained unavailable after bounded retries.")
                response.close()
                response = None
                sleeper(RETRY_DELAYS_SECONDS[attempt])
                continue
            if response.status_code != 200:
                raise SebiSourceError(f"SEBI returned HTTP {response.status_code}.")
            return _read_bounded_html(response)
        except requests.RequestException as exc:
            if attempt == len(RETRY_DELAYS_SECONDS):
                raise SebiSourceError("SEBI request failed after bounded retries.") from exc
            sleeper(RETRY_DELAYS_SECONDS[attempt])
        finally:
            if response is not None:
                response.close()
    raise SebiSourceError("SEBI request failed after bounded retries.")


def fetch_sebi_filings(
    category: SebiFilingCategory,
    from_date: dt.date | None,
    to_date: dt.date,
    *,
    session: Any | None = None,
    sleeper: Callable[[float], None] = time.sleep,
) -> tuple[SebiFiling, ...]:
    """Fetch an inclusive date window from one fixed official SEBI category."""
    category = SebiFilingCategory(category)
    if not isinstance(to_date, dt.date) or (
        from_date is not None and (not isinstance(from_date, dt.date) or from_date > to_date)
    ):
        raise IpoValidationError("from_date and to_date must form a valid inclusive window.")

    owned_session = session is None
    active_session = requests.Session() if session is None else session
    smid, ssid = _CATEGORY_SETTINGS[category]
    source_url = category_listing_url(category)
    filings: list[SebiFiling] = []
    page_number = 1
    next_value = 1
    try:
        while True:
            payload = {
                "sid": "3",
                "smid": smid,
                "ssid": ssid,
                "fromDate": from_date.strftime("%d-%m-%Y") if from_date else "",
                "toDate": to_date.strftime("%d-%m-%Y"),
                "nextValue": str(next_value),
                "next": "n",
                "doDirect": "0" if page_number == 1 else "1",
            }
            parsed = parse_listing_page(
                _fetch_page(active_session, payload, sleeper),
                category=category,
                source_url=source_url,
            )
            if parsed.total_pages > MAX_PAGES:
                raise SebiSourceError(f"SEBI pagination exceeded the {MAX_PAGES}-page cap.")
            filings.extend(
                filing
                for filing in parsed.filings
                if (from_date is None or filing.filing_date >= from_date)
                and filing.filing_date <= to_date
            )
            if page_number >= parsed.total_pages:
                break
            if from_date is not None and parsed.filings and min(
                filing.filing_date for filing in parsed.filings
            ) < from_date:
                break
            page_number += 1
            if page_number > MAX_PAGES:
                raise SebiSourceError(f"SEBI pagination exceeded the {MAX_PAGES}-page cap.")
            next_value = parsed.next_value
            sleeper(POLITE_DELAY_SECONDS)
    finally:
        if owned_session:
            active_session.close()
    return tuple(filings)
