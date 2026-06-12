"""Tests for the screener.in scraper / parser.

No live HTTP is involved. The parser is exercised against a small synthetic
HTML fixture below that mirrors the structure of a real screener.in
company page (top-ratios card, peers/quarters/profit-loss/balance-sheet
tables, pros/cons block).
"""

from __future__ import annotations

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


def test_parse_company_page_captures_static_peer_table_without_session():
    """A visible peer table in the main HTML should be parsed without HTMX."""
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    # Tables come back as a list[dict] with the original column headers.
    assert len(payload["profit_loss"]) >= 3  # Sales, Net Profit, EPS rows
    assert len(payload["quarters"]) >= 2
    assert [row["Name"] for row in payload["peers"]] == [
        "Demo Industries",
        "Peer A",
        "Peer B",
    ]
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

_HTML_WITH_STATIC_PEER_TABLE = """
<html><body>
  <h1>Demo Industries Ltd.</h1>
  <section id="peers">
    <h2>Peer comparison</h2>
    <table>
      <thead>
        <tr><th>S.No.</th><th>Name</th><th>CMP Rs.</th><th>P/E</th><th>Mar Cap Rs.Cr.</th></tr>
      </thead>
      <tbody>
        <tr><td>1.</td><td>Demo Industries</td><td>2281.00</td><td>15.70</td><td>825285.85</td></tr>
        <tr><td>2.</td><td>Peer One</td><td>1159.15</td><td>15.60</td><td>470111.80</td></tr>
        <tr><td>Median: 68 Co.</td><td></td><td>258.5</td><td>20.62</td><td>1261.91</td></tr>
      </tbody>
    </table>
  </section>
</body></html>
"""

_HTML_WITH_HTMX_AND_STATIC_PEERS = _HTML_WITH_STATIC_PEER_TABLE.replace(
    '<section id="peers">',
    '<section id="peers" hx-get="/api/company/12345/peers/?sort=mar_cap">',
)


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


def test_fetch_peer_table_uses_static_table_when_htmx_url_missing():
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    rows = module._fetch_peer_table(
        BeautifulSoup(_HTML_WITH_STATIC_PEER_TABLE, "lxml"),
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )

    names = [row.get("Name", "") for row in rows]
    assert names == ["Demo Industries", "Peer One"]
    assert not any("Median" in name for name in names)


def test_fetch_peer_table_falls_back_to_static_table_when_htmx_fetch_fails(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module
    from backend.fundamentals.screener_in_client import ScreenerInFetchError

    def boom(url, session):
        raise ScreenerInFetchError("simulated rate limit")

    monkeypatch.setattr(module, "_fetch_html", boom)

    rows = module._fetch_peer_table(
        BeautifulSoup(_HTML_WITH_HTMX_AND_STATIC_PEERS, "lxml"),
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )

    assert [row.get("Name", "") for row in rows] == ["Demo Industries", "Peer One"]


def test_fetch_peer_table_falls_back_to_static_table_when_htmx_fragment_empty(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    monkeypatch.setattr(module, "_fetch_html", lambda url, session: "<div>no rows</div>")

    rows = module._fetch_peer_table(
        BeautifulSoup(_HTML_WITH_HTMX_AND_STATIC_PEERS, "lxml"),
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
        limit=7,
    )

    assert [row.get("Name", "") for row in rows] == ["Demo Industries", "Peer One"]


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

    from backend.fundamentals import screener_in_client as module
    from backend.fundamentals.screener_in_client import ScreenerInFetchError

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


def test_fetch_peer_table_rejects_absolute_private_peer_url(monkeypatch):
    """HTMX peer URLs must stay on screener.in.

    The peers URL is copied out of third-party HTML. If that HTML ever points at
    localhost or a LAN host, the parser should fail closed and skip the peers
    section instead of fetching attacker-chosen infrastructure.
    """
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    html = """
    <html><body>
      <div id="peers" hx-get="http://127.0.0.1:8080/private/peers"></div>
    </body></html>
    """
    monkeypatch.setattr(
        module,
        "_fetch_html",
        lambda url, session: pytest.fail(f"unsafe URL was fetched: {url}"),
    )

    rows = module._fetch_peer_table(
        BeautifulSoup(html, "lxml"),
        base_url="https://www.screener.in/company/DEMO/",
        session=None,  # type: ignore[arg-type]
    )

    assert rows == []


def test_fetch_html_rejects_response_larger_than_cap(monkeypatch):
    """The raw HTML helper should stream with a byte cap instead of reading .text.

    Screener pages and JSON fragments are small. A hostile endpoint returning a
    giant body should raise before the full response is materialized in memory.
    """
    from backend.fundamentals import screener_in_client as module

    class _LargeResponse:
        status_code = 200
        url = "https://www.screener.in/company/DEMO/"
        headers = {"Content-Type": "text/html"}

        def iter_content(self, chunk_size=1):
            yield b"A" * 20

        def close(self):
            pass

    class _LargeSession:
        def get(self, url, **kwargs):
            return _LargeResponse()

    monkeypatch.setattr(module, "_MAX_HTML_BYTES", 10, raising=False)

    with pytest.raises(ScreenerInFetchError, match="exceeded"):
        module._fetch_html("https://www.screener.in/company/DEMO/", _LargeSession())


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
# Median P/E — fetched from the chart endpoint and computed locally
#
# screener.in renders the "Median PE" chart line in JavaScript; it is NOT in
# the page HTML. The data comes from /api/company/<id>/chart/?q=Price+to+Earning
# as a {"datasets": [{"values": [[date, pe], ...]}]} payload, and the median is
# computed client-side. These tests exercise that fetch+compute path with a
# monkeypatched _fetch_html (no live network), mirroring the peers-table tests.
# ---------------------------------------------------------------------------


# A trimmed chart payload: [date, pe] pairs with nulls on non-trading days.
# Median of the non-null values [10, 20, 30, 40] is 25.0.
_PE_CHART_JSON = (
    '{"datasets": [{"metric": "Price to Earning", "label": "PE", "values": '
    '[["2021-06-04", 10], ["2021-06-11", null], ["2021-06-18", 20], '
    '["2021-06-25", 30], ["2021-07-02", 40]], "meta": {}}]}'
)

# A page that embeds the numeric company id in an /api/company/<id>/ URL, the
# way the live site does (watchlist add link, chart/peers endpoints, …).
_HTML_WITH_COMPANY_ID = """
<html><body>
  <button hx-post="/api/company/3365/add/">Follow</button>
</body></html>
"""


def test_extract_company_id_finds_numeric_id():
    from bs4 import BeautifulSoup

    from backend.fundamentals.screener_in_client import _extract_company_id

    soup = BeautifulSoup(_HTML_WITH_COMPANY_ID, "lxml")
    assert _extract_company_id(soup) == "3365"


def test_extract_company_id_returns_none_when_absent():
    from bs4 import BeautifulSoup

    from backend.fundamentals.screener_in_client import _extract_company_id

    soup = BeautifulSoup("<html><body><p>no api urls here</p></body></html>", "lxml")
    assert _extract_company_id(soup) is None


def test_median_of_pe_series_ignores_nulls():
    from backend.fundamentals.screener_in_client import _median_of_pe_series

    payload = {"datasets": [{"values": [["d1", 10], ["d2", None], ["d3", 20], ["d4", 30], ["d5", 40]]}]}
    # Median of [10, 20, 30, 40] = 25.0.
    assert _median_of_pe_series(payload) == pytest.approx(25.0)


def test_median_of_pe_series_returns_none_on_empty_or_malformed():
    from backend.fundamentals.screener_in_client import _median_of_pe_series

    assert _median_of_pe_series({}) is None
    assert _median_of_pe_series({"datasets": []}) is None
    assert _median_of_pe_series({"datasets": [{"values": []}]}) is None
    assert _median_of_pe_series({"datasets": [{"values": [["d1", None]]}]}) is None


def test_fetch_median_pe_computes_median_from_chart_endpoint(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    monkeypatch.setattr(module, "_fetch_html", lambda url, session: _PE_CHART_JSON)

    soup = BeautifulSoup(_HTML_WITH_COMPANY_ID, "lxml")
    median = module._fetch_median_pe(
        soup,
        base_url="https://www.screener.in/company/DEMO/consolidated/",
        session=None,  # type: ignore[arg-type]  # _fetch_html is monkey-patched
    )
    # Median of [10, 20, 30, 40] (the null is skipped) = 25.0.
    assert median == pytest.approx(25.0)


def test_fetch_median_pe_soft_fails_when_company_id_missing(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    # No company id on the page → _fetch_html must not be called.
    monkeypatch.setattr(
        module,
        "_fetch_html",
        lambda url, session: pytest.fail("_fetch_html should not be called"),
    )
    soup = BeautifulSoup("<html><body><p>nothing</p></body></html>", "lxml")
    assert module._fetch_median_pe(
        soup, base_url="https://www.screener.in/company/DEMO/", session=None  # type: ignore[arg-type]
    ) is None


def test_fetch_median_pe_soft_fails_on_fetch_error(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    def boom(url, session):
        raise ScreenerInFetchError("boom")

    monkeypatch.setattr(module, "_fetch_html", boom)
    soup = BeautifulSoup(_HTML_WITH_COMPANY_ID, "lxml")
    assert module._fetch_median_pe(
        soup, base_url="https://www.screener.in/company/DEMO/", session=None  # type: ignore[arg-type]
    ) is None


def test_fetch_median_pe_soft_fails_on_non_json(monkeypatch):
    from bs4 import BeautifulSoup

    from backend.fundamentals import screener_in_client as module

    monkeypatch.setattr(module, "_fetch_html", lambda url, session: "<html>not json</html>")
    soup = BeautifulSoup(_HTML_WITH_COMPANY_ID, "lxml")
    assert module._fetch_median_pe(
        soup, base_url="https://www.screener.in/company/DEMO/", session=None  # type: ignore[arg-type]
    ) is None


def test_parse_company_page_payload_includes_median_pe_field():
    """After Job 5 the parsed payload always includes a `median_pe` key (may be None)."""
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")
    assert "median_pe" in payload
