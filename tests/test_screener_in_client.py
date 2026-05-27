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


def test_parse_company_page_captures_tables_and_peers():
    payload = _parse_company_page(_DEMO_HTML, symbol="DEMO", source_url="http://test/demo")

    # Tables come back as a list[dict] with the original column headers.
    assert len(payload["profit_loss"]) >= 3  # Sales, Net Profit, EPS rows
    assert len(payload["quarters"]) >= 2
    # The peer table should list at least the company itself and two peers.
    assert len(payload["peers"]) == 3
    assert payload["peers"][0]["Name"].startswith("Demo Industries")


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
