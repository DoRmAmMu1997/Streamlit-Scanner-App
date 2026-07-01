# IPO-004 Manual Extraction MVP Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> `superpowers:executing-plans` and `superpowers:test-driven-development` task by
> task. This ticket is intentionally executed inline because shared-checkout
> subagents are not in scope.

**Goal:** Add an auditable administrator form for complete manual IPO evidence.

**Architecture:** Three normalized immutable tables retain source-reported
values and page provenance. A framework-free repository validates cached bytes
outside a transaction, rechecks source ownership inside a short transaction,
and returns detached frozen records to Streamlit.

**Tech stack:** Python, Streamlit, SQLAlchemy 2, Alembic, SQLite/PostgreSQL,
pytest, Decimal.

## Checklist

- [x] Refresh `main`, prune stale worktree metadata, and create the isolated
  `feat/ipo-004-manual-extraction-mvp` worktree.
- [x] Write failing unit-conversion/completeness tests and implement frozen
  manual-extraction DTOs.
- [x] Write failing repository tests and implement cached-source verification,
  immutable submission, latest/list/get reads, and safe audit emission.
- [x] Write failing migration tests and implement `20260701ipo004` with ORM
  parity, foreign-key indexes, checks, cascades, and guarded downgrade.
- [x] Write failing role/router/page tests and implement `MANAGE_IPO_DATA` plus
  the Streamlit entry form, prefill, peer editor, and revision history.
- [x] Update architecture, security, observability, authentication, UI, storage,
  and operations documentation plus teaching-policy coverage.
- [x] Run focused/full quality, security, migration, and rendered-browser
  verification. Docker is unavailable on this workstation, so the image and
  Compose smoke gates were verified by hosted CI.
- [x] Commit with Codex co-authorship, push, open draft PR #85, and monitor the
  Docker plus Python 3.11/3.12 hosted checks to a green result.
