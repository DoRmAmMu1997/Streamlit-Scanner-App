# Streamlit Scanner App

A pluggable **daily-candle stock scanner** for Indian equities. It downloads
historical price data from [DhanHQ](https://dhanhq.co/), runs technical-analysis
**screeners** over a universe of stocks, and shows the shortlisted symbols in a
[Streamlit](https://streamlit.io/) web app with interactive charts.

It is designed to be easy to extend: a "screener" is just a small Python file
dropped into the `screeners/` folder.

Shortlisted stocks can also be sent to a built-in **"Check Fundamentals"
agent** (powered by the [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview)
running on your Claude subscription). The agent scrapes [screener.in](https://www.screener.in/) for the
selected stock and returns a structured fundamental analysis — pass/fail on
user-defined criteria (nine for the Hemant Super 45 / Nifty 100 universe, seven
for every other stock), a 0–10 holistic rating, peer / margin / governance
observations, and a three-part forward outlook (announcements signal + concall
transcript signal + integrated view).

> **Disclaimer:** This is an educational / personal research tool. Nothing here
> is financial advice. Always do your own research before trading.

---

## Changes Codex did

- Added the `67 Ka Funda (AI)` screener: a deterministic 67% drawdown and
  100% upside-to-ATH shortlister runs first, then a Claude Agent SDK verifier
  approves only BUY rows backed by web and Screener.in evidence.
- Added `backend/sixty_seven/` with the OHLC shortlister, SerpAPI Google search
  client, and Claude verifier that treats all external text as untrusted
  evidence.
- Added the `hemant_super_good_200_union` universe and generated
  `data/universes/hemant_super_good_200_union.csv`.
- Added tests for the shortlister, SerpAPI client, Claude verifier, screener
  flow, universe generation, and secret redaction.
- Co-authored by: DoRmAmMu1997 <hemantdhamija@gmail.com> and Codex
  <codex@openai.com>.

---

## Features

- **Ten built-in screeners**, all built on a common `BaseScanner` abstract
  base class so adding new ones is a single-file change.
  - **Heikin Ashi SuperTrend** — F&O stocks where the daily Heikin Ashi close
    crosses the SuperTrend line.
  - **Bollinger Band Reversal** — F&O stocks printing a daily Bollinger Band
    rejection candle.
  - **Bollinger Lower Band** — Hemant Super 45 stocks whose latest close is at,
    below, or within a small buffer of the lower Bollinger Band(200, 2.5).
    (Distinct from *Bollinger Band Reversal* above, which scans F&O stocks for
    outer-band rejection candles.)
  - **Envelope** — Hemant Super 45 stocks whose latest close is at or below the
    lower Envelope band (200-EMA basis, 14% bands) — i.e. ≥14% below the 200 EMA.
  - **Envelope + Knoxville** — Hemant Super 45 stocks near the lower Envelope
    band (200-EMA basis, 14% bands) with a recent bullish Knoxville Divergence
    (Bars Back 20, RSI 14).
  - **Stochastic Swing** — NIFTY 500 stocks with a fresh Stochastic swing entry
    (a `%K`/`%D` cross out of the oversold/overbought zone, confirmed by the
    200 SMA trend and a recent 5 EMA / 200 SMA crossover).
  - **52 Week High/Low (Ceyhun)** — Hemant Super 45 stocks whose close came
    within a tolerance (default 2%) of the trailing 252-day low on any of the
    last 10 trading days.
  - **20% Up Green Candles (Lovevanshi)** — Hemant Super 45 ∪ Good 45 stocks
    whose latest candle caps a run of consecutive green candles (up to 20) that
    moved more than 20% from the run's lowest low to its highest high.
  - **67 Ka Funda (AI)** — Hemant Super 45 + Good 45 + Good 200 stocks that have
    fallen at least 67% from their available-history all-time high (with ≥100%
    upside back to it). A cheap deterministic drawdown gate shortlists candidates,
    then a **Claude Agent SDK** verifier researches each survivor (Screener.in data
    + SerpAPI Google snippets, all treated as untrusted evidence) and approves a
    BUY only when the fall is explained, appears resolved, and the profit / growth
    / quarterly-improvement checks pass. Needs a `SERPAPI_API_KEY`; degrades
    gracefully (skips the AI step) when the SDK or SerpAPI is unavailable.
  - **Technical Analysis (AI)** — Hemant Super 45 ∪ Good 45 stocks with an
    AI-confirmed bullish setup: major support, breakout-confirmed classical
    pattern, confirmed double bottom, bullish Fair Value Gap retest, or bullish
    order-block tap. A cheap deterministic gate prefilters candidates, then a
    **Claude Agent SDK** agent confirms with level, pattern, and structure tools.
- **Per-stock Check Fundamentals AI agent** — see the
  [dedicated section below](#check-fundamentals-agent). One click on a
  shortlisted row runs a Claude Agent SDK agent that scrapes screener.in (peer
  table via HTMX, recent announcements, the latest concall transcript via
  `pdfplumber`) and returns a structured verdict with a 0–10 rating, a
  Valuation observation comparing current vs median P/E, and a three-part
  forward outlook.
- **Automatic data prefetch** — running `python app.py` first downloads the
  stock universes and ~10 years of daily candles, *then* opens the UI, so the
  app never blocks on downloads.
- **Reusable scanner universes** — built-in universe keys include `nifty_100`,
  `nifty_500`, `fno`, `hemant_super_45`, `hemant_good_45`,
  `hemant_good_200`, and the composites `hemant_super_good_union`
  (Hemant Super 45 ∪ Good 45) and `hemant_super_good_200_union`
  (Hemant Super 45 ∪ Good 45 ∪ Good 200), both deduped.
- **Interactive TradingView Lightweight Charts** — click any shortlisted stock
  to see a candlestick chart (with a drag-to-scale price axis) showing the
  screener's own indicator overlaid (Heikin Ashi candles for HA-based screeners;
  a dedicated oscillator panel for Stochastic).
- **Library-backed indicators** — indicators run through `TA-Lib` / `pandas_ta`
  when installed, and fall back to pure-pandas implementations otherwise.
- **Local Parquet cache** — candles are cached on disk; subsequent runs only
  fetch the days that are missing.
- **Tested** — a `pytest` suite covers the indicators, data loader, universe
  builder, screener registry, and the screeners themselves.

---

## How it works

```
python app.py
   │
   ├─ 1. Prefetch (plain Python, before the UI)
   │     • refresh the universe CSVs (NIFTY 100 / 500 / F&O / Hemant lists)
   │     • download ~10 years of daily candles for every mapped stock
   │
   └─ 2. Launch the Streamlit UI
         • pick a screener, press "Run screener"
         • browse the shortlist, click a row to open its chart
```

The prefetch is what makes the app feel instant once it opens — all the slow
network work happens up front in the terminal.

---

## Requirements

- **Python 3.11+**
- The core packages in [`requirements.txt`](requirements.txt), installed with
  the verified direct pins in [`constraints.txt`](constraints.txt):
  `pip install -r requirements.txt -c constraints.txt`
- A **DhanHQ account** with API access — needed to download candle data.
- Optional indicator accelerators in
  [`requirements-optional.txt`](requirements-optional.txt). `TA-Lib` needs its
  native C library installed first (see [ta-lib.org](https://ta-lib.org/)).
  If `TA-Lib` or `pandas_ta` is missing, the app automatically falls back to
  pure-pandas indicator maths; it just runs a little slower.

---

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/DoRmAmMu1997/Streamlit-Scanner-App.git
   cd Streamlit-Scanner-App
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt -c constraints.txt
   ```

   Optional, only after installing any native prerequisites you need:

   ```bash
   pip install -r requirements-optional.txt
   ```

3. **Add your DhanHQ credentials**

   Copy the template and fill in your details:

   ```bash
   cp Dependencies/.env.example Dependencies/.env
   ```

   Open `Dependencies/.env` and set `DHAN_CLIENT_CODE`, `DHAN_API_KEY`, and
   `DHAN_API_SECRET` (from web.dhan.co → My Profile → DhanHQ Trading APIs).
   Leave `DHAN_ACCESS_TOKEN` blank for now.

4. **Generate the access token** (one-time, valid 12 months)

   ```bash
   python Dependencies/dhan_token_setup.py
   ```

   This walks you through the DhanHQ OAuth login and writes
   `DHAN_ACCESS_TOKEN` back into `Dependencies/.env` for you.

5. **(Optional) Enable the Check Fundamentals agent** — it runs on the
   [Claude Agent SDK](https://docs.claude.com/en/api/agent-sdk/overview) using
   your Claude subscription (Pro/Max), not an API key:

   ```bash
   pip install claude-agent-sdk        # already in requirements.txt
   ```

   Then sign in once with the bundled Claude CLI (uses your Claude plan), and
   make sure `ANTHROPIC_API_KEY` is **not** set in your environment — if it is,
   the SDK bills your API account instead of your plan's monthly Agent SDK
   credit. Optionally override the model in `Dependencies/.env`:

   ```env
   CLAUDE_AGENT_MODEL=claude-sonnet-4-6
   ```

   The **67 Ka Funda (AI)** screener additionally needs a
   [SerpAPI](https://serpapi.com/) key for its Google web research — add it to
   `Dependencies/.env`:

   ```env
   SERPAPI_API_KEY=your-serpapi-key
   ```

   Most screeners run fine without any of this; only the Check Fundamentals
   panel, the Technical Analysis (AI) confirmation step, and the 67 Ka Funda (AI)
   verifier need it.

> `Dependencies/.env` is git-ignored — your credentials never leave your machine.

---

## Running the app

```bash
python app.py
```

This downloads the data first, then opens the Streamlit app in your browser.

> **First run is slow.** It backfills ~10 years of candles for ~500 stocks.
> Every later run only fetches the days added since you last ran it, so it is
> fast.

You can also start the UI directly with `streamlit run app.py` — but then it
uses whatever data is already cached locally (no prefetch).

---

## Project structure

```
Streamlit Scanner App/
├── app.py                       # Streamlit entry point + CLI prefetch
├── requirements.txt
├── requirements-optional.txt    # Optional TA-Lib/pandas_ta accelerators
├── requirements-dev.txt         # Local verification tools
├── constraints.txt              # Verified direct dependency pins
├── backend/                     # Data + infrastructure (no strategy logic)
│   ├── config.py                # Paths, credentials, tuning knobs
│   ├── dhan_client.py           # DhanHQ API wrapper
│   ├── daily_data_loader.py     # Candle fetching + Parquet cache
│   ├── universe_builder.py      # Builds the stock-universe CSVs
│   ├── universe_loader.py       # Reads the universe CSVs
│   ├── screener_registry.py     # Discovers + validates screeners
│   ├── scanner_base.py          # BaseScanner ABC every screener subclasses
│   ├── indicators.py            # Indicators (TA-Lib/pandas_ta + fallbacks)
│   ├── url_safety.py            # Shared guardrails for server-side fetches
│   ├── charts.py                # Lightweight Charts chart-spec builders
│   ├── fundamentals/            # Check Fundamentals subsystem
│   │   ├── screener_in_client.py# requests + BS4 scraper (peers via HTMX,
│   │   │                        # announcements, concall metadata)
│   │   ├── pdf_reader.py        # PDF download + text extraction
│   │   │                        # (pdfplumber → pypdf fallback)
│   │   ├── fundamentals_cache.py# On-disk JSON cache (data + verdict)
│   │   └── fundamental_agent.py # Claude Agent SDK agent + Pydantic schemas
│   ├── technical/               # Technical Analysis (AI) subsystem
│   │   └── technical_agent.py  # Claude Agent SDK agent + TechnicalVerdict
│   └── sixty_seven/             # 67 Ka Funda (AI) subsystem
│       ├── shortlister.py      # Deterministic 67% drawdown gate
│       ├── search_client.py    # SerpAPI Google search client
│       └── agent.py            # Claude Agent SDK verifier + Pydantic schemas
├── screeners/                   # One file per screener (the strategy logic)
│   ├── heikin_ashi_supertrend.py
│   ├── bollinger_band_reversal.py
│   ├── bollinger_lower_band.py
│   ├── envelope.py
│   ├── envelope_knoxville_buy.py
│   ├── stochastic_swing.py
│   ├── week52_low_ceyhun.py
│   ├── green_candles_20pct_up.py
│   ├── technical_analysis.py    # AI screener: pivot gate + technical agent
│   └── sixty_seven_ka_funda.py  # AI screener: 67% drawdown gate + verifier
├── Dependencies/
│   ├── .env.example             # Credential template (copy to .env)
│   └── dhan_token_setup.py      # One-time OAuth token helper
├── data/                        # Generated at runtime (git-ignored)
│   ├── cache/daily/             # Cached candles (Parquet)
│   ├── cache/fundamentals/      # Cached screener.in data + agent verdicts
│   │   └── pdfs/                # Downloaded concall transcripts + .txt
│   └── universes/               # Universe CSVs, including tracked Hemant lists
└── tests/                       # pytest suite
```

The boundary is deliberate: **strategy logic lives in `screeners/`**, and
**data/broker plumbing lives in `backend/`**.

---

## Check Fundamentals agent

Below the chart for any shortlisted stock, the UI shows a **Check
Fundamentals** button. Click it and a Claude Agent SDK agent (running on your
Claude subscription) scrapes the stock's
[screener.in](https://www.screener.in/) page and returns a structured verdict.

### Two modes

The agent runs in one of two modes depending on which universe the selected
stock belongs to:

- **Criteria mode** — when the symbol is in **Hemant Super 45 ∪ Nifty 100**.
  The agent evaluates **nine** user-defined criteria: the seven universal ones
  below **plus** business age ≥ 15 years and market leader by both market cap
  and profit.
- **Universal mode** — for any other shortlisted stock. The agent evaluates the
  **seven universal criteria** (Net Debt/Equity < 0.2; ROCE > 12% / 10% for
  banks; Sales+Profits+EPS near all-time highs; latest Net Profit > ₹200 Cr;
  future growth prospects; public holding lower than promoter, FII, and DII;
  promoter pledge < 5%), skipping only business age and market leader (which
  need curated peer/longevity context).

Both modes also add 4–8 additional fundamental observations of the agent's own
choosing (margins, capital allocation, governance, moat, valuation vs peers, …)
and produce the same 0–10 rating, forward outlook, and summary.

The mode is determined automatically from the row's symbol; the UI shows a
caption above the button that makes the active mode clear.

### What the verdict contains

- **Rating** — a holistic 0–10 score based on the agent's weighted judgment
  (NOT a count of passed criteria).
- **Criteria breakdown** — one row per criterion (nine in criteria mode, seven
  in universal mode) with the measured value, threshold, and reasoning.
- **Additional observations** — 4–8 agent-chosen observations grouped by
  positive/negative/neutral sentiment. One of these is always a **Valuation**
  observation that explicitly compares current P/E to the stock's own median
  P/E when available, falling back to industry P/E otherwise.
- **Forward outlook** — three-part structured outlook covering the next 1–4
  quarters:
  - *Conclusion from Announcements* — what the recent corporate announcements
    on screener.in signal.
  - *Conclusion from the latest Concall* — what the most recent quarterly
    concall transcript revealed (empty when the agent did not need to read
    the transcript).
  - *Overall summary* — the integrated forward view.
- **Summary** — a 3–6 sentence plain-English explanation of the rating.

### Tools the agent has access to

- `fetch_company_data(symbol)` — scrapes the screener.in company page,
  including the HTMX-loaded peer comparison table and the Documents card
  (Announcements + Concalls metadata).
- `read_recent_concall_transcript(symbol)` — downloads + extracts the text of
  the most recent quarterly concall transcript PDF via
  [`pdfplumber`](https://github.com/jsvine/pdfplumber). Called only when the
  agent needs management commentary for its forward outlook.

### Caching

Two on-disk caches under `data/cache/fundamentals/` keep repeated clicks free:

- **Data cache** — one JSON file per stock with a 30-day TTL (configurable via
  `SCANNER_FUNDAMENTALS_TTL_DAYS`).
- **Verdict cache** — keyed by `(symbol, model, mode, data_fetch_date)`. The
  same stock evaluated in criteria mode and universal mode gets two
  distinct cache entries.

The verdict cache is also resilient to schema changes — pre-Job-6 verdicts
that had `forward_outlook` as a plain string are automatically migrated into
the new three-part shape on load (the legacy string becomes the
`overall_summary` subfield).

### Cost ballpark (Claude Sonnet, billed to your plan)

Usage draws on your Claude plan's monthly **Agent SDK credit** (Pro $20 /
Max 5× $100 / Max 20× $200) rather than per-token API billing:

- Typical criteria-mode check, no transcript read: ~$0.02 of credit.
- Criteria-mode check that also reads the concall transcript: ~$0.08.

Both are one-shot prices — the verdict cache covers subsequent clicks at zero
cost until either the underlying data or the model changes. When the monthly
credit is exhausted, the agent pauses until it refreshes (or falls back to
standard API rates if you've enabled API billing).

## Adding your own screener

Create a new file in `screeners/`, for example `screeners/my_screener.py`,
containing a subclass of [`BaseScanner`](backend/scanner_base.py):

```python
from backend.scanner_base import BaseScanner

class MyScanner(BaseScanner):
    SCREENER = {
        "key": "my_screener",
        "name": "My Screener",
        "description": "What this scans for in one sentence.",
        "universe": "hemant_super_45",   # or any other universe key
        "timeframe": "daily",
        "lookback_days": 200,
        "default_params": {"period": 20},
    }
    EXTRA_RESULT_COLUMNS = ["my_indicator_value"]  # added to the common schema

    def compute_signal(self, symbol, candles, params):
        frame = self.prepare_candles(candles)
        period = self.coerce_param(params, "period", int)
        # ... your strategy logic ...
        return None  # or a dict matching the common + extra columns

    def build_chart(self, candles, params):  # optional
        ...
```

The app discovers the class automatically on the next start — no other file
needs to change. `BaseScanner` provides the common helpers (`prepare_candles`,
`coerce_param`, `empty_result`, a template `run` that handles per-symbol
exception capture), plus enforces a common result-schema prefix
(`symbol, rating, signal_date, close, reason`) that every screener returns.

Use `screeners/envelope.py` as the simplest template, or
`screeners/envelope_knoxville_buy.py` for a more involved example with
multiple indicators and pivot detection.

For backward compatibility the registry also accepts the legacy
module-level contract (`SCREENER` dict + `run(...)` function) if you prefer
to write a screener as plain module functions.

Supported universe keys are `nifty_100`, `nifty_500`, `fno`,
`hemant_super_45`, `hemant_good_45`, `hemant_good_200`, and the composite
`hemant_super_good_union` (the deduped union of Hemant Super 45 and Good 45).
The Hemant lists live in `data/universes/` alongside the other universe CSVs
and are mapped to Dhan cash-equity IDs when universe files are refreshed; the
union is assembled from those same source lists at refresh time.

---

## Tests And Security Checks

For normal app use, install only the runtime dependencies:

```bash
pip install -r requirements.txt -c constraints.txt
```

For development or PR review, install the verification tools too:

```bash
pip install -r requirements-dev.txt -c constraints.txt
```

Run the full local verification set before publishing changes:

```bash
python -m pytest -q
python -m compileall -q app.py backend screeners tests
python -m ruff check app.py backend screeners Dependencies tests
python -m bandit -r app.py backend screeners Dependencies -q
python -m pip_audit -r requirements.txt
```

Beginner note: `requirements-optional.txt` is intentionally separate. Those
packages speed up some indicators when their native prerequisites are already
available, but the app falls back to pure-pandas math without them.

---

## License

Released under the [MIT License](LICENSE).

---

## Changes Claude did

This section documents the **Technical Analysis (AI) screener expansion** added in
this branch. The goal was to make the screener's Claude agent a far more
proficient technical analyst: able to judge *which* support/resistance is
relevant, recognise modern price-action concepts, reason with market structure
and a higher timeframe, and call **tools** for deterministic analysis instead of
eyeballing a candle dump.

### What's new

- **Level relevance scoring** — answers "which support/resistance is relevant and
  which is not". `backend/indicators.py:rank_levels` scores every major level
  0–1 by blending five signals: touches, recency (exponential decay), proximity
  to price, volume at the level, and reaction strength — plus `last_touch_bars_ago`
  and a `flipped` (polarity-change) flag.
- **New price-action detectors** (`backend/technical/patterns.py`, pure pandas,
  deterministic): **Fair Value Gaps** (3-candle imbalances, with filled/unfilled
  tracking), **Double Top / Double Bottom** (with neckline-breakout confirmation),
  **Order Blocks** (demand/supply candle before a structure break, with
  mitigation tracking), and **Market structure** (swing trend + Break of Structure
  / Change of Character).
- **Weekly multi-timeframe** — `backend/indicators.py:resample_to_weekly`
  aggregates the cached daily candles into weekly candles (no new data source), so
  the agent gets higher-timeframe trend/level context and reports `htf_alignment`.
- **Externalized agent knowledge** — the agent's "skill" now lives in
  `backend/technical/knowledge.py` as composable prose (concept definitions,
  decision rules, tool-usage guide) composed by `build_system_prompt()`, instead
  of one inline string.
- **Agent MCP tools** — `backend/technical/tools.py` exposes three in-process
  tools the agent calls on demand: `level_map` (relevance-scored S/R, daily +
  weekly), `price_patterns` (FVGs, double tops/bottoms, order blocks), and
  `market_structure` (trend + BOS/CHoCH on both timeframes). Wired exactly like
  the fundamentals agent (`create_sdk_mcp_server`, `allowed_tools`,
  `permission_mode="dontAsk"`). The per-day verdict cache stays correct because
  the tool outputs are deterministic functions of the candles + settings, which
  are folded into the cache hash.
- **Richer verdict** — `TechnicalVerdict` gained `trend`, `htf_alignment`,
  `relevant_levels` (the levels the agent is keying on, each high/medium/low), and
  `caution` (bearish/structure warnings). New BUY-eligible patterns:
  `double_bottom`, `fair_value_gap`, `order_block`.
- **New first-class BUY triggers** — the cheap gate in
  `screeners/technical_analysis.py` now also admits a freshly-confirmed double
  bottom, a retested unfilled bullish Fair Value Gap, or a tap of a bullish order
  block (in addition to at-support and resistance breakouts). Graceful gate-only
  degradation (when the Claude Agent SDK is unavailable) covers the new triggers.
- **Chart visualizations** — the screener chart now draws relevance-weighted
  support/resistance (thicker line = more relevant), dotted FVG / order-block
  zones, and double-pattern necklines (`backend/charts.py:add_levels_overlay`,
  `add_zone_overlays`, `add_neckline_overlay`).

### Files

| Area | Files |
|---|---|
| New | `backend/technical/patterns.py`, `backend/technical/knowledge.py`, `backend/technical/tools.py`, `tests/test_patterns.py`, `tests/test_technical_tools.py` |
| Changed | `backend/indicators.py`, `backend/technical/technical_agent.py`, `screeners/technical_analysis.py`, `backend/charts.py`, and their tests |

### Verification

The full local verification set passes (`pytest` 266 passed, `compileall`,
`ruff`, `bandit`, `pip-audit`). Detectors are covered by TDD-style tests with
hand-verified synthetic fixtures. Note: a live end-to-end run of the agent
calling its tools requires the Claude Agent SDK signed in and Dhan candle data,
which a normal `python app.py` run exercises.
