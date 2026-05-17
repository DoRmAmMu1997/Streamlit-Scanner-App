# Streamlit Scanner App

A pluggable **daily-candle stock scanner** for Indian equities. It downloads
historical price data from [DhanHQ](https://dhanhq.co/), runs technical-analysis
**screeners** over a universe of stocks, and shows the shortlisted symbols in a
[Streamlit](https://streamlit.io/) web app with interactive charts.

It is designed to be easy to extend: a "screener" is just a small Python file
dropped into the `screeners/` folder.

> **Disclaimer:** This is an educational / personal research tool. Nothing here
> is financial advice. Always do your own research before trading.

---

## Features

- **Three built-in screeners**
  - **Heikin Ashi SuperTrend** — F&O stocks where the daily Heikin Ashi close
    crosses the SuperTrend line.
  - **Bollinger Band Reversal** — F&O stocks printing a daily Bollinger Band
    rejection candle.
  - **Stochastic Swing** — NIFTY 500 stocks with a fresh Stochastic swing entry
    (a `%K`/`%D` cross out of the oversold/overbought zone, confirmed by the
    200 SMA trend and a recent 5 EMA / 200 SMA crossover).
- **Automatic data prefetch** — running `python app.py` first downloads the
  stock universes and ~10 years of daily candles, *then* opens the UI, so the
  app never blocks on downloads.
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
   │     • refresh the universe CSVs (NIFTY 100 / 500 / F&O)
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
├── app.py                  # Streamlit entry point + CLI prefetch
├── requirements.txt
├── backend/                # Data + infrastructure (no strategy logic)
│   ├── config.py           # Paths, credentials, tuning knobs
│   ├── dhan_client.py      # DhanHQ API wrapper
│   ├── daily_data_loader.py# Candle fetching + Parquet cache
│   ├── universe_builder.py # Builds the stock-universe CSVs
│   ├── universe_loader.py  # Reads the universe CSVs
│   ├── screener_registry.py# Discovers + validates screeners
│   ├── indicators.py       # Indicators (TA-Lib/pandas_ta + fallbacks)
│   └── charts.py           # Lightweight Charts chart-spec builders
├── screeners/              # One file per screener (the strategy logic)
│   ├── heikin_ashi_supertrend.py
│   ├── bollinger_band_reversal.py
│   └── stochastic_swing.py
├── Dependencies/
│   ├── .env.example        # Credential template (copy to .env)
│   └── dhan_token_setup.py # One-time OAuth token helper
├── data/                   # Generated at runtime (git-ignored)
│   ├── cache/daily/         # Cached candles (Parquet)
│   └── universes/           # Generated universe CSVs
└── tests/                  # pytest suite
```

The boundary is deliberate: **strategy logic lives in `screeners/`**, and
**data/broker plumbing lives in `backend/`**.

---

## Adding your own screener

Create a new file in `screeners/`, for example `screeners/my_screener.py`, that
exposes:

- `SCREENER` — a metadata dict (`key`, `name`, `description`, `universe`,
  `timeframe`, `lookback_days`, `default_params`).
- `run(universe_df, data_loader, params) -> pandas.DataFrame` — returns the
  shortlist table.
- `build_chart(candles, params)` *(optional)* — returns a chart-spec dict for
  the per-stock Lightweight Charts chart.

The app discovers it automatically on the next start — no other file needs to
change. Use `screeners/heikin_ashi_supertrend.py` as a template.

---

## Tests

```bash
python -m pytest -q
```

---

## License

Released under the [MIT License](LICENSE).
