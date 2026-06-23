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

Access is gated behind **Google sign-in with an email allowlist**, and the app
ships with a **scan-history persistence foundation** (SQLAlchemy + Alembic; SQLite
by default or Postgres) that is ready to record every run for later replay and audit.

> **Disclaimer:** This is an educational / personal research tool. Nothing here
> is financial advice. Always do your own research before trading.

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
- **Hardened AI screeners** — all three Claude agents (Check Fundamentals,
  Technical Analysis, 67 Ka Funda) treat scraped/search text as untrusted. A
  shared quarantine (**TEST-003**, `backend/security/prompt_injection.py`) scans
  external evidence (Screener.in scrapes, SerpAPI snippets, concall transcripts)
  for model-directed instructions and **fails closed before the model sees it**,
  and every AI verdict is parsed against a strict Pydantic schema with a bounded
  retry budget — malformed output is rejected, never persisted (**AI-004**,
  `SCANNER_AI_MAX_ATTEMPTS`).
- **Candle data-quality checks (DATA-001)** — every OHLCV frame is validated at
  the loader boundary before any screener runs. Structurally impossible candles
  (high < low, NaN/inf, duplicate dates, negative volume) are **quarantined** and
  downgrade the run to `PARTIAL`/`FAILED`, while stale or gappy data is recorded
  as a warning. Findings persist in a per-run `data_quality_json` receipt and are
  summarized on the Admin health page.
- **Automatic data prefetch** — running `python app.py` first downloads the
  stock universes and ~10 years of daily candles, *then* opens the UI, so the
  app never blocks on downloads. Each successful prefetch keeps only the latest
  Dhan instrument-master snapshot in `Dependencies/`.
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
- **Authentication & access control** — every page sits behind a Google SSO
  (OIDC) sign-in gate (`backend/auth/`). An environment-driven **email allowlist**
  (`ALLOWED_EMAILS`) restricts who may use the app, with **admin identification**
  (`ADMIN_EMAILS`); in production the gate fails closed when SSO config or the
  allowlist is missing.
- **Scan-run persistence + history page** — every scan (from the UI or the
  headless daily job) is recorded into a SQLAlchemy `scan_runs` / `scan_results`
  schema (`backend/storage/`) with a local SQLite default (`data/scanner.db`) or
  Postgres via `DATABASE_URL`, managed by **Alembic** migrations and a small
  repository API. A built-in **Scan history** view lists recent runs (status,
  started/finished timestamps, symbols scanned, shortlisted count, who triggered
  it, error state) with screener/universe/status/date/trigger/symbol filters and
  click-through to each run's persisted results. A read-only **Scan comparison**
  view compares the latest finalized shortlist against the immediately previous
  finalized shortlist for each screener/universe pair, with new, repeated,
  dropped, improved-score, degraded-score, and CSV export sections. Historical
  validation stores
  per-signal forward returns and exposes backend aggregate metrics by screener,
  universe, and horizon, surfaced in a read-only **Validation / Signal
  Performance** dashboard (filters, summary table, return distribution, win
  rate by horizon, benchmark-relative rows, monthly signal counts, sector
  concentration with an `Unknown` fallback, best/worst signals, and CSV export).
  Benchmark-relative (excess) returns compare each signal against its universe's
  index (NIFTY 50 / 100 / 500) using verified Dhan `IDX_I` instrument IDs
  configured in `config/benchmarks.yaml` (VALID-002B); an unconfigured benchmark
  stays null rather than guessing. Operators can fill pending rows with
  `python -m backend.jobs.compute_forward_returns --limit 500`.
- **Tested** — a `pytest` suite covers the indicators, data loader, universe
  builder, screener registry, the screeners themselves, the auth gate, the
  persistence layer, forward-return validation metrics, benchmark-index
  resolution, candle data-quality validation, the AI prompt-injection quarantine
  corpus, structured AI-output validation, and the Docker artifacts —
  plus **golden-snapshot** tests that catch screener output drift and an Alembic
  migration drift-guard.

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

3. **Create the local scan-history database** (optional)

   By default, persisted scan runs live in `data/scanner.db`, which is
   generated locally and git-ignored. `DATA_DIR` can point the whole runtime
   data folder somewhere else, and `DATABASE_URL` can point the app at Postgres
   or another SQLAlchemy-supported database in deployed environments.

   The app and the daily scan command apply migrations automatically on
   startup, so a fresh checkout needs no manual step. Running the upgrade
   yourself is still useful to pre-provision a database or debug migrations:

   ```bash
   python -m alembic upgrade head
   ```

4. **Review runtime settings**

   The app reads runtime config through `backend.config.settings`. Local
   development has safe defaults:

   ```env
   APP_ENV=development
   LOG_LEVEL=WARNING
   AUTH_REQUIRED=false
   # DATA_DIR defaults to ./data
   # DATABASE_URL defaults to sqlite:///data/scanner.db
   ```

   Production should set these in the hosting environment, not in committed
   files:

   ```env
   APP_ENV=production
   DATA_DIR=/persistent/data
   DATABASE_URL=postgresql+psycopg://scanner:password@host:5432/scanner
   AUTH_REQUIRED=true
   ALLOWED_EMAILS=you@gmail.com
   ADMIN_EMAILS=you@gmail.com
   ```

   Production fails clearly if `DATABASE_URL`, `DATA_DIR`, Dhan credentials, or
   an authorized/admin email is missing. It also rejects
   `AUTH_REQUIRED=false`. `backend.security.redaction` masks configured
   secret-like values plus common token/API-key/password formats before text
   reaches UI errors, scan failure details, or configured logs. Redaction is a
   safety net only; do not paste real secrets into issues, screenshots, or PRs.

5. **Add your DhanHQ credentials**

   Copy the template and fill in your details:

   ```bash
   cp Dependencies/.env.example Dependencies/.env          # macOS/Linux/Git Bash
   ```

   ```powershell
   Copy-Item Dependencies\.env.example Dependencies\.env   # Windows PowerShell
   ```

   > Why is this folder called `Dependencies/`? Historical accident — it holds
   > credentials and setup helpers, not Python packages (those live in
   > `requirements*.txt`). It keeps the name because renaming would break
   > every existing local `.env` setup for zero functional gain.

   Open `Dependencies/.env` and set `DHAN_CLIENT_ID`, `DHAN_API_KEY`, and
   `DHAN_API_SECRET` (from web.dhan.co → My Profile → DhanHQ Trading APIs).
   Leave `DHAN_ACCESS_TOKEN` blank for now. Existing `.env` files that still
   use the legacy `DHAN_CLIENT_CODE` name continue to work.

6. **Generate the access token** (one-time, valid 12 months)

   ```bash
   python Dependencies/dhan_token_setup.py
   ```

   This walks you through the DhanHQ OAuth login and writes
   `DHAN_ACCESS_TOKEN` back into `Dependencies/.env` for you.

7. **Configure Google SSO for the Streamlit app**

   Create a Google OAuth/OIDC client with this local redirect URI:

   ```text
   http://localhost:8501/oauth2callback
   ```

   For a deployed app, add the same callback path on the deployed base URL:

   ```text
   https://your-app.example.com/oauth2callback
   ```

   Then copy the Streamlit secrets template and fill in the Google client
   values:

   ```bash
   cp .streamlit/secrets.example.toml .streamlit/secrets.toml
   ```

   Required keys:

   ```toml
   [auth]
   redirect_uri = "http://localhost:8501/oauth2callback"
   cookie_secret = "a-long-random-secret"

   [auth.google]
   client_id = "your-google-oauth-client-id"
   client_secret = "your-google-oauth-client-secret"
   server_metadata_url = "https://accounts.google.com/.well-known/openid-configuration"
   ```

   Set `APP_ENV=production` and `AUTH_REQUIRED=true` in the deployment
   environment for production. If SSO config is missing in production, the app
   fails closed before loading any scanner controls. `SCANNER_ENV` is still
   accepted as a legacy alias for older local files.

   **Restrict who can use the app (email allowlist).** Once Google SSO works,
   limit access by email in `Dependencies/.env`:

   ```env
   # Comma-separated; case and surrounding spaces don't matter.
   ALLOWED_EMAILS=you@gmail.com, teammate@gmail.com
   ADMIN_EMAILS=you@gmail.com
   ```

   - `ADMIN_EMAILS` are always allowed and are flagged as admins (reserved for
     future admin-only features).
   - If `ALLOWED_EMAILS` is **empty**, development permits any signed-in Google
     user when auth is enabled, but production (`APP_ENV=production`) requires
     either `ALLOWED_EMAILS` or `ADMIN_EMAILS`. A signed-in user who is not
     allowed sees an "unauthorized" message instead of the scanner.

8. **(Optional) Enable the Check Fundamentals agent** — it runs on the
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

> `Dependencies/.env` and `.streamlit/secrets.toml` are git-ignored — your
> credentials never leave your machine.

---

## Running the app

```bash
python app.py
```

This downloads the data first, then opens the Streamlit app in your browser.
Local development skips Google SSO unless `AUTH_REQUIRED=true` is set. When
auth is required, only allow-listed or admin emails (see step 7) may proceed
past sign-in before scanner controls, results, charts, or CSV downloads load.

> **First run is slow** — expect roughly 10–30 minutes depending on your
> connection: it backfills ~10 years of candles for ~500 stocks at a polite
> request pace. Setting `SCANNER_DHAN_FETCH_WORKERS=4` in `Dependencies/.env`
> overlaps download latency with disk writes **without** increasing the
> request rate Dhan sees (see [docs/operations.md](docs/operations.md)).
> Every later run only fetches the days added since you last ran it, so it is
> fast.

You can also start the UI directly with `streamlit run app.py` — but then it
uses whatever data is already cached locally (no prefetch).

---

## Running with Docker

The recommended local production-like path is Docker Compose: it starts the
Streamlit app plus a private Postgres database, uses named volumes for durable
runtime state, and keeps secrets outside the image.

```bash
cp .env.example .env
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
# Edit .env and .streamlit/secrets.toml before running with real credentials.
docker compose up --build
```

Open <http://localhost:8501>. Compose publishes only the UI with
`-p 8501:8501` behavior via `SCANNER_UI_PORT=8501`; Postgres has no host port
and is reachable only inside the Compose network as `postgres:5432`.

Compose uses two named volumes:

- `scanner-data` mounted at `/data` for candles, caches, SQLite fallback files,
  and other app-generated state.
- `postgres-data` mounted at `/var/lib/postgresql/data` for the local Postgres
  cluster.

Stop the stack without deleting data:

```bash
docker compose down
```

Reset both named volumes when you deliberately want a clean local production
environment:

```bash
docker compose down --volumes
```

Run the daily scan job against the same Compose database and `/data` volume
without adding a long-lived scheduler service:

```bash
docker compose run --rm scanner-ui python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml
```

To send ALERT-001 notifications from that Compose-run job, fill the optional
notification variables in the root `.env` before running it: `APP_URL`,
`TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID`, and/or `SMTP_HOST`, `SMTP_USER`,
`SMTP_PASSWORD`, `ALERT_EMAIL_TO`. Leaving them blank keeps alerts disabled and
the scan still runs normally.

Use the single-container commands below when you only want to build or smoke-test
the image without starting Postgres.

Build the deployment image from the repository root:

```bash
docker build -t streamlit-scanner-app .
```

For a local container smoke test, keep auth disabled and persist generated data
in a named Docker volume:

```bash
docker run --rm \
  -p 8501:8501 \
  -e APP_ENV=development \
  -e AUTH_REQUIRED=false \
  -e DATA_DIR=/data \
  -v streamlit-scanner-data:/data \
  streamlit-scanner-app
```

Open <http://localhost:8501>. The image starts with `streamlit run app.py`
instead of `python app.py`, so it serves the UI directly and does not run the
local prefetch/relaunch wrapper at container boot.

Production containers default to fail-closed settings (`APP_ENV=production`,
`AUTH_REQUIRED=true`, `DATA_DIR=/data`). Supply the same runtime environment the
non-container app expects, mount a persistent `/data` volume, and provide
Streamlit's Google OIDC secrets file. The inline `-e` values below are
placeholders for a manual run; prefer your host's managed secret/environment
injection for real deployments:

```bash
docker run -d --name streamlit-scanner-app \
  -p 8501:8501 \
  -e APP_ENV=production \
  -e AUTH_REQUIRED=true \
  -e DATA_DIR=/data \
  -e DATABASE_URL=postgresql+psycopg://scanner:<password>@db-host:5432/scanner \
  -e DHAN_CLIENT_ID=your-dhan-client-id \
  -e DHAN_ACCESS_TOKEN=your-dhan-access-token \
  -e ALLOWED_EMAILS=you@gmail.com \
  -e ADMIN_EMAILS=you@gmail.com \
  -e LOG_FORMAT=json \
  -v streamlit-scanner-data:/data \
  -v /absolute/path/secrets.toml:/app/.streamlit/secrets.toml:ro \
  streamlit-scanner-app
```

`Dockerfile` exposes port `8501` and includes a health check against
`/_stcore/health`. `.dockerignore` keeps local secrets (`Dependencies/.env`,
`.streamlit/secrets.toml`) and generated cache/database files out of the build
context. See [docs/operations.md](docs/operations.md#docker--container-deployment)
for container runbook details and daily-job commands.

---

## Running the daily scan job

JOB-001 adds a headless command for schedulers, terminals, and hosting
platforms that need to run scans without opening Streamlit:

```bash
python -m backend.jobs.run_daily_scan
```

By default it runs the deterministic daily set:
`bollinger_band_reversal`, `heikin_ashi_supertrend`, and
`envelope_knoxville_buy`. Each screener uses the universe declared in its
registry metadata, so F&O screeners run on `fno` and the Envelope + Knoxville
screener runs on `hemant_super_45`.

The command expects the normal runtime setup to exist first: keep the
universe CSVs under `DATA_DIR/universes` and configure Dhan credentials so the
daily data loader can fetch/cache candles. Scan-history tables are created
automatically — the command applies Alembic migrations on startup before any
screener runs. Local scan history defaults to
`data/scanner.db`; deployments can point `DATABASE_URL` at Postgres or another
SQLAlchemy-supported database.

To run a custom set, repeat `--screener`:

```bash
python -m backend.jobs.run_daily_scan --screener technical_analysis --screener envelope
```

For a fixed, named schedule that cron or a hosting platform can run without long
flag lists, point the command at a YAML config (JOB-002):

```bash
python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml
```

`config/daily_scans.yaml` is the committed Render/default schedule used by the
Blueprint cron. It contains no secrets, enables the deterministic daily set, and
keeps AI-heavy jobs disabled by default. Copy/edit it for your deployment, or
point `--config` at another repo-available file. Keep
`config/daily_scans.example.yaml` as the documented template when you want more
inline guidance. Each entry under `daily_scans` is one named scan batch:

```yaml
daily_scans:
  - name: Bollinger Band Reversal (daily)
    screener_key: bollinger_band_reversal
    enabled: true

  - name: Envelope Knoxville Buy (daily)
    screener_key: envelope_knoxville_buy
    enabled: true
    universe_key: hemant_super_45   # optional; defaults to the screener's universe
    params:                         # optional; merged over the screener defaults
      percent: 14.0

  - name: 67 Ka Funda (AI)
    screener_key: sixty_seven_ka_funda
    enabled: false                  # AI-heavy: opt in deliberately (see below)
```

Only `name` and `screener_key` are required; `enabled` defaults to `true`.
Disabled entries are skipped (and logged as skipped). `--config` and `--screener`
cannot be combined. A malformed YAML file, an unknown `screener_key` or
`universe_key`, or a config with no enabled entries each exit non-zero so a
scheduler notices the problem.

> **AI-heavy jobs are opt-in.** The `sixty_seven_ka_funda` and
> `technical_analysis` screeners call the Claude Agent SDK (and SerpAPI), so they
> cost API quota and depend on optional external services. They ship **disabled**
> in the committed Render/default schedule and the example config; enable them
> deliberately and consider lowering `max_ai_candidates` to cap per-run cost.

Exit code `0` means every selected scan persisted history and finished
`success` or `partial`. Exit code `1` means a fatal problem occurred, such as an
unknown screener key, missing setup, a failed screener, or a scan whose results
could not be written to `scan_runs` / `scan_results`. Operator summaries include
status and run ids, but not raw secrets.

ALERT-001 notifications are opt-in for this command. Set `APP_URL` for the
message link, then configure Telegram (`TELEGRAM_BOT_TOKEN`,
`TELEGRAM_CHAT_ID`) and/or email (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_PASSWORD`, optional `SMTP_FROM`, `ALERT_EMAIL_TO`) in
`Dependencies/.env`, the root `.env` for Docker Compose, or your hosting
dashboard. A notification failure is logged and never changes the scan exit
code.

---

## Deploying to Render

[`render.yaml`](render.yaml) is a Render **Blueprint** (DEPLOY-003) that
provisions the production stack from one file, reusing the same `Dockerfile` as
local Docker:

- a **web service** (`scanner-web`) running `streamlit run app.py` bound to
  Render's `$PORT`, with a **persistent disk** mounted at `DATA_DIR` for the
  candle cache;
- a managed **Postgres** database (`scanner-db`) auto-wired into `DATABASE_URL`
  (Render emits `postgresql://…`; the app rewrites it to the pinned
  `postgresql+psycopg://` driver at startup) with public database ingress closed
  by `ipAllowList: []`;
- a **cron job** (`scanner-daily-scan`) that refreshes universes and runs
  `python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml`,
  writing results to the shared Postgres.

Every secret is `sync: false` in the Blueprint (provided in the Render dashboard,
never committed). The cron also exposes opt-in ALERT-001 variables there
(`APP_URL`, Telegram bot/chat values, SMTP settings, and `ALERT_EMAIL_TO`) so it
can send the daily summary after the scheduled scan. Google OIDC secrets go in a
Render Secret File at
`streamlit-secrets.toml`, which Render exposes to Docker at
`/etc/secrets/streamlit-secrets.toml`. The web `dockerCommand` passes that file
through `--secrets.files` so `st.login` can read the `[auth]` tables. The cron's
`--config` target is the committed Render/default schedule, so the deployed image
contains the file Render runs; change the path only when your replacement config
is also present in the image. Full
step-by-step: [docs/operations.md → "Deploying to Render"](docs/operations.md#deploying-to-render-managed). Topology rationale: [the
deployment-runtime LLD](docs/architecture/components/deployment-runtime.md).

---

## Observability & logging

To make failures diagnosable in production (OBS-001), the app emits **named,
structured log events** through `backend/observability`. Every entrypoint — the
Streamlit UI, the `python app.py` prefetch, and the headless daily-scan job —
configures logging the same way via `configure_logging()`.

Admins also receive an **Admin health** view (OBS-002) in the app's top view
selector. The page is available only when the authenticated email is listed in
`ADMIN_EMAILS`; it repeats that admin check inside the renderer so an
auth-disabled development session or a future direct caller cannot bypass the
guard.

The health snapshot reports the latest exact `SUCCESS` and `FAILED` scan runs,
the newest persisted **candle data-quality receipt** (DATA-001 — checked/usable
symbol counts plus a capped, redacted findings sample), the newest generated
universe/cache-file refresh, cached symbol count, latest candle date, unreadable
cache files, cache/data sizes, disk free space, database query readiness, and
passive Dhan/Claude Agent SDK/SerpAPI setup status. The snapshot is cached for 60
seconds to keep normal Streamlit reruns inexpensive.

Provider readiness is intentionally **passive**: opening the page never calls
Dhan, Claude, or SerpAPI and therefore never consumes quota. “Ready” means the
required credentials are present or the local SDK is installed; the page says
when sign-in/connectivity has not been live-tested. Database health failures
show only the exception type, and persisted scan failure text passes through the
same secret redactor used elsewhere in the app.

**Events** carry searchable context such as `run_id`, `scan_name`,
`screener_key`, `universe_key`, and `symbol` whenever that context is available:

| Event | Level | Emitted when |
| --- | --- | --- |
| `daily_job_started` / `daily_job_completed` | INFO or ERROR | the headless command starts / finishes, including its aggregate exit code and outcome counts |
| `daily_job_config_loaded` | INFO | a JOB-002 YAML schedule is valid, including enabled/disabled entry counts |
| `daily_job_config_invalid` | ERROR | a schedule cannot be loaded or contains no enabled scans |
| `scan_started` / `scan_completed` | INFO | a scan starts / successfully persists (with run context, result count, and duration) |
| `scan_partial` | WARNING | a scan persists usable rows but one or more symbols fail to load or compute |
| `scan_failed` | ERROR | screener, header, or result persistence fails (safe phase and exception **type**, never the raw message) |
| `symbol_scan_failed` | WARNING | a single symbol fails to load or compute (run and screener context plus `symbol`) |
| `external_api_failed` | WARNING | a Dhan candle fetch fails for a `symbol` |
| `candle_data_quality_warning` | WARNING | a frame passes with warning-only quality findings, e.g. stale data (DATA-001; finding codes only) |
| `candle_data_quality_failed` | WARNING | a frame is quarantined for a fatal quality finding before scanning (DATA-001; finding codes only) |
| `auth_denied` | WARNING | a signed-in email is not on the allowlist (logs the email, never the allowlist) |
| `data_refresh_started` / `data_refresh_completed` | INFO or ERROR | the universe/candle prefetch starts / reaches a terminal success, skip, or failure state |
| `login_success` / `login_denied` | INFO or WARNING | a sign-in is accepted / rejected (OBS-003 audit; also persisted to `audit_logs`) |
| `manual_scan_started` | INFO | a user presses **Run screener** (OBS-003 audit; distinct from the service `scan_started`) |
| `config_changed` | INFO | an admin changes a runtime setting via the settings form (OBS-003 audit; old/new redacted) |
| `export_downloaded` | INFO | a results/history CSV is downloaded (OBS-003 audit) |
| `admin_page_accessed` | INFO | an admin opens an admin page (OBS-003 audit; once per page per session) |

The seven OBS-003 audit events are `login_success`, `login_denied`,
`manual_scan_started`, `data_refresh_started`, `config_changed`,
`export_downloaded`, and `admin_page_accessed`. They are emitted to the log
stream *and* written to a durable `audit_logs` table. See
[Audit log](#audit-log-obs-003) below.

**Plain text vs JSON.** `LOG_FORMAT` controls rendering:

- `auto` (default) — human-readable text in development, machine-readable **JSON
  in production** (`APP_ENV=production`).
- `json` / `text` — force one rendering regardless of environment (handy for
  testing JSON locally or keeping a production console readable).

```bash
# One JSON object per line, ready for a log aggregator:
LOG_FORMAT=json LOG_LEVEL=INFO python -m backend.jobs.run_daily_scan
```

```json
{"timestamp": "2026-06-10T...", "level": "INFO", "logger": "backend.scanning.service", "event": "scan_completed", "message": "scan_completed", "run_id": 42, "scan_name": "Daily Envelope", "screener_key": "envelope", "universe_key": "hemant_super_45", "status": "success", "results_count": 5, "duration_seconds": 1.23}
```

**Levels.** Routine lifecycle events log at INFO. Partial outcomes log at WARNING,
and failures log at ERROR, so degraded and failed jobs remain visible at the
default `LOG_LEVEL=WARNING`. Set `LOG_LEVEL=INFO` to see the full event stream.

**Secrets never leak.** Every rendered line (text or JSON, including exception
tracebacks and structured field values) passes through the SEC-002 redaction
filter / `redact_text`, so tokens, API keys, and database passwords are masked.

---

## Project structure

```
Streamlit Scanner App/
├── app.py                       # Streamlit entry point + CLI prefetch
├── AGENTS.md                    # Agent/contributor guide (Claude Code + Codex)
├── CLAUDE.md                    # Loads AGENTS.md for Claude Code (@import)
├── requirements.txt
├── requirements-optional.txt    # Optional TA-Lib/pandas_ta accelerators
├── requirements-dev.txt         # Local verification tools
├── constraints.txt              # Verified direct dependency pins
├── alembic.ini                  # Alembic config for scan-history migrations
├── migrations/                  # Alembic migration scripts (scan-history + audit log schema)
├── config/                      # Committed runtime config (no secrets)
│   ├── daily_scans.yaml         # Render/default daily-scan schedule (JOB-002)
│   ├── daily_scans.example.yaml # Verbose daily-scan schedule template
│   └── benchmarks.yaml          # Verified Dhan IDX_I benchmark index IDs (VALID-002B)
├── docs/                        # Project documentation
│   ├── operations.md            # Operations / runbook guide
│   ├── adding-a-screener.md     # Screener authoring walkthrough
│   └── architecture/            # HLD + per-component LLDs + 2026-06 audit register (start at README.md)
├── backend/                     # Data + infrastructure (no strategy logic)
│   ├── admin/                   # Admin runtime-config override service (OBS-003)
│   ├── audit/                   # Best-effort, secret-safe audit recorder (OBS-003)
│   ├── auth/                    # Streamlit OIDC login/session gate
│   ├── config/                  # Runtime settings package + legacy exports
│   ├── dhan_client.py           # DhanHQ API wrapper
│   ├── daily_data_loader.py     # Candle fetching + Parquet cache
│   ├── universe_builder.py      # Builds the stock-universe CSVs
│   ├── universe_loader.py       # Reads the universe CSVs
│   ├── validation/              # Forward-return calculators, services, dashboard metrics, sector + benchmark-index helpers
│   ├── screener_registry.py     # Discovers + validates screeners
│   ├── scanner_base.py          # BaseScanner ABC every screener subclasses
│   ├── jobs/                    # Headless commands: daily scans + forward-return validation batches
│   ├── indicators.py            # Indicators (TA-Lib/pandas_ta + fallbacks)
│   ├── url_safety.py            # Shared guardrails for server-side fetches
│   ├── charts.py                # Lightweight Charts chart-spec builders
│   ├── health.py                # Passive admin health snapshot (OBS-002)
│   ├── ai_validation.py         # Strict AI-output parsing + bounded retry (AI-004)
│   ├── ai_cache_integrity.py    # HMAC sign/verify for the AI verdict cache (PROV-003)
│   ├── data_quality/            # Candle OHLCV validation + per-run receipt (DATA-001)
│   │   └── candles.py          # validate_candles + CandleQualityReport
│   ├── scanning/                # Scan lifecycle + provenance (SCAN-003 / PROV-001A)
│   │   ├── service.py          # run_scan: create -> run -> save -> finish
│   │   └── result_contract.py  # Typed result/provenance normalization
│   ├── observability/           # Structured, secret-safe logging (OBS-001)
│   ├── security/                # Secret redaction + AI prompt-injection quarantine
│   │   ├── redaction.py        # redact_text / SecretRedactionFilter / is_secret_key_name (SEC-002)
│   │   └── prompt_injection.py # External-evidence quarantine for the AI agents (TEST-003)
│   ├── fundamentals/            # Check Fundamentals subsystem
│   │   ├── screener_in_client.py# requests + BS4 scraper (peers via HTMX,
│   │   │                        # announcements, concall metadata)
│   │   ├── pdf_reader.py        # PDF download + text extraction
│   │   │                        # (pdfplumber → pypdf fallback)
│   │   ├── fundamentals_cache.py# On-disk JSON cache (data + verdict)
│   │   └── fundamental_agent.py # Claude Agent SDK agent + Pydantic schemas
│   ├── technical/               # Technical Analysis (AI) subsystem
│   │   ├── technical_agent.py  # Claude Agent SDK agent + TechnicalVerdict
│   │   ├── patterns.py         # FVG / double / order-block / market-structure detectors
│   │   ├── knowledge.py        # Externalized agent prompt knowledge
│   │   └── tools.py            # In-process MCP tools (level_map / price_patterns / market_structure)
│   ├── sixty_seven/             # 67 Ka Funda (AI) subsystem
│   │   ├── shortlister.py      # Deterministic 67% drawdown gate
│   │   ├── search_client.py    # SerpAPI Google search client
│   │   └── agent.py            # Claude Agent SDK verifier + Pydantic schemas
│   └── storage/                 # Scan-history persistence (SCAN-001/002) + audit log (OBS-003)
│       ├── models.py           # scan_runs / scan_results / ai_evaluations / audit_logs / app_config ORM schema
│       ├── database.py         # Engine + session factory (SQLite/Postgres)
│       └── repository.py       # Create/finish runs, save/read results + audit/config helpers
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
├── ui/                          # Streamlit UI pages + shared display helpers
│   ├── common.py               # Shared helpers (emoji badges, CSV-safe, redaction)
│   ├── chart_cache.py          # Per-session rendered-chart cache
│   ├── history_page.py         # Scan history view (SCAN-004)
│   ├── comparison_page.py      # Latest-vs-previous scan comparison view (JOB-003)
│   ├── validation_page.py      # Validation / Signal Performance dashboard (VALID-003B/004)
│   ├── health_page.py          # Admin health view (OBS-002)
│   ├── audit_page.py           # Admin audit log viewer (OBS-003)
│   └── config_page.py          # Admin runtime settings form (OBS-003)
├── Dependencies/
│   ├── .env.example             # Credential template (copy to .env)
│   └── dhan_token_setup.py      # One-time OAuth token helper
├── .streamlit/
│   ├── config.toml              # Streamlit theme
│   └── secrets.example.toml     # Google SSO template (copy to secrets.toml)
├── Dockerfile                    # Production Streamlit image (DEPLOY-001)
├── docker-compose.yml            # Local production stack: scanner-ui + Postgres (DEPLOY-002)
├── .env.example                  # Root Compose env template (copy to .env)
├── .dockerignore                 # Excludes secrets/generated runtime data
├── data/                        # Generated at runtime (git-ignored)
│   ├── cache/daily/             # Cached candles (Parquet)
│   ├── cache/fundamentals/      # Cached screener.in data + agent verdicts
│   │   └── pdfs/                # Downloaded concall transcripts + .txt
│   └── universes/               # Universe CSVs, including tracked Hemant lists
└── tests/                       # pytest suite (+ tests/golden/ screener snapshots)
```

The boundary is deliberate: **strategy logic lives in `screeners/`**, and
**data/broker plumbing lives in `backend/`**.

> **Architecture docs:** for a full design reference, see
> [`docs/architecture/`](docs/architecture/README.md) — a high-level design of the
> whole system plus a low-level design for each subsystem (with Mermaid diagrams).

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

PROV-003 AI-screener envelopes are HMAC-authenticated before reuse. Set
`SCANNER_AI_CACHE_SIGNING_KEY` to a deployment secret for restart-stable cache
hits. When it is absent, a process-random key allows safe hits only within the
current app process, so unsigned or previously forged files naturally miss.

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

## Authentication & access control

The Streamlit app is gated. `backend/auth/session.py` runs a single
`require_authorized_user(st)` check at the top of `main()`, so an unauthenticated
or unauthorized visitor stops **before** any screener control, result, chart, or
CSV download is reached.

- **Sign-in (AUTH-001)** — Google SSO via Streamlit's native OpenID Connect
  (`st.login` / `st.user` / `st.logout`), configured in `.streamlit/secrets.toml`
  (setup step 6). The email claim is verified and lower-cased.
- **Allowlist + admins (AUTH-002)** — `ALLOWED_EMAILS` decides who may use the
  app; `ADMIN_EMAILS` are always allowed and flagged `is_admin`. Admins can open
  the OBS-002 operational health view; scanner and history access remain
  unchanged. Both lists are comma-separated and case/space-insensitive.
- **Dev-permits / prod-fails-closed** — with an empty `ALLOWED_EMAILS`,
  development lets any signed-in Google user through when auth is enabled, but
  `APP_ENV=production` requires at least one allowed or admin email and cannot
  disable auth. Missing SSO config in production is a hard error, not a warning.

General role-based feature gating remains out of scope; OBS-002 uses only the
existing admin flag for its narrowly scoped operational page.

---

## Scan history & persistence

A persistence layer records each scan execution and its shortlisted rows so the
app can later answer *"why was this stock shortlisted on date D?"* without
re-running today's data, universe, or model.

- **Schema (SCAN-001)** — three SQLAlchemy tables in `backend/storage/models.py`:
  `scan_runs` (the audit header — screener, universe, params, data snapshot,
  app/git version, status, error, and the DATA-001 candle-quality receipt in
  `data_quality_json`), `scan_results` (one row per shortlisted stock), and
  `ai_evaluations` (approved, rejected, and error AI decisions).
- **Database layer (SCAN-002)** — `backend/storage/database.py` (engine + short
  `session_scope()` sessions; SQLite default at `data/scanner.db`, Postgres via
  `DATABASE_URL`) and `backend/storage/repository.py` (the only query/write
  helpers: `create_scan_run`, `save_scan_results`, `save_ai_evaluations`,
  `finish_scan_run`, `get_latest_scan_runs`, `get_scan_results`,
  `get_ai_evaluations`, `count_scan_results_for_runs`,
  `list_distinct_screener_keys`, `list_distinct_universe_keys`,
  `list_distinct_triggered_by_values`). Schema changes are versioned with
  **Alembic** (`migrations/`, `alembic.ini`); create or upgrade the local database
  with `python -m alembic upgrade head` (setup step 3).
- **Scan service (SCAN-003)** — `backend/scanning/run_scan(...)` wraps every scan
  (Streamlit UI and the headless daily job alike) in the persistence lifecycle:
  a RUNNING header before the screener executes, then results + a final
  SUCCESS / PARTIAL / FAILED status. The header also records how many symbols the
  universe contained (`symbols_scanned`).
- **Typed result contract (PROV-001A)** —
  `backend/scanning/result_contract.py` defines the small `ScreenerResult`,
  `SignalProvenance`, `RuleCheck`, `EvidenceReference`, expanded `AIProvenance`,
  and `AIEvaluationRecord` domain models. `BaseScanner` validates provenance and
  strict JSON before constructing the result DataFrame; the scan service then
  creates a separate secret-safe persistence copy with canonical
  `provenance_json`.
- **Scan history page (SCAN-004)** — switch the view radio at the top of the app
  to **Scan history** to inspect previous runs: started/finished timestamps,
  screener, universe, status badge, symbols scanned, shortlisted count, trigger,
  and error state. Filter by screener, universe, status, started-date range,
  trigger, or symbol (exact, case-insensitive), then click a run to see its
  persisted results and download them as CSV. Failed runs show their full
  (secret-redacted) error message.
- **Scan comparison page (JOB-003)** — switch to **Scan comparison** to compare
  the latest finalized run with the immediately previous finalized run for a
  selected screener/universe pair. Finalized means `SUCCESS` or `PARTIAL`; the
  page derives New today, Repeated from yesterday, Dropped today, Improved score,
  and Degraded score sections from existing `scan_runs` / `scan_results` data
  and exports the combined view as a formula-safe audited CSV.

> **Upgrading an existing checkout?** The app applies migrations automatically
> on startup, so the database gains the `symbols_scanned` column, the
> `ai_evaluations` ledger, and the `data_quality_json` column on first run.
> Runs recorded before the upgrade show "—" in that column — the value was not
> stored back then.

The design is documented in
[`docs/architecture/scan-run-persistence.md`](docs/architecture/scan-run-persistence.md).

### Result and provenance boundary

`raw_result_json` is the complete, JSON-safe audit copy of the screener row,
including strategy-specific columns. `provenance_json` is the standardized
explanation envelope: screener identity/version, triggered rules, indicator
values, parameters, data date, source category, notes, and an optional reserved
AI receipt. Legacy `provenance` and `rules` keys remain readable and are
enriched rather than discarded.

For a beginner-friendly analogy, `raw_result_json` is the complete worksheet a
screener handed in, while `provenance_json` is the consistently labeled
"show your work" section. Keeping both means new history features can read a
stable explanation format without throwing away strategy-specific details.

Every emitted shortlist row requires a symbol plus structured provenance with
non-empty triggered rules, non-empty scalar indicator values, and a valid source.
Dates, lossless `Decimal` strings, and NumPy scalars are normalized to strict
JSON; non-finite indicators and custom/collection values are rejected, callable
parameters are omitted, and credential-shaped values are redacted.

**Deterministic screener provenance (PROV-002).** Every screener now records
*why* a stock shortlisted. `BaseScanner` exposes `build_provenance(...)`, and each
`compute_signal` returns it under a reserved trailing `provenance` column:

```python
"provenance": self.build_provenance(
    triggered_rules=["close_at_or_below_lower_envelope_band"],
    indicator_values={"close": latest_close, "env_lower": latest_lower},
    # source defaults to "deterministic"; AI-assisted screeners pass "ai"/"hybrid".
)
```

The helper stamps the screener's `key` and `SCREENER_VERSION`, converts indicator
values to plain JSON scalars, and the persistence layer folds it into
`provenance_json` (filling run-level `params_snapshot`/`data_snapshot_date`). The
Both `provenance` and `provenance_json` are dropped from the on-screen results
table and download CSV, so the receipt is durable audit evidence without
cluttering the UI. The two AI screeners label
their rows `source="hybrid"` (gate + AI) or `"deterministic"` (gate-only
fallback).

**AI screener provenance (PROV-003).** Technical Analysis and 67 Ka Funda stamp
the configured model, semantic prompt version, full prompt SHA-256, UTC
generation time, cache-hit state, verdict/confidence/reason, and hashed evidence
references. Versioned, HMAC-authenticated cache entries store only the validated
verdict-plus-receipt envelope. The 67 Ka Funda research collector rejects
model-directed instructions in external evidence and never persists raw model
responses or scraped snippets; result summaries use sanitized source
labels/domains.

---

## Audit log (OBS-003)

Important user actions are recorded to a durable, queryable **audit log** so an
operator can later answer *"who exported that file?"* or *"when was the log level
changed, and by whom?"* Every audit row carries the **user email**, a UTC
**timestamp**, **action metadata**, and has **sensitive values redacted**.

- **Schema** — two SQLAlchemy tables in `backend/storage/models.py`: `audit_logs`
  (the trail — `event`, `user_email`, `created_at`, redacted `metadata_json`) and
  `app_config` (admin runtime-config overrides). They are created by the
  `…obs003_create_audit_logs` Alembic migration and apply automatically on
  startup.
- **Recorder** — `backend/audit` writes each event to the `audit_logs` table
  **and** emits the matching structured log event. Recording is **best-effort**
  (a database hiccup never breaks the user's action) and routes metadata through
  the same redactor as scan provenance, so a token can never become durable audit
  evidence. System actions that run before sign-in (the startup data refresh)
  record `user_email` as `system`; first-run audit call sites bootstrap the
  schema before writing so fresh local databases can still keep the durable row.
- **Events recorded** — `login_success`, `login_denied`, `manual_scan_started`,
  `data_refresh_started`, `config_changed`, `export_downloaded`, and
  `admin_page_accessed` (see the event table under
  [Observability & logging](#observability--logging)).
- **Admin pages** — admins gain two views in the top selector (alongside Admin
  health): **Audit log** (browse/filter the trail) and **Admin settings** (change
  the operational settings `LOG_LEVEL` / `LOG_FORMAT` at runtime). A settings
  change is validated, persisted to `app_config`, applied immediately
  (`get_settings()` reads the environment live), and recorded as `config_changed`.
  Only those non-secret operational keys are editable — credentials and auth/infra
  settings are deliberately out of scope.

Full design: [`docs/architecture/obs-003-audit-log.md`](docs/architecture/obs-003-audit-log.md)
and the LLD [`docs/architecture/components/audit-log.md`](docs/architecture/components/audit-log.md).

## Adding your own screener

> The complete walkthrough — including golden tests, chart hooks, registry
> validation, and the pre-merge checklist — lives in
> [docs/adding-a-screener.md](docs/adding-a-screener.md). The short version:

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
        return None  # or a dict of the common + extra columns, plus provenance:
        # return {
        #     "symbol": symbol, "rating": "BUY", "signal_date": ...,
        #     "close": ..., "reason": ..., "my_indicator_value": ...,
        #     "provenance": self.build_provenance(
        #         triggered_rules=["my_rule_fired"],
        #         indicator_values={"my_indicator_value": ...},
        #     ),
        # }

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
`hemant_super_45`, `hemant_good_45`, `hemant_good_200`, and the composites
`hemant_super_good_union` (Hemant Super 45 ∪ Good 45) and
`hemant_super_good_200_union` (Hemant Super 45 ∪ Good 45 ∪ Good 200), both deduped.
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
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
python -m pre_commit validate-config .pre-commit-config.yaml
python -m pre_commit run --all-files
# CI also verifies the deployment image:
docker build --tag streamlit-scanner-app:ci .
docker compose config
docker compose up --build --wait --wait-timeout 180
docker compose down --volumes --remove-orphans
```

Beginner note: `requirements-optional.txt` is intentionally separate. Those
packages speed up some indicators when their native prerequisites are already
available, but the app falls back to pure-pandas math without them.

The suite includes **golden-snapshot** regression tests for the deterministic
screeners (`tests/test_screener_golden_outputs.py`, with snapshots under
`tests/golden/`). After an *intentional* screener change, refresh the snapshots
with `UPDATE_GOLDEN=1 python -m pytest tests/test_screener_golden_outputs.py` and
review the diff. The Alembic migration is also guarded by a drift test that fails
if the ORM models and the migration fall out of sync. A **repository-boundary**
guard (`tests/test_repository_layer_boundary.py`, REFACTOR-002) likewise fails CI
if any module outside `backend/storage` builds raw SQL, opens an engine, or creates
a session, keeping all database access behind the repository layer.

> **Contributing / AI agents:** the development conventions, layering rules, full
> CI gate suite, multi-agent worktree workflow, and the skills this project expects
> you to use are documented in [`AGENTS.md`](AGENTS.md) — Claude Code loads it via
> [`CLAUDE.md`](CLAUDE.md). Read it before starting work.

---

## License

Released under the [MIT License](LICENSE).
