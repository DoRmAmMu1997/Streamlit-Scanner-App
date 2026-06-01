from __future__ import annotations

"""Polite, label-driven scraper for one screener.in company page.

Why this module exists:
The Check Fundamentals agent needs structured numbers (debt, ROCE, sales
history, peer list, etc.) plus a free-form text dump so it can also reason
about things the user did not pre-define. Screener.in renders these on a
plain HTML page (no JS required), so a `requests` + `BeautifulSoup` parse
is sufficient — Selenium would be overkill.

Design choices:
- Parsing finds data by **label text** (e.g. "Return on capital employed")
  rather than div index. Screener.in occasionally re-orders sections; using
  visible labels keeps the parser stable across those layout shifts.
- Every field extraction is wrapped in `try / except` and on failure returns
  `None` with a `logger.warning`. The agent can reason about partial data;
  a parser crash would break the whole feature.
- The fetcher is a single public function (`fetch_company_data`) so the
  Check Fundamentals agent can wrap it as a tool without touching the parser
  internals.
"""

import json
import logging
import os
import re
import statistics
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup


logger = logging.getLogger(__name__)


# Screener.in serves the cleaner consolidated view at /consolidated/.
# Some companies (mostly single-entity ones) only have the standalone page.
_SCREENER_URL_CONSOLIDATED = "https://www.screener.in/company/{symbol}/consolidated/"
_SCREENER_URL_STANDALONE = "https://www.screener.in/company/{symbol}/"

# Polite identifier. The contact email is intentionally generic; users can
# override via the User-Agent below if they need their own attribution.
_DEFAULT_USER_AGENT = (
    "hemant-scanner/1.0 (+personal use; "
    "https://github.com/DoRmAmMu1997/Streamlit-Scanner-App)"
)

_REQUEST_TIMEOUT_SECONDS = 20
_RETRY_DELAYS_SECONDS = (2.0, 5.0, 10.0)


class ScreenerInFetchError(RuntimeError):
    """Raised when the screener.in page cannot be fetched after retries."""


# ---------------------------------------------------------------------------
# HTTP layer
# ---------------------------------------------------------------------------


def _request_delay_seconds() -> float:
    """How long to pause between successful fetches. Defaults to 1.5 s."""
    raw = (os.getenv("SCANNER_SCREENER_IN_DELAY_SECONDS") or "").strip()
    if not raw:
        return 1.5
    try:
        value = float(raw)
        return value if value >= 0 else 1.5
    except (TypeError, ValueError):
        return 1.5


def _build_headers() -> dict[str, str]:
    """Return HTTP headers used for every screener.in request."""
    return {
        "User-Agent": _DEFAULT_USER_AGENT,
        # Screener.in serves slightly cleaner HTML when the Accept header is
        # specified — without it the server sometimes returns gzip-only.
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-IN,en;q=0.9",
    }


def _fetch_html(url: str, session: requests.Session) -> str | None:
    """Fetch one screener.in URL with backoff on HTTP 429.

    Returns the raw HTML text on success, or `None` if the page does not
    exist (404). Raises `ScreenerInFetchError` on terminal failure.
    """
    for attempt, delay in enumerate((0.0, *_RETRY_DELAYS_SECONDS)):
        if delay:
            # Sleep BEFORE the retry, not after the previous attempt, so the
            # first request has no delay and a 429 triggers a 2-second wait.
            logger.warning("screener.in rate-limited; sleeping %.1fs before retry", delay)
            time.sleep(delay)
        try:
            response = session.get(url, headers=_build_headers(), timeout=_REQUEST_TIMEOUT_SECONDS)
        except requests.RequestException as exc:
            if attempt == len(_RETRY_DELAYS_SECONDS):
                raise ScreenerInFetchError(f"Network error fetching {url}: {exc}") from exc
            continue

        if response.status_code == 404:
            # 404 is a normal outcome — caller will retry with the
            # standalone URL.
            return None
        if response.status_code == 429:
            # Retry per backoff schedule
            continue
        if 500 <= response.status_code < 600:
            # Treat 5xx as retryable.
            continue
        if response.status_code != 200:
            raise ScreenerInFetchError(
                f"screener.in returned HTTP {response.status_code} for {url}"
            )
        return response.text

    raise ScreenerInFetchError(f"screener.in did not respond successfully for {url}")


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


_PERCENT_PATTERN = re.compile(r"-?\d+(?:\.\d+)?")
_NUMBER_PATTERN = re.compile(r"-?\d[\d,]*(?:\.\d+)?")


def _to_number(text: str | None) -> float | None:
    """Parse a screener.in numeric cell into a float, or None if unparseable.

    Screener.in renders numbers like "1,234.56", "12%", "₹ 1,234 Cr.", etc.
    The regex tolerates currency symbols, commas, and unit suffixes.
    """
    if text is None:
        return None
    cleaned = text.replace(",", "").replace(" ", " ").strip()
    if not cleaned or cleaned in {"-", "--"}:
        return None
    match = _NUMBER_PATTERN.search(cleaned)
    if match is None:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except (TypeError, ValueError):
        return None


def _find_top_ratio(soup: BeautifulSoup, label: str) -> float | None:
    """Look up a single value from the 'company-ratios' card at the top of the page.

    Each row in that card has a <span class="name"> label and a
    <span class="number"> value. Matching on the label text keeps the parser
    resilient to row reordering on screener.in.
    """
    try:
        for li in soup.select("#top-ratios li"):
            name_el = li.find("span", class_="name")
            number_el = li.find("span", class_="number")
            if name_el is None or number_el is None:
                continue
            if label.strip().lower() in name_el.get_text(" ", strip=True).lower():
                return _to_number(number_el.get_text(" ", strip=True))
    except Exception:  # noqa: BLE001 — partial parse must not crash
        logger.warning("Could not parse top-ratio %s", label, exc_info=True)
    return None


# screener.in renders the "Median PE" line on the Price-to-Earning chart in
# JavaScript — it is NOT in the page HTML. The chart's data, however, comes from
# a JSON endpoint (the same `/api/company/<id>/...` family the peers table uses).
# We match the numeric company id from any such URL on the page, then fetch the
# PE time series and compute the median ourselves — exactly what the chart does.
_COMPANY_ID_REGEX = re.compile(r"/api/company/(\d+)/")

# 5 years, to match screener.in's default "5Yr" chart view (the one that shows
# the "Median PE" legend). 1825 days ≈ 5 * 365.
_MEDIAN_PE_WINDOW_DAYS = 1825


def _extract_company_id(soup: BeautifulSoup) -> str | None:
    """Return screener.in's numeric company id from the page, or None.

    The id appears in the `/api/company/<id>/...` URLs the page embeds (e.g.
    the watchlist "add" link and the chart/peers endpoints). We only need the
    number to build the chart-data URL.
    """
    match = _COMPANY_ID_REGEX.search(str(soup))
    return match.group(1) if match else None


def _median_of_pe_series(payload: dict[str, Any]) -> float | None:
    """Compute the median of the PE series in a screener.in chart JSON payload.

    The payload shape is ``{"datasets": [{"metric", "label", "values", ...}]}``
    where ``values`` is a list of ``[date_string, pe_or_null]`` pairs. We take
    the median of the non-null PE numbers — the same calculation screener.in's
    chart shows as "Median PE". Returns None when the series is empty/malformed.
    """
    datasets = payload.get("datasets") if isinstance(payload, dict) else None
    if not datasets:
        return None
    values = datasets[0].get("values") if isinstance(datasets[0], dict) else None
    if not values:
        return None
    numbers: list[float] = []
    for point in values:
        # Each point is [date, value]; value is null on non-trading days.
        if isinstance(point, (list, tuple)) and len(point) >= 2 and point[1] is not None:
            try:
                numbers.append(float(point[1]))
            except (TypeError, ValueError):
                continue
    if not numbers:
        return None
    return round(statistics.median(numbers), 2)


def _fetch_median_pe(
    soup: BeautifulSoup,
    *,
    base_url: str,
    session: requests.Session,
) -> float | None:
    """Fetch the PE time series from screener.in's chart endpoint and return its median.

    The agent uses this as the preferred reference for valuation observations
    (current P/E vs the stock's own historical median). Soft-fails to None on
    any error so the agent falls back to industry_pe — a missing median must
    never break the whole fetch.

    Mirrors `_fetch_peer_table`: find the numeric company id on the page, build
    the JSON URL, fetch it with the shared HTTP helper, and parse defensively.
    """
    company_id = _extract_company_id(soup)
    if not company_id:
        logger.info("No screener.in company id found on page %s", base_url)
        return None

    # The chart endpoint returns the daily P/E series the on-page chart plots.
    chart_url = urljoin(
        base_url,
        f"/api/company/{company_id}/chart/?q=Price+to+Earning&days={_MEDIAN_PE_WINDOW_DAYS}",
    )
    try:
        body = _fetch_html(chart_url, session)
    except ScreenerInFetchError:
        logger.warning("Median-P/E chart fetch failed for %s", chart_url, exc_info=True)
        return None
    if not body:
        return None

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        logger.warning("Median-P/E chart returned non-JSON for %s", chart_url, exc_info=True)
        return None
    return _median_of_pe_series(payload)


def _find_pros_cons(soup: BeautifulSoup) -> dict[str, list[str]]:
    """Return the pros/cons bullets screener.in shows at the top of the page.

    These short bullets are surprisingly useful as qualitative input for the
    agent's "future growth" and "moat" reasoning.
    """
    bullets: dict[str, list[str]] = {"pros": [], "cons": []}
    try:
        for section_key, header_text in (("pros", "Pros"), ("cons", "Cons")):
            header = soup.find(lambda tag, txt=header_text:
                               tag.name in ("p", "h2", "h3") and
                               tag.get_text(strip=True) == txt)
            if header is None:
                continue
            ul = header.find_next("ul")
            if ul is None:
                continue
            bullets[section_key] = [li.get_text(" ", strip=True) for li in ul.find_all("li")]
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse pros/cons", exc_info=True)
    return bullets


def _parse_table(soup: BeautifulSoup, section_id: str) -> list[dict[str, str]]:
    """Return a list of {column_header: cell_value} dicts for a section's first table.

    section_id is the HTML id of the screener.in section (e.g. "quarters",
    "profit-loss", "balance-sheet", "ratios", "shareholding", "peers").
    """
    try:
        section = soup.find(id=section_id)
        if section is None:
            return []
        table = section.find("table")
        if table is None:
            return []

        headers: list[str] = []
        head = table.find("thead")
        if head is not None:
            headers = [th.get_text(" ", strip=True) for th in head.find_all("th")]

        rows: list[dict[str, str]] = []
        for tr in table.find("tbody").find_all("tr") if table.find("tbody") else []:
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if headers and len(headers) == len(cells):
                rows.append(dict(zip(headers, cells)))
            else:
                rows.append({str(idx): cell for idx, cell in enumerate(cells)})
        return rows
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse table for section %s", section_id, exc_info=True)
        return []


def _find_row_by_label(rows: list[dict[str, str]], label: str) -> dict[str, str] | None:
    """Return the first row whose first column matches `label` (case-insensitive)."""
    target = label.strip().lower()
    for row in rows:
        first_key = next(iter(row.keys()), None)
        if first_key is None:
            continue
        candidate = row[first_key].strip().lower()
        if candidate.startswith(target) or target in candidate:
            return row
    return None


def _yearly_series_from_row(row: dict[str, str] | None) -> list[float | None]:
    """Convert a row of yearly columns into an ordered list of numeric values.

    Skips the first cell (the label) and parses each remaining cell. Missing
    cells become `None` so the agent can see the gap.
    """
    if not row:
        return []
    values: list[float | None] = []
    for index, (_column_name, cell) in enumerate(row.items()):
        if index == 0:
            continue
        values.append(_to_number(cell))
    return values


def _section_markdown(soup: BeautifulSoup, section_id: str, max_chars: int = 1500) -> str:
    """Return a compact text dump of one screener.in section.

    The agent appreciates plain-text context (commentary, footnotes, etc.)
    for its "beyond-the-seven" observations, but the full HTML is too big.
    This helper extracts visible text only and truncates.
    """
    try:
        section = soup.find(id=section_id)
        if section is None:
            return ""
        text = section.get_text(" ", strip=True)
        return text[: max_chars]
    except Exception:  # noqa: BLE001
        return ""


# ---------------------------------------------------------------------------
# HTMX-loaded peer comparison (second HTTP request)
# ---------------------------------------------------------------------------


# Screener.in renders the company-info section with a peers card. The card has
# an empty placeholder div (id="peers" or similar) carrying an HTMX attribute
# that points to the AJAX URL serving the real peer-table HTML fragment. The
# scraper has to follow that URL to actually see the peer data.
_PEERS_URL_REGEX = re.compile(r"/api/company/\d+/peers/[^\s\"'<>]*")


def _extract_peers_url(soup: BeautifulSoup) -> str | None:
    """Find the screener.in HTMX URL that returns the peers table fragment.

    Returns a relative path like ``/api/company/12345/peers/?...`` or ``None``
    if no such URL is present in the page. Tries multiple strategies because
    screener.in occasionally renames or moves the placeholder.
    """
    # Strategy 1: dedicated placeholder element with an hx-get attribute.
    candidates = []
    for element_id in ("peers", "peers-table-placeholder", "peers-table"):
        element = soup.find(id=element_id)
        if element is not None:
            candidates.append(element)
    # Also include any element with class "peers-cell" or similar; if the page
    # changes the wrapper structure we still want to find the attribute.
    candidates.extend(soup.find_all(attrs={"hx-get": True}))

    for element in candidates:
        for attr_name in ("hx-get", "data-href", "data-url", "data-peers-url"):
            value = element.get(attr_name)
            if isinstance(value, str) and "peers" in value:
                return value.strip()

    # Strategy 2: regex-scan the whole HTML for the well-known URL shape. This
    # catches the case where the URL is rendered inside a <script> tag or as
    # part of a data attribute on a parent we did not enumerate.
    raw = str(soup)
    match = _PEERS_URL_REGEX.search(raw)
    if match:
        return match.group(0)

    return None


def _parse_peer_fragment_html(fragment_html: str) -> list[dict[str, str]]:
    """Parse the HTML fragment returned by screener.in's peers endpoint.

    The fragment is a partial document containing one ``<table>`` whose
    header row labels each column. Returns ``[{column_header: cell}, ...]``.
    """
    try:
        fragment_soup = BeautifulSoup(fragment_html, "lxml")
        table = fragment_soup.find("table")
        if table is None:
            return []
        headers: list[str] = []
        head = table.find("thead")
        if head is not None:
            headers = [th.get_text(" ", strip=True) for th in head.find_all("th")]

        rows: list[dict[str, str]] = []
        body = table.find("tbody")
        for tr in (body.find_all("tr") if body else []):
            cells = [td.get_text(" ", strip=True) for td in tr.find_all(["td", "th"])]
            if not cells:
                continue
            if headers and len(headers) == len(cells):
                rows.append(dict(zip(headers, cells)))
            else:
                rows.append({str(idx): cell for idx, cell in enumerate(cells)})
        return rows
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse peer-table fragment", exc_info=True)
        return []


def _fetch_peer_table(
    soup: BeautifulSoup,
    *,
    base_url: str,
    session: requests.Session,
    limit: int = 7,
) -> list[dict[str, str]]:
    """Follow the HTMX peers URL and return the top-N rows (default 7).

    Soft-fails by returning ``[]`` on any error so the agent can still produce
    a verdict (with the Market-leader criterion marked "data unavailable").
    """
    relative = _extract_peers_url(soup)
    if not relative:
        logger.info("No HTMX peers URL found on page %s", base_url)
        return []

    absolute = urljoin(base_url, relative)
    try:
        fragment = _fetch_html(absolute, session)
    except ScreenerInFetchError:
        logger.warning("Peer-table fetch failed for %s", absolute, exc_info=True)
        return []
    if not fragment:
        return []

    rows = _parse_peer_fragment_html(fragment)
    # Drop the screener.in "median" footer row if present — it's an
    # aggregate, not a real peer.
    rows = [
        row for row in rows
        if not any("median" in str(value).strip().lower() for value in row.values())
    ]
    return rows[: max(1, int(limit))]


# ---------------------------------------------------------------------------
# Documents section: announcements + concalls
# ---------------------------------------------------------------------------


def _extract_announcements(soup: BeautifulSoup, *, limit: int = 10) -> list[dict[str, str | None]]:
    """Return the most recent corporate announcements shown on the page.

    Each item: ``{title, posted_at_text, source_url, summary_snippet}``.
    Soft-fails to an empty list if screener.in restructures the section.
    """
    try:
        documents = soup.find(id="documents")
        if documents is None:
            return []
        # The Announcements card is the first card under #documents whose
        # heading text is "Announcements".
        announcements_card = None
        for card in documents.find_all(["div", "section"]):
            header = card.find(["h2", "h3", "h4"])
            if header is None:
                continue
            if header.get_text(strip=True).lower().startswith("announcements"):
                announcements_card = card
                break
        if announcements_card is None:
            return []

        items: list[dict[str, str | None]] = []
        # Each announcement is typically an <a> wrapping the title + a small
        # timestamp + an optional summary snippet block.
        for link in announcements_card.find_all("a", href=True):
            href = link["href"].strip()
            # Skip in-page tabs (#href) and obvious nav links.
            if not href or href.startswith("#"):
                continue
            title_el = link.find(["h4", "strong", "span"])
            title = (title_el.get_text(" ", strip=True) if title_el
                     else link.get_text(" ", strip=True))
            if not title:
                continue
            posted_at = ""
            posted_el = link.find(class_=re.compile(r"(time|date|ago)", re.IGNORECASE))
            if posted_el is not None:
                posted_at = posted_el.get_text(" ", strip=True)
            summary_snippet = ""
            # Look for any sibling paragraph or div that holds a short summary.
            summary_el = link.find_next_sibling(["p", "div"])
            if summary_el is not None:
                summary_text = summary_el.get_text(" ", strip=True)
                # Only keep short snippets; long blocks are usually unrelated.
                if 0 < len(summary_text) <= 400:
                    summary_snippet = summary_text

            items.append(
                {
                    "title": title,
                    "posted_at_text": posted_at or None,
                    "source_url": href,
                    "summary_snippet": summary_snippet or None,
                }
            )
            if len(items) >= limit:
                break
        return items
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse announcements section", exc_info=True)
        return []


def _extract_concalls(soup: BeautifulSoup, *, limit: int = 8) -> list[dict[str, str | None]]:
    """Return the most recent concalls with their transcript / summary / PPT / REC URLs.

    Each item: ``{month, transcript_url, ai_summary_url, ppt_url, rec_url}``.
    Any missing button maps to ``None`` so the agent can decide whether to
    request the transcript.
    """
    try:
        documents = soup.find(id="documents")
        if documents is None:
            return []
        concalls_card = None
        for card in documents.find_all(["div", "section"]):
            header = card.find(["h2", "h3", "h4"])
            if header is None:
                continue
            if header.get_text(strip=True).lower().startswith("concalls"):
                concalls_card = card
                break
        if concalls_card is None:
            return []

        items: list[dict[str, str | None]] = []
        # Each row is typically a flexbox containing the month label and
        # up to four buttons/links: Transcript, AI Summary, PPT, REC.
        for row in concalls_card.find_all(["li", "div"], recursive=True):
            # Find a month label — short text like "Apr 2026", "Mar 2026", etc.
            month_text = ""
            for tag in row.find_all(["span", "div", "p"], recursive=False):
                candidate = tag.get_text(" ", strip=True)
                if re.match(r"^[A-Za-z]{3,9}\s+\d{4}$", candidate):
                    month_text = candidate
                    break
            if not month_text:
                continue

            # Map each link by its visible label.
            link_by_label: dict[str, str] = {}
            for link in row.find_all("a", href=True):
                label = link.get_text(" ", strip=True).lower()
                if not label:
                    continue
                link_by_label[label] = link["href"].strip()

            def _get(*labels: str) -> str | None:
                for label in labels:
                    if label in link_by_label:
                        return link_by_label[label]
                return None

            items.append(
                {
                    "month": month_text,
                    "transcript_url": _get("transcript"),
                    "ai_summary_url": _get("ai summary", "summary"),
                    "ppt_url": _get("ppt", "notes"),
                    "rec_url": _get("rec", "recording"),
                }
            )
            if len(items) >= limit:
                break
        return items
    except Exception:  # noqa: BLE001
        logger.warning("Could not parse concalls section", exc_info=True)
        return []


# ---------------------------------------------------------------------------
# Public fetch + parse
# ---------------------------------------------------------------------------


def _parse_company_page(
    html: str,
    *,
    symbol: str,
    source_url: str,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Turn one screener.in HTML page into the structured dict the agent uses.

    `session` is required to fetch the HTMX-loaded peer table; when ``None``
    (unit tests) the peer table simply stays empty. The rest of the parse is
    pure / IO-free.
    """
    soup = BeautifulSoup(html, "lxml")

    # Header: company name and "About" / sector. About text often mentions
    # incorporation year, sector, and product mix.
    try:
        company_name = (soup.find("h1") or soup.new_tag("h1")).get_text(" ", strip=True) or symbol
    except Exception:  # noqa: BLE001
        company_name = symbol

    about_text = ""
    try:
        about = soup.find(id="company-info")
        if about is not None:
            about_text = about.get_text(" ", strip=True)[:1500]
    except Exception:  # noqa: BLE001
        pass

    sector = ""
    try:
        # Sector usually appears as a hyperlink near the top.
        sector_link = soup.select_one("a[href*='/company/compare/']")
        if sector_link is not None:
            sector = sector_link.get_text(" ", strip=True)
    except Exception:  # noqa: BLE001
        pass

    # Tables across the page. Each is a list[dict] of {column: cell}.
    quarters = _parse_table(soup, "quarters")
    profit_loss = _parse_table(soup, "profit-loss")
    balance_sheet = _parse_table(soup, "balance-sheet")
    cash_flow = _parse_table(soup, "cash-flow")
    ratios_yearly = _parse_table(soup, "ratios")
    shareholding = _parse_table(soup, "shareholding")
    # The peer table is HTMX-loaded — the static page only ships a placeholder.
    # When a session is provided, follow the HTMX URL and parse the fragment.
    # Without a session (unit tests) the peers list stays empty, which the
    # agent already tolerates.
    if session is not None:
        peers = _fetch_peer_table(soup, base_url=source_url, session=session)
    else:
        peers = []
    # Median P/E also comes from a JSON chart endpoint (the on-page "Median PE"
    # line is computed in JS, not present in the HTML). Same session guard as
    # peers: without a session (unit tests) it stays None and the agent falls
    # back to industry_pe.
    if session is not None:
        median_pe = _fetch_median_pe(soup, base_url=source_url, session=session)
    else:
        median_pe = None
    # Announcements + concalls live in the static HTML, no extra fetch needed.
    announcements = _extract_announcements(soup)
    concalls = _extract_concalls(soup)

    # Yearly time series for the criteria.
    revenue_history = _yearly_series_from_row(_find_row_by_label(profit_loss, "Sales"))
    if not revenue_history:
        revenue_history = _yearly_series_from_row(_find_row_by_label(profit_loss, "Revenue"))
    profit_history = _yearly_series_from_row(_find_row_by_label(profit_loss, "Net Profit"))
    eps_history = _yearly_series_from_row(_find_row_by_label(profit_loss, "EPS"))

    # Latest balance-sheet items used in the Net Debt / Equity criterion.
    debt_history = _yearly_series_from_row(_find_row_by_label(balance_sheet, "Borrowings"))
    equity_capital_history = _yearly_series_from_row(_find_row_by_label(balance_sheet, "Equity Capital"))
    reserves_history = _yearly_series_from_row(_find_row_by_label(balance_sheet, "Reserves"))
    cash_history = _yearly_series_from_row(_find_row_by_label(balance_sheet, "Cash"))
    # Some balance sheets show "Other Assets" as a proxy; we keep both.
    investments_history = _yearly_series_from_row(_find_row_by_label(balance_sheet, "Investments"))

    # Latest annual = last column with a number. If the last cell is blank
    # (year not yet reported) fall back one column.
    def _latest(series: list[float | None]) -> float | None:
        for value in reversed(series):
            if value is not None:
                return value
        return None

    payload: dict[str, Any] = {
        "symbol": symbol.upper(),
        "company_name": company_name,
        "sector": sector,
        "about": about_text,
        "source_url": source_url,
        "fetched_at": datetime.now(UTC).isoformat(),
        # Top ratios card
        "current_price": _find_top_ratio(soup, "Current Price"),
        "market_cap": _find_top_ratio(soup, "Market Cap"),
        "pe": _find_top_ratio(soup, "Stock P/E"),
        "p_book": _find_top_ratio(soup, "Price to book"),
        "book_value": _find_top_ratio(soup, "Book Value"),
        "dividend_yield": _find_top_ratio(soup, "Dividend Yield"),
        "roce_ttm": _find_top_ratio(soup, "ROCE"),
        "roe_ttm": _find_top_ratio(soup, "ROE"),
        "face_value": _find_top_ratio(soup, "Face Value"),
        "industry_pe": _find_top_ratio(soup, "Industry P/E"),
        # The stock's own median P/E (preferred valuation anchor), computed from
        # the chart endpoint's PE series. Falls back to None when the endpoint
        # is unavailable; the agent then uses industry_pe in its valuation
        # observation.
        "median_pe": median_pe,
        "promoter_holding_latest": _find_top_ratio(soup, "Promoter holding"),
        # Latest annual values (used by deterministic criteria + agent reasoning)
        "latest_revenue": _latest(revenue_history),
        "latest_net_profit": _latest(profit_history),
        "latest_eps": _latest(eps_history),
        "latest_debt": _latest(debt_history),
        "latest_cash_equivalents": _latest(cash_history),
        "latest_investments": _latest(investments_history),
        "latest_equity_capital": _latest(equity_capital_history),
        "latest_reserves": _latest(reserves_history),
        # Historical series for ATH / trend checks
        "revenue_history": revenue_history,
        "profit_history": profit_history,
        "eps_history": eps_history,
        # Full table dumps for the agent's qualitative analysis
        "quarters": quarters,
        "profit_loss": profit_loss,
        "balance_sheet": balance_sheet,
        "cash_flow": cash_flow,
        "ratios_yearly": ratios_yearly,
        "shareholding": shareholding,
        "peers": peers,
        "announcements": announcements,
        "concalls": concalls,
        "pros_cons": _find_pros_cons(soup),
        # Compact text dumps for the agent's free-form reasoning. Keeping them
        # under ~1500 chars each avoids blowing up the token budget while
        # still letting the LLM read the unstructured commentary.
        "raw_text": {
            "about": about_text,
            "shareholding_notes": _section_markdown(soup, "shareholding"),
            "cash_flow_notes": _section_markdown(soup, "cash-flow"),
        },
    }
    return payload


def fetch_company_data(
    symbol: str,
    *,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Scrape and parse one screener.in company page.

    The Streamlit UI uses `FundamentalsCache` to cache the result, so this
    function does not cache. It only handles HTTP + parse.
    """
    if not symbol or not str(symbol).strip():
        raise ValueError("fetch_company_data: symbol must be a non-empty string")

    normalized = str(symbol).strip().upper()
    owned_session = session is None
    sess = session or requests.Session()

    try:
        consolidated_url = _SCREENER_URL_CONSOLIDATED.format(symbol=normalized)
        html = _fetch_html(consolidated_url, sess)
        source_url = consolidated_url
        if html is None:
            # Fall back to the standalone variant.
            standalone_url = _SCREENER_URL_STANDALONE.format(symbol=normalized)
            html = _fetch_html(standalone_url, sess)
            source_url = standalone_url
            if html is None:
                raise ScreenerInFetchError(
                    f"screener.in has no page for symbol '{normalized}'. "
                    "Confirm the NSE ticker is correct."
                )

        # Polite pause AFTER the successful fetch so a burst of calls from the
        # same process still respects the configured delay.
        time.sleep(_request_delay_seconds())
        # Pass the session through so `_parse_company_page` can fire the
        # second HTMX request that fetches the peers-table fragment.
        return _parse_company_page(
            html,
            symbol=normalized,
            source_url=source_url,
            session=sess,
        )
    finally:
        if owned_session:
            sess.close()
