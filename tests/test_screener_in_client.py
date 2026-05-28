from __future__ import annotations

"""Tests for the screener.in scraper / parser.

No live HTTP is involved. The parser is exercised against a small synthetic
HTML fixture below that mirrors the structure of a real screener.in
company page (top-ratios card, peers/quarters/profit-loss/balance-sheet
tables, pros/cons block).
"""

import pytest

from backend.fundamentals.screener_in_client import (
    ScreenerInFetchError,
    _parse_company_page,
    fetch_company_data,
)


# A trimmed-but-realistic snapshot of how screener.in lays out a company page.
# The IDs (#top-ratios, #quarters, #profit-loss, #balance-sheet, #peers) and
# the inner span.name / span.number markup all match what the live site uses.
_DEMO_HTML = """
<html>
  <head><title>Demo Industries</title></head>
  <body>
    <h1>Demo Industries Ltd.</h1>
    <a href="/company/compare/00000000/Banks/">Banks</a>
    <div id="company-info">
      Demo Industries Ltd. is a private-sector bank incorporated in 1995.
    </div>
    <ul id="top-ratios">
      <li><span class="name">Market Cap</span><span class="number">150,000 Cr.</span></li>
      <li><span class="name">Current Price</span><span class="number">1,500</span></li>
      <li><span class="name">Stock P/E</span><span class="number">22.5</span></li>
      <li><span class="name">Book Value</span><span class="number">450</span></li>
      <li><span class="name">Dividend Yield</span><span class="number">1.2 %</span></li>
      <li><span class="name">ROCE</span><span class="number">18.5 %</span></li>
      <li><span class="name">ROE</span><span class="number">16.0 %</span></li>
      <li><span class="name">Face Value</span><span class="number">2</span></li>
      <li><span class="name">Promoter holding</span><span class="number">26 %</span></li>
    </ul>
    <p>Pros</p>
    <ul><li>Strong return on capital</li><li>Healthy growth</li></ul>
    <p>Cons</p>
    <ul><li>Promoter holding decreased modestly</li></ul>
    <section id="profit-loss">
      <table>
        <thead>
          <tr><th>Item</th><th>Mar 2020</th><th>Mar 2021</th><th>Mar 2022</th><th>Mar 2023</th><th>Mar 2024</th></tr>
        </thead>
        <tbody>
          <tr><td>Sales</td><td>40,000</td><td>45,000</td><td>50,000</td><td>58,000</td><td>62,000</td></tr>
          <tr><td>Net Profit</td><td>180</td><td>210</td><td>230</td><td>260</td><td>280</td></tr>
          <tr><td>EPS</td><td>30</td><td>34</td><td>37</td><td>42</td><td>45</td></tr>
        </tbody>
      </table>
    </section>
    <section id="balance-sheet">
      <table>
        <thead>
          <tr><th>Item</th><th>Mar 2024</th></tr>
        </thead>
        <tbody>
          <tr><td>Equity Capital</td><td>200</td></tr>
          <tr><td>Reserves</td><td>10,000</td></tr>
          <tr><td>Borrowings</td><td>1,500</td></tr>
          <tr><td>Cash</td><td>800</td></tr>
        </tbody>
      </table>
    </section>
    <section id="quarters">
      <table>
        <thead>
          <tr><th>Item</th><th>Q1FY24</th><th>Q2FY24</th><th>Q3FY24</th><th>Q4FY24</th></tr>
        </thead>
        <tbody>
          <tr><td>Sales</td><td>14,000</td><td>15,000</td><td>16,000</td><td>17,000</td></tr>
          <tr><td>Net Profit</td><td>60</td><td>65</td><td>70</td><td>85</td></tr>
        </tbody>
      </table>
    </section>
    <section id="peers">
      <table>
        <thead>
          <tr><th>Name</th><th>P/E</th><th>Market Cap</th><th>ROCE</th></tr>
        </thead>
        <tbody>
          <tr><td>Demo Industries</td><td>22.5</td><td>150,000</td><td>18.5</td></tr>
          <tr><td>Peer A</td><td>25.0</td><td>120,000</td><td>16.0</td></tr>
          <tr><td>Peer B</td><td>20.0</td><td>90,000</td><td>17.5</td></tr>
        </tbody>
      </table>
    </section>
  </body>
</html>
"""


def test_parse_company_page_extracts_top_ratios():
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    assert payload["symbol"] == "DEMO"
    assert payload["company_name"].startswith("Demo Industries")
    assert payload["market_cap"] == pytest.approx(150000)
    assert payload["current_price"] == pytest.approx(1500)
    assert payload["pe"] == pytest.approx(22.5)
    assert payload["roce_ttm"] == pytest.approx(18.5)
    assert payload["roe_ttm"] == pytest.approx(16.0)
    assert payload["promoter_holding_latest"] == pytest.approx(26)


def test_parse_company_page_extracts_yearly_history():
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    # Five years of revenue / profit / EPS, oldest first.
    assert payload["revenue_history"] == [40000, 45000, 50000, 58000, 62000]
    assert payload["profit_history"] == [180, 210, 230, 260, 280]
    assert payload["eps_history"] == [30, 34, 37, 42, 45]

    # Latest = last non-None entry.
    assert payload["latest_net_profit"] == pytest.approx(280)
    assert payload["latest_revenue"] == pytest.approx(62000)
    assert payload["latest_eps"] == pytest.approx(45)


def test_parse_company_page_extracts_balance_sheet_pieces_for_net_debt_calc():
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    # These values feed the Net Debt / Equity criterion in the agent.
    assert payload["latest_debt"] == pytest.approx(1500)
    assert payload["latest_cash_equivalents"] == pytest.approx(800)
    assert payload["latest_equity_capital"] == pytest.approx(200)
    assert payload["latest_reserves"] == pytest.approx(10000)


def test_parse_company_page_captures_tables_but_skips_peers_without_session():
    """With Job 4, the peer table is fetched via a second HTMX request.

    `_parse_company_page` only triggers that second fetch when a
    `requests.Session` is passed in. The end-to-end peers extraction is
    covered by `test_fetch_peer_table_*` below — this test only confirms
    that the static-tables path still works AND that peers stay empty
    in the no-session path used by unit tests.
    """
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    # Tables come back as a list[dict] with the original column headers.
    assert len(payload["profit_loss"]) >= 3  # Sales, Net Profit, EPS rows
    assert len(payload["quarters"]) >= 2
    # Peers stays empty without a session — the static <section id="peers">
    # on the live screener.in page is only a placeholder. See
    # `test_fetch_peer_table_follows_htmx_url_and_strips_median_row` for
    # the actual peer-fetch coverage.
    assert payload["peers"] == []
    # Job 4 additions: announcements and concalls now live in the payload too.
    assert "announcements" in payload
    assert "concalls" in payload


def test_parse_company_page_picks_up_pros_and_cons():
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")
    pros_cons = payload["pros_cons"]
    assert "Strong return on capital" in pros_cons["pros"]
    assert any("Promoter holding" in con for con in pros_cons["cons"])


def test_fetch_company_data_rejects_empty_symbol():
    with pytest.raises(ValueError):
        fetch_company_data("")


def test_fetch_company_data_raises_on_persistent_404(monkeypatch):
    # Both URL variants return None (simulating 404). The function should
    # raise ScreenerInFetchError with a helpful message.
    from backend.fundamentals import screener_in_client as module

    monkeypatch.setattr(module, "_fetch_html", lambda url, session: None)
    monkeypatch.setattr(module, "_request_delay_seconds", lambda: 0)

    with pytest.raises(ScreenerInFetchError):
        fetch_company_data("UNKNOWN_TICKER")


def test_fetch_company_data_uses_consolidated_then_falls_back(monkeypatch):
    """First URL (consolidated) returns None → second URL (standalone) returns HTML."""
    from backend.fundamentals import screener_in_client as module

    call_log: list[str] = []

    def fake_fetch(url, session):
        call_log.append(url)
        # First call (consolidated) returns None to simulate 404.
        if len(call_log) == 1:
            return None
        # Second call returns our demo HTML.
        return _DEMO_HTML

    monkeypatch.setattr(module, "_fetch_html", fake_fetch)
    monkeypatch.setattr(module, "_request_delay_seconds", lambda: 0)

    payload = fetch_company_data("DEMO")

    # Two URLs tried, consolidated first, standalone second.
    assert len(call_log) == 2
    assert call_log[0].endswith("/consolidated/")
    assert payload["symbol"] == "DEMO"
    assert payload["source_url"] == call_log[1]


# ---------------------------------------------------------------------------
# Job 4: HTMX peer fetch
# ---------------------------------------------------------------------------


_HTML_WITH_PEER_PLACEHOLDER = """
<html><body>
  <h1>Demo Industries Ltd.</h1>
  <div id="peers" hx-get="/api/company/12345/peers/?sort=mar_cap" hx-trigger="load"></div>
</body></html>
"""

_PEERS_FRAGMENT_HTML = """
<table>
  <thead>
    <tr><th>S.No.</th><th>Name</th><th>CMP Rs.</th><th>P/E</th><th>Mar Cap Rs.Cr.</th></tr>
  </thead>
  <tbody>
    <tr><td>1</td><td>Demo Industries</td><td>2281.00</td><td>15.70</td><td>825285.85</td></tr>
    <tr><td>2</td><td>Peer One</td><td>1159.15</td><td>15.60</td><td>470111.80</td></tr>
    <tr><td>3</td><td>Peer Two</td><td>1165.15</td><td>18.27</td><td>316182.79</td></tr>
    <tr><td>Median: 68 Co.</td><td></td><td>258.5</td><td>20.62</td><td>1261.91</td></tr>
  </tbody>
</table>
"""


def test_extract_peers_url_finds_hx_get_attribute():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_peers_url

    soup = BeautifulSoup(_HTML_WITH_PEER_PLACEHOLDER, "lxml")
    url = _extract_peers_url(soup)
    assert url is not None
    assert url.startswith("/api/company/12345/peers/")


def test_extract_peers_url_falls_back_to_regex_scan():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_peers_url

    # No hx-get attribute — the URL is hidden inside a <script> tag.
    html = """
    <html><body>
      <h1>Hidden Peers</h1>
      <script>const peersUrl = "/api/company/99999/peers/";</script>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    url = _extract_peers_url(soup)
    assert url == "/api/company/99999/peers/"


def test_extract_peers_url_returns_none_when_absent():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_peers_url

    soup = BeautifulSoup("<html><body><p>No placeholder here.</p></body></html>", "lxml")
    assert _extract_peers_url(soup) is None


def test_fetch_peer_table_follows_htmx_url_and_strips_median_row(monkeypatch):
    from bs4 import BeautifulSoup
    from backend.fundamentals import screener_in_client as module

    monkeypatch.setattr(module, "_fetch_html", lambda url, session: _PEERS_FRAGMENT_HTML)

    soup = BeautifulSoup(_HTML_WITH_PEER_PLACEHOLDER, "lxml")
    rows = module._fetch_peer_table(
        soup,
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]  # _fetch_html is monkey-patched
        limit=7,
    )
    # The Median footer row must be removed; only real peers remain.
    names = [row.get("Name", "") for row in rows]
    assert "Demo Industries" in names
    assert "Peer One" in names
    assert not any("Median" in name for name in names)


def test_fetch_peer_table_caps_at_limit(monkeypatch):
    """Top-7 cap is honored even when the endpoint returns more rows."""
    from bs4 import BeautifulSoup
    from backend.fundamentals import screener_in_client as module

    # Build a fragment with 12 rows.
    body_rows = "".join(
        f"<tr><td>{i}</td><td>Peer {i}</td><td>1.0</td><td>2.0</td><td>3.0</td></tr>"
        for i in range(1, 13)
    )
    fragment = f"""
    <table>
      <thead><tr><th>S.No.</th><th>Name</th><th>CMP</th><th>P/E</th><th>Mar Cap</th></tr></thead>
      <tbody>{body_rows}</tbody>
    </table>
    """
    monkeypatch.setattr(module, "_fetch_html", lambda url, session: fragment)

    soup = BeautifulSoup(_HTML_WITH_PEER_PLACEHOLDER, "lxml")
    rows = module._fetch_peer_table(
        soup,
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )
    assert len(rows) == 7


def test_fetch_peer_table_soft_fails_when_url_missing(monkeypatch):
    from bs4 import BeautifulSoup
    from backend.fundamentals import screener_in_client as module

    # _fetch_html would not be called because _extract_peers_url returns None.
    monkeypatch.setattr(
        module,
        "_fetch_html",
        lambda url, session: pytest.fail("_fetch_html should not be called"),
    )
    soup = BeautifulSoup("<html><body><p>nothing</p></body></html>", "lxml")
    rows = module._fetch_peer_table(
        soup,
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )
    assert rows == []


def test_fetch_peer_table_soft_fails_on_fetch_error(monkeypatch):
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import ScreenerInFetchError
    from backend.fundamentals import screener_in_client as module

    def boom(url, session):
        raise ScreenerInFetchError("simulated rate limit")

    monkeypatch.setattr(module, "_fetch_html", boom)
    soup = BeautifulSoup(_HTML_WITH_PEER_PLACEHOLDER, "lxml")
    rows = module._fetch_peer_table(
        soup,
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )
    assert rows == []


# ---------------------------------------------------------------------------
# Job 4: announcements + concalls extraction
# ---------------------------------------------------------------------------


_HTML_WITH_DOCUMENTS = """
<html><body>
<section id="documents">
  <div>
    <h3>Announcements</h3>
    <a href="https://bse.example.com/file/abc.pdf">
      <span>Press Release - SKF Awards Global AI-Led Business</span>
      <span class="time">8h</span>
    </a>
    <p>SKF awards TCS a global AI-led business transformation contract to modernize its IT landscape.</p>
    <a href="https://bse.example.com/file/def.pdf">
      <span>Announcement under Regulation 30 (LODR) - Analyst Meet</span>
      <span class="time">1d</span>
    </a>
    <a href="https://bse.example.com/file/ghi.pdf">
      <span>Press Release - TCS Launches SovereignSecure Cloud</span>
      <span class="time">1d</span>
    </a>
  </div>
  <div>
    <h3>Concalls</h3>
    <ul>
      <li>
        <span>Apr 2026</span>
        <a href="https://concall.example.com/q4fy26-transcript.pdf">Transcript</a>
        <a href="https://concall.example.com/q4fy26-summary">AI Summary</a>
        <a href="https://concall.example.com/q4fy26-ppt.pdf">PPT</a>
        <a href="https://concall.example.com/q4fy26-rec">REC</a>
      </li>
      <li>
        <span>Jan 2026</span>
        <a href="https://concall.example.com/q3fy26-transcript.pdf">Transcript</a>
        <a href="https://concall.example.com/q3fy26-ppt.pdf">PPT</a>
      </li>
      <li>
        <span>Oct 2025</span>
        <a href="https://concall.example.com/q2fy26-ppt.pdf">PPT</a>
      </li>
    </ul>
  </div>
</section>
</body></html>
"""


def test_extract_announcements_returns_titles_and_links():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_announcements

    soup = BeautifulSoup(_HTML_WITH_DOCUMENTS, "lxml")
    items = _extract_announcements(soup)

    assert len(items) >= 3
    first = items[0]
    assert first["title"].startswith("Press Release")
    assert first["source_url"].endswith("/abc.pdf")
    # posted_at_text comes from the .time span.
    assert first["posted_at_text"] == "8h"


def test_extract_announcements_caps_at_limit():
    """Even if the page lists 50 announcements, only the first `limit` are kept."""
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_announcements

    items_html = "".join(
        f"<a href='https://bse.example.com/{i}.pdf'><span>Title {i}</span>"
        f"<span class='time'>{i}h</span></a>"
        for i in range(50)
    )
    html = f"""
    <html><body><section id="documents"><div><h3>Announcements</h3>
    {items_html}
    </div></section></body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    items = _extract_announcements(soup, limit=10)
    assert len(items) == 10


def test_extract_announcements_soft_fails_when_section_missing():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_announcements

    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    assert _extract_announcements(soup) == []


def test_extract_concalls_picks_up_transcript_and_button_urls():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_concalls

    soup = BeautifulSoup(_HTML_WITH_DOCUMENTS, "lxml")
    rows = _extract_concalls(soup)

    # Three months in the fixture.
    months = [row["month"] for row in rows]
    assert "Apr 2026" in months
    assert "Jan 2026" in months
    assert "Oct 2025" in months

    # The Apr 2026 row should have all four URLs populated.
    apr = next(row for row in rows if row["month"] == "Apr 2026")
    assert apr["transcript_url"].endswith("q4fy26-transcript.pdf")
    assert apr["ai_summary_url"].endswith("q4fy26-summary")
    assert apr["ppt_url"].endswith("q4fy26-ppt.pdf")
    assert apr["rec_url"].endswith("q4fy26-rec")

    # Oct 2025 only has the PPT button — transcript and others must be None.
    oct_2025 = next(row for row in rows if row["month"] == "Oct 2025")
    assert oct_2025["transcript_url"] is None
    assert oct_2025["ai_summary_url"] is None
    assert oct_2025["rec_url"] is None
    assert oct_2025["ppt_url"].endswith("q2fy26-ppt.pdf")


def test_extract_concalls_caps_at_limit():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _extract_concalls

    rows_html = "".join(
        f"<li><span>Q{i} 2026</span><a href='https://x/{i}.pdf'>Transcript</a></li>"
        for i in range(20)
    )
    # Use month-name pattern that matches the parser's regex.
    rows_html = "".join(
        f"<li><span>Jan {2000 + i}</span><a href='https://x/{i}.pdf'>Transcript</a></li>"
        for i in range(20)
    )
    html = f"""
    <html><body><section id="documents"><div><h3>Concalls</h3>
    <ul>{rows_html}</ul>
    </div></section></body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    rows = _extract_concalls(soup, limit=8)
    assert len(rows) == 8


# ---------------------------------------------------------------------------
# Job 5: median P/E parsing
# ---------------------------------------------------------------------------


def test_find_median_pe_picks_explicit_label_from_top_ratios():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _find_median_pe

    html = """
    <html><body>
      <ul id="top-ratios">
        <li><span class="name">Stock P/E</span><span class="number">25.40</span></li>
        <li><span class="name">Median P/E</span><span class="number">18.75</span></li>
        <li><span class="name">Industry P/E</span><span class="number">22.10</span></li>
      </ul>
    </body></html>
    """
    soup = BeautifulSoup(html, "lxml")
    assert _find_median_pe(soup) == pytest.approx(18.75)


def test_find_median_pe_computes_median_from_ratios_table_when_top_label_missing():
    """No explicit median-P/E label, but the ratios table has a 'Stock P/E' row.
    The helper should compute the median of its yearly values."""
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _find_median_pe

    soup = BeautifulSoup("<html><body></body></html>", "lxml")
    ratios_yearly = [
        # First column is the label; remaining cells are yearly values.
        {"0": "Stock P/E", "1": "10", "2": "15", "3": "20", "4": "25", "5": "30"},
    ]
    # Median of [10, 15, 20, 25, 30] is 20.
    assert _find_median_pe(soup, ratios_yearly) == pytest.approx(20.0)


def test_find_median_pe_returns_none_when_unavailable():
    from bs4 import BeautifulSoup
    from backend.fundamentals.screener_in_client import _find_median_pe

    soup = BeautifulSoup("<html><body><p>No ratios here.</p></body></html>", "lxml")
    assert _find_median_pe(soup) is None
    assert _find_median_pe(soup, []) is None
    # Ratios table without a P/E row → still None.
    assert _find_median_pe(soup, [{"0": "ROCE", "1": "12", "2": "14"}]) is None


def test_parse_company_page_payload_includes_median_pe_field():
    """After Job 5 the parsed payload always includes a `median_pe` key (may be None)."""
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")
    assert "median_pe" in payload
