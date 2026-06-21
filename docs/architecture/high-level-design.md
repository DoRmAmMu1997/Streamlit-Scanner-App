# High-Level Design — Streamlit Scanner App

> **What this is.** A whole-system design overview of the Streamlit Scanner App.
> For the internal design of any one subsystem, follow the links in the
> [component map](#5-component-map) to its low-level design (LLD) under
> [`components/`](components/). For the persistence schema rationale see the
> existing [`scan-run-persistence.md`](scan-run-persistence.md).

| | |
|---|---|
| **System** | Pluggable daily-candle stock scanner for Indian equities (DhanHQ data + Streamlit UI + optional Claude-agent analysis). |
| **Audience** | New contributors, reviewers, and the Claude/Codex split working the backlog. |
| **Status** | Living document — reflects `main`. |

## 1. System summary

`python app.py` downloads stock universes and ~10 years of daily candles, then
opens a Streamlit app. A user picks a **screener** (a single Python file in
`screeners/`), runs it over a universe, and browses the shortlist with
interactive TradingView Lightweight Charts. Shortlisted rows can be sent to a
**Check Fundamentals** Claude agent. Two screeners are themselves AI-assisted
(Technical Analysis, 67 Ka Funda). Every scan — from the UI or a headless daily
job — is recorded to a scan-history database. Access is gated behind Google SSO
with an email allowlist.

## 2. Goals & requirements

**Functional**
- Run pluggable screeners over configurable stock universes on cached daily candles.
- Interactive per-stock charts with the screener's own indicator overlay.
- Per-stock AI fundamental analysis + two AI-assisted screeners (graceful degradation when AI is unavailable).
- Persist every scan run + shortlist for later "why was this shortlisted on date D?" audit.
- Headless daily job for schedulers; Google-SSO gate + allowlist.

**Non-functional**
- **Single-writer research tool**, not a high-availability service: correctness, auditability, and low cost over throughput.
- **Fast after first run**: prefetch up front; incremental candle top-up; chart/session caches.
- **Cost-bounded AI**: cheap deterministic gates before any LLM; per-day verdict caches; Claude-subscription billing (no per-token API key).
- **Secret-safe & fail-closed**: redaction on every output sink; production refuses unsafe config.
- **Portable storage**: SQLite locally, Postgres in deployment, same schema.

**Constraints**: Python 3.11+; DhanHQ account for data; TA-Lib/pandas_ta optional (pure-pandas fallback); Claude Agent SDK + SerpAPI optional.

## 3. Context — external systems

```mermaid
flowchart TD
    User["User (browser)"] --> APP["Streamlit Scanner App"]
    Cron["Scheduler / cron"] --> JOB["Daily scan job"]
    JOB --> APP
    APP -->|OIDC sign-in| Google["Google OIDC"]
    APP -->|daily candles, instrument master| Dhan["DhanHQ API"]
    APP -->|company data scrape| ScreenerIn["screener.in"]
    APP -->|agentic analysis via subscription| Claude["Claude Agent SDK / CLI"]
    APP -->|web research| Serp["SerpAPI (Google)"]
    APP -->|chart lib via CDN+SRI| CDN["unpkg: Lightweight Charts"]
    APP --> DB[("SQLite / Postgres scan history")]
    APP --> Cache[("Local Parquet + JSON caches")]
```

External data and AI text are treated as **untrusted evidence**, never instructions (prompt-injection posture): a shared quarantine (TEST-003) scans scraped/search/transcript evidence before it reaches any model and the AI agents fail closed on a hit; server-side fetches pass SSRF guards.

## 4. Architecture overview

The deliberate boundary: **strategy logic in `screeners/`, plumbing in `backend/`**. Three entrypoints share one backend.

```mermaid
flowchart TB
    subgraph Entrypoints
      PRE["python app.py — prefetch (CLI)"]
      UI["streamlit run app.py — UI"]
      JOB["python -m backend.jobs.run_daily_scan"]
    end
    subgraph Strategy["screeners/ (strategy)"]
      SCR["10 screeners : BaseScanner subclasses"]
    end
    subgraph Backend["backend/ (plumbing)"]
      REG["screener_registry"]; BASE["scanner_base"]; IND["indicators"]
      DATA["dhan_client + daily_data_loader"]; UNI["universe_*"]
      SVC["scanning.service + result_contract"]; VAL["validation"]; STORE["storage + migrations"]
      AIF["fundamentals"]; AIT["technical"]; AI67["sixty_seven"]
      CH["charts"]; AUTH["auth"]; CFG["config"]; OBS["observability"]; SEC["security"]; HLT["health"]
    end
    PRE --> UNI & DATA
    UI --> AUTH --> REG --> SCR
    UI --> SVC; JOB --> SVC
    SCR --> BASE --> IND
    SVC --> SCR --> DATA --> UNI
    SVC --> STORE
    VAL --> STORE & DATA & UNI
    SCR -. AI screeners .-> AIT & AI67
    UI -. Check Fundamentals .-> AIF
    UI --> CH
    CFG & OBS & SEC --- Backend
```

## 5. Component map

| Subsystem | LLD | Responsibility |
|---|---|---|
| App entrypoint & orchestration | [app-orchestration](components/app-orchestration.md) | Prefetch CLI + Streamlit `main()`, view router, scan state machine |
| Authentication | [authentication](components/authentication.md) | Google OIDC gate + email allowlist/admins |
| Configuration | [configuration](components/configuration.md) | Typed env settings, prod fail-closed, secret list |
| Deployment runtime | [deployment-runtime](components/deployment-runtime.md) | Docker image, build context, container env, port, health check |
| Data acquisition | [data-acquisition](components/data-acquisition.md) | DhanHQ client + Parquet candle cache |
| Data quality | [data-quality](components/data-quality.md) | Candle OHLCV validation + loader-boundary quarantine + per-run quality receipt (DATA-001) |
| Universe management | [universe-management](components/universe-management.md) | Build/load universe CSVs, symbol→security_id |
| Screener framework | [screener-framework](components/screener-framework.md) | `BaseScanner` ABC + plugin registry |
| Indicators | [indicators](components/indicators.md) | TA-Lib/pandas_ta + fallbacks, levels, weekly |
| Screener catalog | [screener-catalog](components/screener-catalog.md) | The 10 strategies |
| Scan service & provenance | [scan-service-and-provenance](components/scan-service-and-provenance.md) | `run_scan` lifecycle + strict result/provenance contract + AI evaluation receipts |
| Storage & persistence | [storage-persistence](components/storage-persistence.md) | ORM (`scan_runs`/`scan_results`/`ai_evaluations`/`audit_logs`/`app_config`), engine/session, repository, finalized comparison helpers, Alembic |
| Scan comparison | [scan-comparison](components/scan-comparison.md) | JOB-003 latest-vs-previous shortlist read model over `scan_runs`/`scan_results` + finalized-run helpers |
| Forward-return validation | [validation](components/validation.md) | VALID-002 calculator/service, VALID-003A/004 aggregate/dashboard metrics for `signal_forward_returns` rows, the read-only Validation / Signal Performance dashboard, and the headless compute job |
| Daily scan job | [daily-scan-job](components/daily-scan-job.md) | Headless CLI + YAML schedule |
| Check Fundamentals (AI) | [fundamentals-ai](components/fundamentals-ai.md) | Claude agent + screener.in scraper + PDF + cache |
| Technical Analysis (AI) | [technical-analysis-ai](components/technical-analysis-ai.md) | Claude agent + price-action detectors + MCP tools |
| 67 Ka Funda (AI) | [sixty-seven-ka-funda-ai](components/sixty-seven-ka-funda-ai.md) | Drawdown gate + SerpAPI + Claude verifier |
| Charts & visualization | [charts-visualization](components/charts-visualization.md) | Lightweight-Charts specs + chart cache |
| UI pages | [ui-pages](components/ui-pages.md) | Scan-history page + scan comparison + validation dashboard + shared UI helpers |
| Observability | [observability](components/observability.md) | Structured, secret-safe logging |
| Audit log | [audit-log](components/audit-log.md) | Durable user-action trail (`audit_logs`) + admin runtime-config form (`app_config`) + viewer (OBS-003) |
| Security | [security](components/security.md) | Secret redaction + SSRF guards + AI verdict-cache integrity (HMAC) |
| Health monitoring | [health-monitoring](components/health-monitoring.md) | Passive admin health snapshot/page |

## 6. End-to-end flows

### 6a. Interactive scan → chart → Check Fundamentals
```mermaid
sequenceDiagram
    participant U as User
    participant App as app.main()
    participant Auth as auth gate
    participant Svc as run_scan
    participant Scr as screener
    participant Loader as DailyDataLoader
    participant DB as scan history
    U->>App: open / Run screener
    App->>Auth: require_authorized_user
    App->>Svc: run_scan(screener, universe, loader, params, triggered_by)
    Svc->>DB: create RUNNING header
    Svc->>Scr: run() → per-symbol compute
    Scr->>Loader: iter_universe_history (cache/Dhan)
    Scr-->>Svc: results DataFrame
    Svc->>DB: save results + finish (SUCCESS/PARTIAL/FAILED)
    Svc-->>App: ScanRunResult
    App->>U: table + chart (row click)
    U->>App: Check Fundamentals
    App->>App: FundamentalAgent.check (cache/Claude) → verdict
```

### 6b. Headless daily scan + persistence
```mermaid
sequenceDiagram
    participant Cron as scheduler
    participant Job as run_daily_scan
    participant Svc as run_scan
    participant DB as scan history
    Cron->>Job: python -m backend.jobs.run_daily_scan [--config]
    Job->>DB: ensure_database_schema (else exit 1)
    loop each enabled screener
        Job->>Svc: run_scan(..., triggered_by="job:daily_scan")
        Svc->>DB: header → results → status
        Svc-->>Job: DailyScanOutcome (run_id?, status)
    end
    Job-->>Cron: exit code (1 if any fatal / no run_id)
```

### 6c. Auth gate
```mermaid
flowchart LR
    V["visitor"] --> A{"signed in (Google OIDC) + verified email?"}
    A -->|no| L["login button / stop"]
    A -->|yes| Z{"on allowlist OR admin? (prod fails closed)"}
    Z -->|no| D["auth_denied + stop"]
    Z -->|yes| OK["scanner / history (+ admin health if admin)"]
```

## 7. Cross-cutting concerns

- **Auth** — one gate at the top of `main()`; nothing renders before it. ([authentication](components/authentication.md))
- **Observability** — named structured events, JSON in prod, identical across all three entrypoints. ([observability](components/observability.md))
- **Audit trail (OBS-003)** — important user actions (sign-ins, manual scans, the startup data refresh, config changes, CSV exports, admin-page access) are recorded to a durable `audit_logs` table with the actor email, a UTC timestamp, and redacted metadata. Recording is best-effort (never breaks the action) and routes through the same redactor as scan provenance; admins browse it in an in-app viewer. ([audit-log](components/audit-log.md))
- **Security** — secret redaction on every sink (logs, UI errors, persisted messages); SSRF guards on scraped fetches; CSV-injection escaping; a shared prompt-injection quarantine (TEST-003) that scans external evidence before model exposure and fails the AI screeners closed on a hit. ([security](components/security.md))
- **Persistence, provenance, comparison, and validation** — every shortlisted row carries a deterministic receipt (PROV-002: `triggered_rules` + `indicator_values` + `source`, built by `BaseScanner.build_provenance`); AI screeners add a tamper-evident verdict receipt (PROV-003: model, semantic prompt version, prompt/evidence/context SHA-256, sanitized source URLs — never raw scraped/model text) persisted to the `ai_evaluations` ledger. JOB-003 compares the latest finalized shortlist with the immediately previous finalized shortlist per screener/universe pair. VALID-002 computes per-signal forward returns into `signal_forward_returns` without re-running the screener, VALID-003A/004 aggregate those stored rows into screener/universe/horizon performance metrics and dashboard slices, VALID-003B/004 surface them in a read-only Validation / Signal Performance dashboard, and VALID-004 adds the headless compute job for pending rows. ([scan-service-and-provenance](components/scan-service-and-provenance.md), [storage-persistence](components/storage-persistence.md), [validation](components/validation.md))
- **Caching** — Parquet candle cache (incremental), per-day AI verdict cache (**HMAC-signed and verified before reuse** — a tampered entry is rejected and recomputed), per-session chart cache, 30/60s Streamlit data caches.
- **Graceful AI degradation** — cheap gate first; if the SDK/SerpAPI is absent, Technical Analysis falls back to a gate-only BUY while 67 Ka Funda skips the candidate (partial run) — neither crashes the scan. Approved, rejected, **and** error AI decisions are all recorded in `ai_evaluations` for audit.
- **Trustworthy AI output (AI-004)** — every AI verdict is parsed into a strict Pydantic schema; malformed/incomplete output may be retried within the configured attempt budget (`SCANNER_AI_MAX_ATTEMPTS`, default 2; 1 disables retries) and is then **rejected** as `AIValidationError`, while the run records the count distinctly (`ai_validation_failures`, `phase="ai_validation"`) so junk output can never silently corrupt scan results. ([scan-service-and-provenance](components/scan-service-and-provenance.md))

## 8. Tech stack

Python 3.11+ · Streamlit · pandas / pyarrow · SQLAlchemy 2 + Alembic · `dhanhq` · `requests` + BeautifulSoup · `pdfplumber`/`pypdf` (optional) · `claude-agent-sdk` (optional) · TradingView Lightweight Charts v5 (CDN+SRI) · Pydantic. Optional accelerators TA-Lib / pandas_ta. **Dependency policy**: `requirements.txt` (bare names) installed with `constraints.txt` (exact `==` pins); `requirements-optional.txt` / `requirements-dev.txt` separate.

## 9. Data & storage

Runtime data under `DATA_DIR` (default `./data`, git-ignored): `cache/daily/*.parquet` (candles), `cache/fundamentals/` (JSON data + verdicts + concall PDFs), `universes/*.csv`, `scanner.db` (SQLite). Scan history = `scan_runs` (1) ──< `scan_results` (many), `scan_runs` (1) ──< `ai_evaluations` (many — the AI verdict ledger of approved/rejected/error receipts), and `scan_results` (1) ──< `signal_forward_returns` (many — VALID-002 per-horizon validation rows, aggregated by VALID-003A); deterministic columns for queries + `raw_result_json`/`provenance_json` for flexible audit. JOB-003 scan comparison is derived from those existing scan history tables, so it adds no runtime table. Full design: [scan-run-persistence.md](scan-run-persistence.md). OBS-003 adds two standalone tables on the same database: `audit_logs` (the user-action trail) and `app_config` (admin runtime-config overrides) — design: [obs-003-audit-log.md](obs-003-audit-log.md).

## 10. Deployment & runtime

- **Local dev**: `AUTH_REQUIRED=false` default, SQLite, repo-local `data/`, optional providers off.
- **Docker image**: `python:3.11-slim-bookworm`, runtime dependencies installed with `requirements.txt` + `constraints.txt`, non-root `appuser`, `DATA_DIR=/data`, `EXPOSE 8501`, Streamlit bound to `0.0.0.0:8501`, and a `/_stcore/health` health check. The image runs `streamlit run app.py`, not the local `python app.py` prefetch wrapper.
- **Docker Compose local production mode**: `docker-compose.yml` starts exactly two long-lived services, `scanner-ui` and `postgres`. `scanner-ui -> postgres` uses the private Compose network and `postgresql+psycopg://...@postgres:5432/...`; only Streamlit's `${SCANNER_UI_PORT:-8501}:8501` is published to the host. `scanner-data` keeps `/data` app state separate from `postgres-data` database storage.
- **Render managed hosting (DEPLOY-003 / DEPLOY-003B)**: `render.yaml` Blueprint reusing the same image — a `scanner-web` web service (persistent disk at `DATA_DIR=/data` for the candle cache) + a managed `scanner-db` Postgres auto-wired into `DATABASE_URL` with public ingress closed + an ephemeral `scanner-daily-scan` cron. Render disks are single-attach, so the disk lives on the web service while the cron re-fetches candles and writes to the shared Postgres. Env secrets are dashboard-provided (`sync: false`); Google OIDC uses the Render Docker secret file at `/etc/secrets/streamlit-secrets.toml`. DEPLOY-003B keeps the cron deployable by committing `config/daily_scans.yaml` as the deterministic default schedule while AI-heavy jobs stay opt-in.
- **Production** (`APP_ENV=production`): requires explicit `DATABASE_URL` + `DATA_DIR` (persistent volume) + Dhan creds + `AUTH_REQUIRED=true` + an allow/admin email; rejects `AUTH_REQUIRED=false`. Logs render JSON. Migrations apply automatically on startup. A bare `postgresql://` `DATABASE_URL` (as managed providers auto-wire) is normalized to the pinned `postgresql+psycopg://` driver at startup.
- **CI quality gates** (`.github/workflows/quality-and-security.yml`, Python 3.11 + 3.12): `pytest` (coverage ≥84% on `backend`/`screeners`/`ui`), `compileall`, `ruff`, `mypy`, `bandit`, `pip-audit`, `pre-commit`, plus golden-snapshot + Alembic drift tests. The `docker-build` job also runs `docker build --tag streamlit-scanner-app:ci .`, `docker compose config`, and `docker compose up --build --wait --wait-timeout 180` before `docker compose down --volumes --remove-orphans`, so both image assembly and the local production stack are verified in CI.

## 11. System-wide design decisions

| Decision | Rationale |
|---|---|
| **Screeners vs backend boundary** | Strategy authors touch one file; plumbing changes never require editing strategies. |
| **Plugin auto-discovery** | Drop a file in `screeners/` → it appears in the UI; no central registration. |
| **Prefetch-then-UI** | Slow network work happens once up front; the app feels instant. |
| **Cheap gate → AI on survivors** | Bounds AI cost/latency to a handful of candidates. |
| **One persistence schema (typed cols + JSON)** | Serves deterministic and AI screeners without per-strategy tables or flag-day migrations. |
| **Best-effort persistence in UI, strict in the job** | The UI always shows fresh rows even if the DB is down; the scheduled job fails loudly if history isn't written. |
| **Secret-safe by construction** | Redaction is a shared filter on every output sink, not a per-call concern. |
| **Claude-subscription billing** | AI features draw on the plan's Agent SDK credit; `ANTHROPIC_API_KEY` is deliberately kept unset. |
| **Tamper-evident AI receipts** | AI verdicts persist hashed evidence + a semantic prompt version as an audit ledger (`ai_evaluations`); the on-disk verdict cache is HMAC-signed so a forged/edited entry is rejected and recomputed, never trusted. Raw scraped text and raw model responses are never stored. |
| **Strict result contract, truthful status** | Rows are validated against the provenance contract *before* the DataFrame is built; contract-rejected rows and persistence failures downgrade the run to `PARTIAL`/`FAILED` rather than reporting a false success. |
| **Validate within a bounded attempt budget (AI-004)** | Strict-schema parsing may retry malformed output within the configured 1–3 attempt budget (never SDK/usage-limit errors); a budget of 1 disables retries, and invalid output is rejected and counted, never persisted. |
| **Quarantine bad candle data at the loader boundary (DATA-001)** | A pure validator screens every OHLCV frame; structurally impossible candles (high<low, NaN, dup dates) are withheld from scanners and downgrade the run (`PARTIAL`/`FAILED`), while stale/gappy data passes as a recorded warning. Each run persists a bounded, redacted quality receipt (`scan_runs.data_quality_json`) so the app does not trust raw vendor candles without an audit trail. |
| **Forward validation uses elapsed data only (VALID-002)** | Historical signal validation enters at the next trading day's open and exits at the Nth trading day's close. Rows stay `pending` while the window has not elapsed, become `insufficient_data` only after stale missing data, and never forward-fill absent bars. |
| **Validation aggregates stay read-only (VALID-003A/004)** | Screener-level validation metrics and dashboard slices read existing `signal_forward_returns` rows through repository-owned joins. Pending and insufficient rows remain separate counts, hit rate is computed only from stored computed returns, missing benchmark/excess values stay null, and sector labels fall back to `Unknown` rather than being fabricated. |
| **Ranking is additive, never destructive (RANK-001)** | The composite `final_score` (0–100 over technical/liquidity/risk/freshness, renormalized over the components that have data) is *added* beside each row's `reason`/`raw_result_json`, never replacing them. It lands in the reserved `final_score` column plus a `score_breakdown` receipt in `provenance_json`, so ranking needs no flag-day migration and the raw evidence behind a rank stays visible. Missing inputs drop a component (never fabricated), scoring is non-fatal to the scan, and the formula uses stored dates (no wall-clock) so re-scoring is replay-stable. See [rank-001-final-scoring-model.md](rank-001-final-scoring-model.md). |
| **Container entrypoint serves Streamlit directly (DEPLOY-001)** | `python app.py` is optimized for local prefetch-before-browser startup. The Docker image uses `streamlit run app.py` directly, sets production/auth defaults, exposes only port 8501, and keeps mutable state in `/data` so deployments are repeatable and host-independent. |
| **Compose mirrors the production split locally (DEPLOY-002)** | Local production mode uses the same image plus a private Postgres service, keeping `scanner-data` and `postgres-data` in separate named volumes. Postgres is intentionally not published on the host because only the app needs database access during normal operation. |
| **Render Blueprint reuses the image; disk on web, cron ephemeral (DEPLOY-003 / DEPLOY-003B)** | `render.yaml` provisions web + managed Postgres + disk + cron from the same `Dockerfile`. Because a Render disk is single-attach, the persistent candle cache lives on the web service; the cron runs ephemerally and re-fetches candles, writing scan history to the shared Postgres (the only must-share state). The auto-wired `postgresql://` URL is normalized to the psycopg v3 driver so the Blueprint self-wires without a hand-edited value, `ipAllowList: []` keeps the database private to Render services, and the cron's `--config config/daily_scans.yaml` points at a committed deterministic-default schedule. |

## 12. Risks & future evolution

- **AI/external dependency** — Dhan/screener.in/SerpAPI/CLI changes can break features; mitigated by fallbacks, caches, and untrusted-evidence handling.
- **Single-writer SQLite locally** — WAL + short sessions mitigate; Postgres for real concurrency.
- **Recently shipped**: PROV-002 (deterministic per-screener receipts via `build_provenance`), PROV-003 (AI verdict receipts + the `ai_evaluations` ledger + HMAC verdict-cache integrity), DATA-001 (candle data-quality quarantine + per-run receipt), DEPLOY-001 (Docker runtime packaging), DEPLOY-002 (Docker Compose local production stack), DEPLOY-003/DEPLOY-003B (Render Blueprint + DB-URL driver normalization + committed daily-scan schedule), OBS-003 (user-action audit log + admin runtime-config form/viewer), JOB-003 (latest-vs-previous scan comparison), and VALID-001/VALID-002/VALID-003A/VALID-003B/VALID-004 (forward-return schema, calculator/service, backend aggregate metrics, read-only validation dashboard, dashboard slices/export, and headless compute job) are now in flight.
- **Roadmap (backlog)**: RANK-002 (implement the `final_score` scorer — methodology designed in RANK-001) and RANK-003 (fundamental/valuation components), richer validation visualizations beyond v1 tables, AUTH-003 (role-gated features), and later DEPLOY-* hosting automation. These land in reserved columns / JSON or runtime docs without flag-day migrations.
