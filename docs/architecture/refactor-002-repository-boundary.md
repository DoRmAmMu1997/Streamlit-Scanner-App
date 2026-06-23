# ADR: A single repository layer is the only database-access boundary (REFACTOR-002)

**Status:** Accepted
**Date:** 2026-06-23
**Deciders:** Codex (ticket owner), Claude (reviewer / implementer)

> Beginner note: an *Architecture Decision Record* (ADR) is a short, durable note
> that says "we made this design choice, here's why, and here's what it costs us
> later." It exists so the next person (or AI agent) doesn't have to re-derive the
> reasoning from the code.

## Context

REFACTOR-002 asked us to *create* `backend/storage/repository.py` with
`create_scan_run`, `finish_scan_run`, `save_scan_results`, `get_latest_scan_runs`,
`get_scan_results`, and `record_audit_event`, so that:

- the UI never writes SQL directly,
- the scan service does not know database internals, and
- the repository methods are tested.

When the work was picked up, exploration found that **the repository layer already
exists and already satisfies all three goals.** It was built incrementally across
SCAN-001/002/003, OBS-003, VALID-001/002, and RANK-001/002:

- `backend/storage/repository.py` (~1,035 lines) already exposes every method the
  ticket lists. The ticket's `record_audit_event` is the higher-level wrapper in
  [`backend/audit/recorder.py`](../../backend/audit/recorder.py), which records the
  structured log event and then calls the repository's `create_audit_log_entry`.
- A static sweep of `app.py`, `backend/` (outside `storage/`), `screeners/`, and
  `ui/` found **no** raw SQL, engines, or sessions. The only SQLAlchemy references
  in those layers are `from sqlalchemy.orm import Session` (a type hint for a
  caller-owned session) and `from sqlalchemy.exc import OperationalError` (so the
  UI can show "database unavailable" instead of crashing).
- The methods are covered by `tests/test_scan_storage_repository.py` and
  `tests/test_audit_repository.py`.

The force at play: with ~25 worktrees and two AI agents committing ticket-by-ticket,
the *real* risk is not "the layer doesn't exist" — it is **regression**: a future
change quietly adding `session.execute(text(...))` to a Streamlit page, or a new
service building its own engine, eroding the boundary one PR at a time.

### The boundary (system-design view)

```
ui/ ─┐
backend/scanning/  ─┤
backend/validation/ ─┤  call typed helpers, receive plain values / DTOs
backend/notifications/ ─┤        (never build SQL, never open a connection)
app.py ─┘
          │
          ▼
  backend/storage/  ◄── the ONLY module allowed to import SQLAlchemy query
   repository.py         builders, create engines, and open sessions.
   database.py           Sessions are created here (session_scope / SessionLocal).
   models.py
          │
          ▼
      SQLite / Postgres
```

One deliberate, already-documented rule shapes this boundary
([`repository.py:9-12`](../../backend/storage/repository.py)): **the repository
never opens its own `Session`; the caller owns the transaction.** That is what lets
the scan service wrap "create run → run screener → save results → finish run" in a
single atomic transaction. So services legitimately *hold* a `Session` and pass it
in — they just must not *build SQL* with it.

## Decision

Treat REFACTOR-002 as **already delivered** by the existing repository layer, and
**lock the boundary in with an automated guard** instead of rewriting working code.

Add `tests/test_repository_layer_boundary.py`: a standard-library `ast` check (no
new dependencies) that fails CI if any module under `app.py` / `backend` (excluding
`backend/storage`) / `screeners` / `ui`:

1. imports from the top-level `sqlalchemy` package (where `select`/`text`/
   `create_engine`/… live);
2. imports an engine/session/table factory from a SQLAlchemy submodule
   (`sessionmaker`, `scoped_session`, `create_engine`, `MetaData`, `Table`); or
3. calls the legacy `session.query(...)` / `session.execute(...)` API on a
   session-like receiver (matched by name, so pandas `frame.query(...)` is not a
   false positive).

`from sqlalchemy.orm import Session` and `from sqlalchemy.exc import …` stay allowed.

Do **not** modify `repository.py` or any caller; add no tables, migrations, or
dependencies.

## Options Considered

### Option A: Add a regression guard test (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — one ~190-line test, stdlib `ast` only |
| Cost | One fast static test in the existing suite |
| Scalability | Scales to every new module for free |
| Team familiarity | High — mirrors `tests/test_supply_chain_policy.py` |

**Pros:** Delivers REFACTOR-002's real intent (no raw DB access *and* it stays that
way); zero risk to working code; green on the current tree; clear failure message.
**Cons:** Static/heuristic — can't catch a session smuggled through a dynamically
named variable. Acceptable: the import checks catch every way to *construct* SQL.

### Option B: Recreate `repository.py` as the ticket literally says
| Dimension | Assessment |
|-----------|------------|
| Complexity | High |
| Cost | Re-deriving ~1,035 tested lines |
| Scalability | n/a |
| Team familiarity | n/a |

**Pros:** Matches the ticket text verbatim.
**Cons:** Destructive — overwrites mature, tested code that many tickets depend on;
risks regressions across scan/audit/validation/config; pure waste. Rejected.

### Option C: Close the ticket with a doc note only, no guard
**Pros:** Lowest effort. **Cons:** Leaves the boundary unprotected against the exact
regression the multi-agent workflow makes likely. Rejected as the primary action,
but the documentation half is kept (this ADR + the audit-register entry).

## Trade-off Analysis

The honest tension is "the ticket says *create a file*" vs. "the file already
exists and is correct." Following the letter of the ticket (Option B) would destroy
value; following its *intent* — "avoid raw database access throughout the app" —
means making the already-good state permanent and verifiable. A guard test is the
cheapest mechanism that converts a one-time review finding into a standing
invariant, exactly as `test_supply_chain_policy.py` did for dependency pins.

## Consequences

- **Easier:** Reviewers no longer have to eyeball every PR for stray SQL; the gate
  does it. New contributors get a precise, actionable failure with remediation.
- **Harder:** A genuinely new persistence pattern that *must* live outside
  `backend/storage` (none is foreseen) would need an explicit, reviewed exemption
  in the test's allow-list — friction by design.
- **Revisit when:** the storage package is split or relocated (update the exempt
  path), or a deliberate architectural change introduces a second data store.

## Action Items

1. [x] Add `tests/test_repository_layer_boundary.py` (guard + self-test).
2. [x] Record the closure in [`audit-2026-06.md`](audit-2026-06.md).
3. [ ] If a future store is added, extend the exempt list with a reviewed rationale.
