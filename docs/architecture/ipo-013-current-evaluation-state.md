# IPO-013: current evaluation and semantic freshness

Status: Accepted for the approved modernization Task 8, 2026-09-22.

## Decision

Keep scores and recommendations immutable. Add one `ipo_scoring_state` row per
issue containing a monotonic `input_revision`, nullable `current_score_id`,
`evaluated_revision`, and `last_verified_at`. Historical newest and current are
different: A -> B -> A reselects the original A receipt without changing its
calculation time or duplicating history. Migration `20260921ipo013` follows
`20260909valid005`; backfill selects the newest complete pair by calculation time
then score id, but never certifies legacy history as verified.

## Transactions and shared review boundary

Every actual scoring-input writer atomically increments the state revision in
the caller's transaction before changing input rows. The state row is always
locked before subordinate rows. `lock_ipo_scoring_state(session, issue_id,
expected_revision=None)` is the shared serialization interface for downstream
manual-review work. Its no-op SQL UPDATE holds the lock until caller commit.
The downstream manual-only baseline must remain distinct from input_revision:
subscription and GMP refreshes must not pretend to be manual edits.

Read scalar state before and after detaching every input and eager child.
SQL column reads bypass the identity map; a changed token discards the bundle,
including A -> B -> A mutations during assembly. Read-time selection metadata
belongs to that same bundle. Derive factors outside the read transaction.
Publication starts a new transaction and conditionally locks the captured
revision before inserting/reusing a complete owned score pair. The pointer,
verified revision and verification time commit together. This real outer UPDATE
also encloses SQLite savepoints, so late failure rolls back inserted history.
There are at most three total attempts across snapshot and publication conflicts.
Unrelated database/validation failures retain their original meaning.

## Freshness and compatibility

Current means a complete issue-owned pair, verified matching revision, present
verification time, and model/fingerprint matching current inputs at the read
clock. Canonical source URLs enter the new versioned fingerprint. Raw time and
the mutation token never enter semantic identity. GMP is valid at exactly five
days and expires immediately after; near-close retains its existing UTC date
rule. Explicit aware `as_of` freezes evaluation time; production checks the
time-derived fingerprint again before publication. Verification time is actual
UTC wall time and remains independent of the immutable calculation time.

Current dashboard snapshots rebuild each render with one aware UTC instant.
Display activity (proposals/cache downloads) may change last_updated without
invalidating scoring. Stale receipts remain visible as history but cannot enter
actionable recommendation sections or screener ratings. Missing manual evidence
does not fabricate a score or certify an old pointer. Legacy evaluation APIs
remain historical and cannot certify arbitrary caller-supplied scorecards.

## Alternatives and validation

Timestamp comparisons miss semantic reuse, clock expiry and concurrent commits.
Rewriting receipts destroys audit history; deleting uniqueness creates duplicates.
Fixed UI cache TTLs cannot prove freshness after another session writes.
The selected design uses existing transaction scopes and unique/savepoint logic.

Regression coverage must include semantic return to A, refresh without duplicate
history, all storage writers and rollback, snapshot/publication interleavings,
three-attempt exhaustion, ownership, migration retention, clock boundaries,
uncached actionable reads, and deterministic same-input concurrency.
