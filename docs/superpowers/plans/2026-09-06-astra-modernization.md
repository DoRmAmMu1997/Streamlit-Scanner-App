# Streamlit Scanner App: approved fixes and modernization

> Approved by the user on 2026-09-06. Execute with isolated worktrees, regression-first development, independent reviews, and verified PR publication. Merging and production deployment remain with the user.

## Mandatory conventions

Preserve the research-tool architecture, Claude integration, strategies, and dark/teal theme. Give new and materially changed functions/classes/complex helpers detailed Google-style docstrings with accurate Args, Returns and Raises as applicable. Include **Beginner note:** paragraphs explaining purpose, assumptions, safety reasoning and trade-offs. Comment non-obvious security checks, transaction boundaries, concurrency controls, numerical edge cases, cache behavior and state transitions. Explain each regression's original failure and protected invariant. Review documentation quality alongside correctness and security in every PR.

Use modern typing and `from __future__ import annotations`; Decimal for money; SQL only in backend/storage; caller-owned transactions; no Streamlit imports in backend; UI imports backend, never app or sibling pages; preserve required app compatibility exports. All database changes include an Alembic migration in the same commit. Preserve unrelated dirty universe CSVs in the shared checkout. Add `Co-authored-by: Codex <codex@openai.com>` to commits and co-authorship in PR bodies.

## Reviewed baseline

Main: `8a8846f1635fcb35e97ea37b36f33aebec75b09c`. Existing PRs:116,117,118,122,123,124. Initial current heads all had six passing hosted checks. Security scan `74be3f46-ace1-48ac-948f-eea33e7eb3ae` reported three findings (two medium, one low), with partial manual coverage; remaining source review is required before final handoff. Parent review's100 focused tests and additional reviewer batches passed, as did Ruff/Bandit; those are baseline evidence, not implementation verification.

## Task 1: existing PR corrections

Retain PR116 source-path fix and source-symbol membership/order; verify relocated DATA_DIR in final image. Retain PR117 Ruff alignment guard and PR123 app coverage with89% floor. Correct PR118 claims that open PRs landed; record remediation links, evidence, limitations, and current documentation.

In PR122 recognize requirement version constraints, extras, markers, comments and normalized project names such as pytest_cov. Test both runtime exclusion and required development declarations.

In PR124 read each CSV once; represent valid, missing, unreadable and legacy-unknown observations. Invalid observations warn and cannot replace the last valid baseline. Preserve25-name cap, record truncation and suppress exact membership-change claims when either snapshot is incomplete. Select newest valid row per universe in SQL ordered by captured time and ID. Inject health checking/test sessions so mocked scan tests never touch configured application storage. Ship migration with schema changes. Test first check, corrupt/missing/recovered data, truncated names, equal times and long histories. Explain baseline ownership, false recovery and incomplete membership in beginner notes.

## Task 2: transcript destination security

Disable automatic redirects; validate destinations before requests, allowing at most3 redirects. Reject unsafe addresses, invalid locations, loops and unsupported schemes. Bind connections to validated public DNS addresses while preserving hostname TLS verification; prevent ambient proxies bypassing policy. Preserve streamed size/PDF validation, cleanup and safe failures. Test zero requests to blocked targets, legitimate redirects, DNS/connection binding, proxy isolation and transport failures. Explain why response validation after a request is too late.

## Task 3: PDF containment

Move both transcript extractors into killable child processes, preserving first30 pages and40,000 characters. Enforce60 seconds,512MiB memory and bounded serialized output. Reuse IPO worker design and add Windows Job Object memory enforcement alongside Linux limits. Apply limits before parser imports/execution; failure is safe unavailability, never an in-process fallback. Test primary/fallback, stalls, output/memory budgets and successful bounded extraction. Explain compressed input versus expansion and thread versus process containment.

## Task 4: analysis authorization and SDK contracts

Pass trusted current role/identity through scan output to Fundamentals; gate controls and recheck RUN_SCAN before agent construction/execution. Viewers retain read-only cached verdicts. Explicitly disable built-in SDK tools for MCP-only agents while preserving their intended tools. Give IPO runner typed usage-limit, failed-result, missing-CLI and process-failure handling. Failed SDK text must not enter proposal/verdict parsing. Test demotion with retained state, fallback Viewer, authorized calls, error/quota outputs and tool configuration. Explain authentication/action authorization/tool availability/tool approval distinctly.

## Task 5: candle history preservation

Preserve cached rows outside the requested interval and overlay that interval with fresh vendor data. Preserve conflicting rows inside the fresh response for quarantine. Return only requested slice; empty response preserves cache. Publish atomically; serialize per-cache-file read/merge/write across threads/processes. Derive vendor-earliest evidence from raw response, never merged cache. Preserve unreadable files for repair. Test narrow/forced windows, outside-range preservation, concurrent writers, interrupted writes, empty data and sidecar evidence. Explain requested window, durable full cache and vendor evidence.

## Task 6: forward-return processing

Return detached work items listing unresolved stock horizons and pending benchmark work. Preserve terminal stock receipts via conditional writes, including concurrent completion. Validate raw candles before dropping/deduping; malformed bars must not shift entry/horizon. Service treats fatal stock data as retryable; pure calculators report unavailable. Benchmark failure remains retryable. Fetch stock/benchmark only through as_of; future signal causes no invalid request.

Service accepts typed SessionFactory: short read, external fetching/calculation without open transaction, per-signal atomic persistence. Add last_attempted_at and benchmark_retry_pending in the same migration. Select oldest attempted unresolved work with deterministic ties, max500 distinct signals/invocation. Benchmark-only retries preserve all stock fields; no configured benchmark exits retry queue. Validate positive integral horizons/limits, reject bool/fractional/zero/negative inputs, ordered deduplication, empty-horizon no-op. Preserve and report earlier commits after later failure.

Tests cover mixed terminal/pending and every failure path, concurrent terminalization, malformed entry/intermediate/exit data and optional volume, valid holiday gaps, no future requests, independent DB writer during network calls, atomic per-signal writes, fair small batches, benchmark-only recovery, direct input contracts and migrations. Explain terminal immutability, fairness, benchmark-only work and short transactions. Add validation ADR.

## Task 7: ranking and finite numbers

Filter cached candles by snapshot date before risk/liquidity. Missing/untrustworthy dates omit those components and keep weight renormalization. Share finite-number validation across ranking/comparison/notifications/price conversion; reject NaN/infinity. Version corrected ranking provenance; preserve stored history. Test future-cache append invariance for score/order/receipts and invalid/missing inputs. Explain lookahead and finite-value checks.

## Task 8: IPO current evaluation and freshness

Add ipo_scoring_state(issue_id,input revision,current score ID,evaluated revision,last verification time). Backfill newest complete historical evaluation as unverified. Increment revision transactionally for scoring-input writes. Assemble consistent snapshot; publish conditionally on matching revision. Roll back/retry at most3 times, then typed retryable conflict. Preserve unique fingerprints and immutable history. A->B->A must select existing A. Distinguish calculation and verification times. Freshness compares fingerprints/model/time-derived rules (GMP expiry, near-close demand). Invalidate after writes/time boundaries. Stale verdicts are historical and excluded from actionable recommendations.

Tests: A->B->A, concurrent identical idempotency, old-snapshot rejection, changing snapshot retry, repeated conflict, time-only expiry and unchanged refreshed evidence. Document current selection separately from immutable content, revision checks, retries and semantic freshness. Add ADR and migration.

## Task 9: IPO evidence approval/editing

Autoapprove at most one current candidate/issue: newest dated RHP, otherwise newest dated DRHP. Ambiguous chronology/conflicting terms/legacy missing baseline require human review. Capture manual revision at proposal creation; atomically recheck approval; intervening edits make it stale. Preserve hash/citation/ownership/CAS checks. Bind editing to issue, document and loaded revision; reset source-bound fields when source changes. Copy previous values explicitly and reverify citations against new source. Tests prove older DRHP cannot supersede RHP, stale proposals cannot overwrite corrections, and source changes cannot relabel evidence. Explain why confidence is not freshness or overwrite authority.

## Task 10: code and dependency maintenance

Share evidence-reference validation (safe URLs, full SHA256, redacted160-character labels, consistent cache rejection). Type touched session factories. Move IPO orchestration into named service modules with compatibility contracts. Remove unused export_module_compat and correct docs. Replace wall-clock sleeps in tests with deterministic synchronization. Pin/audit optional indicator dependencies on supported Python versions; test accelerated paths, warmups, missing values and documented differences. Pin Actions to reviewed commit SHAs; add weekly dependency updates; isolate dependency upgrades from behavior changes. Update living HLD/LLDs/screener/role/CI/security docs. Document shared contracts and callers' responsibilities.

## Task 11: database verification

Migration comparison includes checks, unique constraints, index columns/predicates, primary keys, defaults and foreign keys. Add disposable PostgreSQL integration tests. Deployment smoke performs application migrations plus representative repository operations; listener health alone is insufficient. Test upgrades from existing data and single Alembic head. Explain each schema guard and drift fixture.

## Task 12: Streamlit workflows

Group sidebar navigation into Research,IPO,Administration with stable identities/capability gates. Show actual snapshot dates, cached/live status, partial coverage and recovery guidance. Proposal review uses grouped tables (current/proposed value,unit,page,verification,differences), raw JSON advanced expander. Add selected/stale rescore actions, progress, safe per-company failure details, accurate success/partial/failure feedback and retry failed only. Preserve useful selections; clear stale forms/reviewed proposals. Preserve theme/native controls and keep implementation details out of product copy.

## Verification and delivery

Use regression-first work and independent spec/quality/security/doc reviews. Run pre-commit validation, pytest with app/backend/screeners/ui coverage>=89%, compileall,Ruff,mypy,Bandit,pinned audits,Python3.11/3.12,Docker/Compose,Postgres and Windows worker checks as applicable. Browser-test with isolated fixtures; no real provider calls or production mutations. Reconcile dependency/base changes and rerun affected gates.

Serialize schema-changing worktrees into a documented migration chain and test all heads in a separate integration worktree. Publish followups/new PRs with co-authorship, findings, tests and dependency links. Complete remaining security-source review on the integrated revision; repeat tech-debt/general review and report remaining issues honestly. Do not silently rewrite historical records. Do not merge or deploy. User universe edits stay untouched.
