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
seven user-defined criteria for the Hemant Super 45 / Nifty 100 universe, a
0–10 holistic rating, peer / margin / governance observations, and a three-part
forward outlook (announcements signal + concall transcript signal + integrated
view).

> **Disclaimer:** This is an educational / personal research tool. Nothing here
> is financial advice. Always do your own research before trading.

---

## Features

- **Six built-in screeners**, all built on a common `BaseScanner` abstract
  base class so adding new ones is a single-file change.
  - **Heikin Ashi SuperTrend** — F&O stocks where the daily Heikin Ashi close
    crosses the SuperTrend line.
  - **Bollinger Band Reversal** — F&O stocks printing a daily Bollinger Band
    rejection candle.
  - **Bollinger Knoxville Buy** — Hemant Super 45 stocks near the lower
    Bollinger Band(200, 2.5) with a recent bullish Knoxville Divergence.
  - **Stochastic Swing** — NIFTY 500 stocks with a fresh Stochastic swing entry
    (a `%K`/`%D` cross out of the oversold/overbought zone, confirmed by the
    200 SMA trend and a recent 5 EMA / 200 SMA crossover).
  - **52 Week High/Low (Ceyhun)** — Hemant Super 45 stocks whose close came
    within a tolerance (default 2%) of the trailing 252-day low on any of the
    last 10 trading days.
  - **14% Below 200 EMA** — Hemant Super 45 stocks trading at least 14% below
    the 200-period exponential moving average.
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
  `nifty_500`, `fno`, `hemant_super_45`, `hemant_good_45`, and
  `hemant_good_200`.
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
- The packages in [`requirements.txt`](requirements.txt) (`pip install -r requirements.txt`)
- A **DhanHQ account** with API access — needed to download candle data.
- **TA-Lib** (optional): the `TA-Lib` Python package needs its underlying C
  library installed first (see [ta-lib.org](https://ta-lib.org/)). If it (or
  `pandas_ta`) is missing, the app automatically falls back to pure-pandas
  indicator maths — it just runs a little slower.

---

## Setup

1. **Clone the repository**

   ```bash
   git clone https://github.com/DoRmAmMu1997/Streamlit-Scanner-App.git
   cd Streamlit-Scanner-App
   ```

2. **Install dependencies**

   ```bash
   pip install -r requirements.txt
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

   The screeners run fine without any of this; only the Check Fundamentals
   button needs it.

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
├── backend/                     # Data + infrastructure (no strategy logic)
│   ├── config.py                # Paths, credentials, tuning knobs
│   ├── dhan_client.py           # DhanHQ API wrapper
│   ├── daily_data_loader.py     # Candle fetching + Parquet cache
│   ├── universe_builder.py      # Builds the stock-universe CSVs
│   ├── universe_loader.py       # Reads the universe CSVs
│   ├── screener_registry.py     # Discovers + validates screeners
│   ├── scanner_base.py          # BaseScanner ABC every screener subclasses
│   ├── indicators.py            # Indicators (TA-Lib/pandas_ta + fallbacks)
│   ├── charts.py                # Lightweight Charts chart-spec builders
│   └── fundamentals/            # Check Fundamentals subsystem
│       ├── screener_in_client.py# requests + BS4 scraper (peers via HTMX,
│       │                        # announcements, concall metadata)
│       ├── pdf_reader.py        # PDF download + text extraction
│       │                        # (pdfplumber → pypdf fallback)
│       ├── fundamentals_cache.py# On-disk JSON cache (data + verdict)
│       └── fundamental_agent.py # Claude Agent SDK agent + Pydantic schemas
├── screeners/                   # One file per screener (the strategy logic)
│   ├── heikin_ashi_supertrend.py
│   ├── bollinger_band_reversal.py
│   ├── bollinger_knoxville_buy.py
│   ├── stochastic_swing.py
│   ├── week52_low_ceyhun.py
│   └── ema200_14percent_below.py
├── Dependencies/
│   ├── .env.example             # Credential template (copy to .env)
│   └── dhan_token_setup.py      # One-time OAuth token helper
├── data/                        # Generated at runtime (git-ignored)
│   ├── cache/daily/             # Cached candles (Parquet)
│   ├── cache/fundamentals/      # Cached screener.in data + agent verdicts
│   │   └── pdfs/                # Downloaded concall transcripts + .txt
│   └── universes/               # Universe CSVs, including tracked Hemant lists
└── tests/                       # pytest suite (152 tests)
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
  The agent evaluates seven user-defined criteria (Net Debt/Equity < 0.2,
  ROCE > 12% / 10% for banks, Sales+Profits+EPS near all-time highs,
  latest Net Profit > ₹200 Cr, future growth prospects, business age ≥ 15
  years, market leader by both market cap and profit) and also adds 4–8
  additional fundamental observations of its own choosing (margins, capital
  allocation, governance, moat, valuation vs peers, …).
- **Insights-only mode** — for any other shortlisted stock. The agent skips
  the seven-criteria checklist but still produces the same 0–10 rating,
  observations, forward outlook, and summary from the same screener.in data.

The mode is determined automatically from the row's symbol; the UI shows a
caption above the button that makes the active mode clear.

### What the verdict contains

- **Rating** — a holistic 0–10 score based on the agent's weighted judgment
  (NOT a count of passed criteria).
- **Criteria breakdown** (criteria mode only) — one row per criterion with the
  measured value, threshold, and reasoning.
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
  same stock evaluated in criteria mode and insights-only mode gets two
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

---

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

Use `screeners/ema200_14percent_below.py` as the simplest template, or
`screeners/bollinger_knoxville_buy.py` for a more involved example with
multiple indicators and pivot detection.

For backward compatibility the registry also accepts the legacy
module-level contract (`SCREENER` dict + `run(...)` function) if you prefer
to write a screener as plain module functions.

Supported universe keys are `nifty_100`, `nifty_500`, `fno`,
`hemant_super_45`, `hemant_good_45`, and `hemant_good_200`. The Hemant lists
live in `data/universes/` alongside the other universe CSVs and are mapped to
Dhan cash-equity IDs when universe files are refreshed.

---

## Tests

```bash
python -m pytest -q
```

---

## License

Released under the [MIT License](LICENSE).
