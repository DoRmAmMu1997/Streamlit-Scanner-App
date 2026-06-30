# IPO-002 SEBI Filing Ingestion Implementation Checklist

> **For agentic workers:** Use `superpowers:executing-plans`,
> `superpowers:test-driven-development`, and the repository security-review workflow.

**Goal:** Inventory official SEBI DRHP, RHP, and final-offer listings into the IPO-001
issue/document schema without downloading or parsing PDFs.

**Architecture:** Keep hostile-network handling and HTML parsing in
`backend/ipo/sources/sebi.py`, SQLAlchemy statements in `backend/storage`, typed
transaction orchestration in `backend/ipo/repository.py`, and the command boundary in
`backend/jobs/scan_ipo_filings.py`. Each category is fetched and committed independently.

**Tech stack:** Python 3.11+, frozen dataclasses, Requests, Beautiful Soup, SQLAlchemy 2,
Alembic, pytest.

## 1. Schema and contracts

- [ ] Add the `unknown` issue type and nullable SEBI identity/fingerprint columns.
- [ ] Add unique indexes, hash validation, migration upgrade/downgrade, and ORM parity.
- [ ] Add frozen source, normalized filing, and ingestion-summary contracts.
- [ ] Preserve existing nullable/manual IPO-001 rows.

## 2. Source adapter

- [ ] Parse listing date, outer detail URL, and title while ignoring nested PDF anchors.
- [ ] Normalize display names/company keys and derive deterministic record fingerprints.
- [ ] Implement fixed endpoints, validated redirects, bounded retries/timeouts/delays,
  content-type and 2 MiB response limits, and a 200-page cap.
- [ ] Treat malformed/non-empty pages as parse loss instead of silent success.

## 3. Persistence and command

- [ ] Match/claim issues conservatively and advance status monotonically.
- [ ] Match documents by fingerprint then URL; reject cross-issue ownership conflicts.
- [ ] Make repeat ingestion idempotent and each category transaction atomic.
- [ ] Add watermark-based default dates, CLI flags, summaries, partial-success exit codes,
  structured lifecycle logs, and durable secret-safe failure audits.

## 4. Documentation and delivery

- [ ] Update architecture/index/storage and operations documentation.
- [ ] Update boundary guards so only `backend/ipo/sources` may use networking.
- [ ] Run focused tests and every repository quality/security/deployment gate.
- [ ] Run a Codex Security diff scan and remediate validated findings.
- [ ] Commit with Codex co-authorship, push, open a draft PR, and monitor hosted CI.
