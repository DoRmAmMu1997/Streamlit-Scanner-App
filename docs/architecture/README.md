# Architecture documentation

This directory documents the architecture of the **Streamlit Scanner App** at two
levels:

- a **[High-Level Design](high-level-design.md)** of the whole system, and
- a **Low-Level Design (LLD)** for each subsystem under [`components/`](components/).

New here? Read the [HLD](high-level-design.md) first — its
[component map](high-level-design.md#5-component-map) and
[end-to-end flows](high-level-design.md#6-end-to-end-flows) orient you, then each
row links to the matching LLD for internal detail.

> Diagrams use **Mermaid** (renders natively on GitHub). Every LLD is grounded in
> the current source on `main`; file references are clickable relative links.

## High-level

- **[high-level-design.md](high-level-design.md)** — system summary, goals, context + architecture diagrams, component map, end-to-end flows, cross-cutting concerns, tech stack, deployment, decisions, and roadmap.

## Component LLDs

Each LLD follows the same shape: purpose & boundaries · position diagram · public
interface · design decisions & trade-offs · failure modes · configuration ·
testing · extension points.

### Entry points & UI
- [app-orchestration.md](components/app-orchestration.md) — `app.py` prefetch CLI + Streamlit `main()` + scan flow.
- [ui-pages.md](components/ui-pages.md) — scan-history page + shared `ui/common.py` helpers.
- [daily-scan-job.md](components/daily-scan-job.md) — headless daily-scan CLI + YAML schedule.

### Screening engine
- [screener-framework.md](components/screener-framework.md) — `BaseScanner` ABC + plugin registry.
- [screener-catalog.md](components/screener-catalog.md) — the 10 screeners.
- [indicators.md](components/indicators.md) — indicator library (TA-Lib/pandas_ta + fallbacks).
- [scan-service-and-provenance.md](components/scan-service-and-provenance.md) — `run_scan` lifecycle + result/provenance contract.
- [charts-visualization.md](components/charts-visualization.md) — Lightweight-Charts specs + chart cache.

### Data & persistence
- [data-acquisition.md](components/data-acquisition.md) — DhanHQ client + Parquet candle cache.
- [data-quality.md](components/data-quality.md) — candle OHLCV validation + loader-boundary quarantine + per-run quality receipt (DATA-001).
- [universe-management.md](components/universe-management.md) — universe build/load.
- [storage-persistence.md](components/storage-persistence.md) — ORM, engine/session, repository, Alembic.

### AI subsystems
- [fundamentals-ai.md](components/fundamentals-ai.md) — Check Fundamentals agent + screener.in scraper + PDF reader + cache (the shared SDK plumbing).
- [technical-analysis-ai.md](components/technical-analysis-ai.md) — Technical Analysis agent + detectors + MCP tools.
- [sixty-seven-ka-funda-ai.md](components/sixty-seven-ka-funda-ai.md) — drawdown gate + SerpAPI + Claude verifier.

### Cross-cutting
- [audit-log.md](components/audit-log.md) — durable user-action audit trail + admin runtime-config form/viewer (OBS-003).
- [authentication.md](components/authentication.md) — Google OIDC gate + allowlist/admins.
- [configuration.md](components/configuration.md) — typed runtime settings + prod fail-closed.
- [deployment-runtime.md](components/deployment-runtime.md) — Docker image, Docker Compose local production stack, build context, container env, port, health check.
- [observability.md](components/observability.md) — structured, secret-safe logging.
- [security.md](components/security.md) — secret redaction + SSRF guards + AI verdict-cache integrity (`ai_cache_integrity.py`, HMAC).
- [health-monitoring.md](components/health-monitoring.md) — passive admin health snapshot/page.

## Ticket-scoped design docs (historical, still authoritative)

- **[scan-run-persistence.md](scan-run-persistence.md)** — SCAN-001 scan-run persistence schema (the column-by-column rationale the Storage LLD links to).
- **[scan-002-handoff.md](scan-002-handoff.md)** — SCAN-002 database-layer implementation handoff brief.
- **[obs-003-audit-log.md](obs-003-audit-log.md)** — OBS-003 audit log + runtime-config schema, recorder design, and the seven tracked events.
- **[audit-2026-06.md](audit-2026-06.md)** — June 2026 codebase audit & hardening register (QUAL-001/002/003, REF-001, PERF-001, DOC-001): what was found, fixed, and deferred.

## Conventions

- **Boundary**: strategy logic lives in `screeners/`; data/broker/infra plumbing lives in `backend/`.
- **Untrusted evidence**: scraped pages and AI/tool output are treated as evidence, never instructions.
- **Secret-safe**: every log line, UI error, and persisted message passes through the SEC-002 redactor.
- **One persistence schema** serves deterministic and AI screeners via `raw_result_json` / `provenance_json`.

Keep these docs in sync with the code: when a subsystem's interface, schema, or a
key decision changes, update its LLD (and the HLD if the change is system-wide) in
the same PR.
