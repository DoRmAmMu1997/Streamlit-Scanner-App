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
- [ui-pages.md](components/ui-pages.md) — scan-history, scan-comparison + validation pages + shared `ui/common.py` helpers.
- [scan-comparison.md](components/scan-comparison.md) — JOB-003 latest-vs-previous shortlist read model (`backend/scanning/comparison.py`) + finalized-run repository helpers.
- [daily-scan-job.md](components/daily-scan-job.md) — headless daily-scan CLI + YAML schedule.

### Screening engine
- [screener-framework.md](components/screener-framework.md) — `BaseScanner` ABC + plugin registry.
- [screener-catalog.md](components/screener-catalog.md) — the 11 screeners.
- [indicators.md](components/indicators.md) — indicator library (TA-Lib/pandas_ta + fallbacks).
- [scan-service-and-provenance.md](components/scan-service-and-provenance.md) — `run_scan` lifecycle + result/provenance contract.
- [scoring.md](components/scoring.md) — RANK-002 deterministic `final_score` scorer + score-component UI/export behavior.
- [charts-visualization.md](components/charts-visualization.md) — Lightweight-Charts specs + chart cache.

### Data & persistence
- [data-acquisition.md](components/data-acquisition.md) — DhanHQ client + Parquet candle cache.
- [data-quality.md](components/data-quality.md) — candle OHLCV validation + loader-boundary quarantine + per-run quality receipt (DATA-001).
- [universe-management.md](components/universe-management.md) — universe build/load.
- [storage-persistence.md](components/storage-persistence.md) — ORM, engine/session, repository, Alembic.
- [validation.md](components/validation.md) — VALID-002 forward-return calculator + benchmark comparison service.
- [ipo-screener.md](components/ipo-screener.md) — IPO-001 domain through IPO-010: ingestion, cache, evidence, ratios, factors/flags, verdicts, dashboard, and orchestration.

### AI subsystems
- [fundamentals-ai.md](components/fundamentals-ai.md) — Check Fundamentals agent + screener.in scraper + PDF reader + cache (the shared SDK plumbing).
- [ipo-extraction-ai.md](components/ipo-extraction-ai.md) — IPO-010 financial-extraction agent: quarantined tools, host-side citation verification, fail-closed proposals.
- [technical-analysis-ai.md](components/technical-analysis-ai.md) — Technical Analysis agent + detectors + MCP tools.
- [sixty-seven-ka-funda-ai.md](components/sixty-seven-ka-funda-ai.md) — drawdown gate + SerpAPI + Claude verifier.

### Cross-cutting
- [audit-log.md](components/audit-log.md) — durable user-action audit trail + admin runtime-config form/viewer (OBS-003).
- [authentication.md](components/authentication.md) — Google OIDC gate + allowlist/admins.
- [configuration.md](components/configuration.md) — typed runtime settings + prod fail-closed.
- [deployment-runtime.md](components/deployment-runtime.md) — Docker image, Docker Compose local production stack, the Render Blueprint (DEPLOY-003), build context, container env, port, health check.
- [observability.md](components/observability.md) — structured, secret-safe logging.
- [notifications.md](components/notifications.md) — ALERT-001 daily-scan Telegram/email summary (opt-in, best-effort).
- [security.md](components/security.md) — secret redaction + SSRF guards + AI verdict-cache integrity (`ai_cache_integrity.py`, HMAC).
- [health-monitoring.md](components/health-monitoring.md) — passive admin health snapshot/page.

## Ticket-scoped design docs (historical, still authoritative)

- **[ipo-001-domain-score-contract.md](ipo-001-domain-score-contract.md)** — IPO domain tables, offline score contract, fail-closed verdict policy, and typed CRUD boundary.
- **[ipo-002-sebi-filing-ingestion.md](ipo-002-sebi-filing-ingestion.md)** — hardened official-SEBI listing inventory, deterministic filing identity, category-atomic persistence, and recovery semantics.
- **[ipo-003-document-downloader-cache.md](ipo-003-document-downloader-cache.md)** — bounded SEBI PDF retrieval, content-addressed storage, cache provenance, and recovery semantics.
- **[ipo-004-manual-extraction-mvp.md](ipo-004-manual-extraction-mvp.md)** — admin-only complete financial entry, exact page/document/user provenance, immutable revisions, and the raw-data scoring bridge.
- **[ipo-005-ratio-engine.md](ipo-005-ratio-engine.md)** — exact general-company ratios, diagnostic missing-data receipts, raw-input additions, and accounting edge policies.
- **[ipo-006-factor-derivation-and-verdict.md](ipo-006-factor-derivation-and-verdict.md)** — deterministic 0-100 factor bands from typed evidence, the None-vs-zero rule, seven hard caution flags, and the "Insufficient verified data" verdict type.
- **[ipo-007-dashboard.md](ipo-007-dashboard.md)** — the read-only IPO screener page: Streamlit-free snapshot builder, seven spec sections, verdict filter, and the capability-gated re-score action.
- **[ipo-008-screener-orchestration.md](ipo-008-screener-orchestration.md)** — the one-command `run_ipo_screener` pipeline and the inputs-fingerprint idempotency contract.
- **[ipo-009-serpapi-enrichment.md](ipo-009-serpapi-enrichment.md)** — optional low-confidence web signals under strict trust rules: quarantine before storage, keywords-only red flags, graceful no-key skip.
- **[ipo-010-ai-extraction-proposals.md](ipo-010-ai-extraction-proposals.md)** — bounded PDF extraction, deterministic section classification, and the fail-closed AI proposal/review trust model.
- **[scan-run-persistence.md](scan-run-persistence.md)** — SCAN-001 scan-run persistence schema (the column-by-column rationale the Storage LLD links to).
- **[scan-002-handoff.md](scan-002-handoff.md)** — SCAN-002 database-layer implementation handoff brief.
- **[obs-003-audit-log.md](obs-003-audit-log.md)** — OBS-003 audit log + runtime-config schema, recorder design, and the seven tracked events.
- **[valid-001-forward-return-validation.md](valid-001-forward-return-validation.md)** — VALID-001 methodology, no-lookahead rules, and schema rationale.
- **[valid-002-handoff.md](valid-002-handoff.md)** — VALID-002 build brief plus resolved implementation decisions.
- **[rank-001-final-scoring-model.md](rank-001-final-scoring-model.md)** — RANK-001 scoring methodology: the four v1 components, normalization, weighting, score ranges, missing-data behaviour, and the no-hidden-reasons invariant (no schema/migration).
- **[rank-002-handoff.md](rank-002-handoff.md)** — RANK-002 implemented build brief for the `backend/scoring/` scorer (pure components + config + the `run_scan` call + UI sort/components + tests).
- **[auth-003-role-model.md](auth-003-role-model.md)** — AUTH-003 role model: hierarchical viewer/analyst/admin, the capability→min-role map, the database-driven `user_roles` store with an `ADMIN_EMAILS` bootstrap floor, resolution precedence, defense-in-depth enforcement, and denial logging/audit.
- **[auth-003-handoff.md](auth-003-handoff.md)** — AUTH-003 build brief for the `backend/auth/roles.py` policy + `user_roles` table/migration + repository + `require_capability` enforcement + the admin Roles page + tests.
- **[audit-2026-06.md](audit-2026-06.md)** — June–July 2026 codebase audit and hardening register through PR #107: what was found, fixed, rejected, and deferred across both review waves.

## Conventions

- **Boundary**: strategy logic lives in `screeners/`; data/broker/infra plumbing lives in `backend/`.
- **Untrusted evidence**: scraped pages and AI/tool output are treated as evidence, never instructions.
- **Secret-safe**: every log line, UI error, and persisted message passes through the SEC-002 redactor.
- **One persistence schema** serves deterministic and AI screeners via `raw_result_json` / `provenance_json`.

Keep these docs in sync with the code: when a subsystem's interface, schema, or a
key decision changes, update its LLD (and the HLD if the change is system-wide) in
the same PR.
