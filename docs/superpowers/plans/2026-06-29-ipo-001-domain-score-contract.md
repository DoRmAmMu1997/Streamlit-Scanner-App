# IPO-001 Domain Model and Score Contract Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the backend-only IPO domain, persistence, scorecard, and binary verdict contract.

**Architecture:** Keep domain policy in `backend/ipo`, all SQLAlchemy operations in
`backend/storage`, and table creation in one additive Alembic migration. Scoring
is a pure fixed-weight calculation; verdicts fail closed on missing fundamental data.

**Tech Stack:** Python 3.11+, frozen dataclasses, Decimal, SQLAlchemy 2, Alembic, pytest.

---

### Task 1: Domain and score contract

- [ ] Add factor/input/result DTOs and strict enums.
- [ ] Test and implement fixed PDF weights, rounding, missing-data receipts, and JSON output.
- [ ] Test and implement 80/65 bands, confidence, and mandatory-data override.

### Task 2: Persistent schema

- [ ] Add the six ORM models with portable constraints, indexes, and cascades.
- [ ] Add the hand-written IPO-001 migration and update migration drift/table guards.
- [ ] Verify upgrade, downgrade, exact Decimal storage, uniqueness, and cascades.

### Task 3: Repository APIs

- [ ] Add storage-only query/write helpers.
- [ ] Add typed CRUD for issues, documents, financial periods, and subscriptions.
- [ ] Add atomic immutable evaluation create/read/list/latest/delete-pair operations.
- [ ] Verify ownership, ordering, idempotent deletion, secret-safe JSON, and rollback.

### Task 4: Documentation and verification

- [ ] Publish the architecture decision and update the architecture index/HLD/storage LLD.
- [ ] Add a static no-network/no-Streamlit guard for `backend/ipo`.
- [ ] Run targeted and full repository quality/security gates.
- [ ] Run a Codex Security diff review, publish a draft PR, and monitor CI.
