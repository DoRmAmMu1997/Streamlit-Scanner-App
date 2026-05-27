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
  LangChain agent can wrap it as a Tool without touching the parser
  internals.
"""

import logging
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

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
# Public fetch + parse
# ---------------------------------------------------------------------------


def _parse_company_page(html: str, *, symbol: str, source_url: str) -> dict[str, Any]:
    """Turn one screener.in HTML page into the structured dict the agent uses."""
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
    peers = _parse_table(soup, "peers-table-placeholder") or _parse_table(soup, "peers")

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
        return _parse_company_page(html, symbol=normalized, source_url=source_url)
    finally:
        if owned_session:
            sess.close()
