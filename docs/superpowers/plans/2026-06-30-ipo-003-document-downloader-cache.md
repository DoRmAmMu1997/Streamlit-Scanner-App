# IPO-003 Document Downloader and Cache - Execution Checklist

> Use `superpowers:executing-plans`, `superpowers:test-driven-development`, and
> the Codex Security diff-scan workflow.

## 1. Schema and typed contracts

- [x] Add cache provenance/status columns, portable constraints, ORM parity,
  legacy defaults, and guarded downgrade behavior.
- [x] Keep IPO-002 `record_hash` distinct from file `content_sha256`.
- [x] Add frozen parse-status and download result/error contracts.

## 2. Secure download and cache

- [x] Validate every SEBI URL, public DNS answer, and manual redirect hop.
- [x] Bound retries, timeouts, HTML/PDF bytes, and resource lifetimes.
- [x] Resolve exactly one official iframe PDF and ignore abridged links.
- [x] Stream to a temporary file, hash incrementally, fsync, and atomically rename.
- [x] Verify cache hits and reject traversal, symlinks, and corrupt bytes.

## 3. Repository, audit, and configuration

- [x] Keep HTTP outside database transactions and SQL inside storage helpers.
- [x] Persist success/failure states and invalidate provenance on source edits.
- [x] Compare-and-set source identity after HTTP so concurrent corrections win.
- [x] Add structured lifecycle events, safe durable failure audits, and DATA_DIR paths.

## 4. Teaching pass and delivery

- [x] Add beginner-friendly docstrings/comments across IPO-001/002/003 and tests.
- [x] Update HLD, LLD, storage, security, observability, configuration, and operations docs.
- [x] Run focused/full local quality, security, and migration gates.
- [ ] Run Docker/Compose/PostgreSQL gates in hosted CI (Docker is unavailable locally).
- [x] Run Codex Security diff review and harden both suppressed observations.
- [ ] Commit with Codex co-authorship, push, open a draft PR, and monitor CI.
