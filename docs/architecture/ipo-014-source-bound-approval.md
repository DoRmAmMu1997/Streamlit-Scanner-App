# IPO-014: source-bound drafts and manual approval baselines

Status: implemented; competing-claim policy refined after independent review (2026-10-13).

## Decision

HIGH extraction confidence means deterministic host code matched values to PDF
citations. It does not establish current filing authority or permission to replace
newer human evidence. Keep these gates independent.

Add `ipo_scoring_state.manual_revision`, incremented atomically for every manual
header insertion, including approved extraction. Backfill it from immutable manual
history. Preserve general scoring revision semantics: enrichment and subscription
changes invalidate scores but do not change this manual generation.

Capture an issue, selected source, latest manual profile and generation together
under the existing state-first lock before parsing or agent retries. Store the
original generation separately from model-controlled proposal JSON. Existing
proposals retain NULL (unknown), distinct from a trusted empty generation of zero.
Neither a stale nor unknown proposal can be directly approved by a human or bot;
recovery is fresh source-bound manual review. Semantic duplicates remain duplicates.

Final approval and manual save acquire the issue state lock before reading authority
inputs. All source mutations, including cache and filing-date updates, share this
lock. Cache/date-only changes do not increment the scoring revision. PDF hashing
and parsing happen outside database transactions. Failed baseline, source, terms or
review CAS checks roll back both generations, terms and complete manual children.

Automation selects the unique latest dated RHP, or DRHP only without an RHP, from
the complete inventory before checking proposal readiness. Unknown/tied chronology,
a later draft, unknown current manual provenance, conflicting stored terms, or an
unready current document require review. Never fall back to an older queued source.
At most one proposal per issue is attempted in one pass; the final transaction
repeats the same policy. Human review uses an explicit decision for changed terms
or a noncurrent document, but cannot bypass a stale manual generation.

The latest-equivalent term cohort consists of registered DRHP/RHP documents on the
selected current document's exact filing date. Compare supported non-null
`price_band_high` claims only from pending HIGH proposals with no review reasons,
the same known manual baseline, and matching source URL/SHA snapshots. Unequal
Decimal values require human review even if the stored issue cap is NULL. Equal
values and absent claims create no conflict and never fill or erase missing terms.
A strictly older DRHP, rejected history, or stale baseline does not gain authority.
The initial selector and final locked approval use the same pure cohort policy.
Final approval rereads pending proposals under the issue lock; public proposal
submission takes that lock before insertion, so a competing writer either commits
before the fresh read or waits until the evidence transaction has finished.

## Browser drafts

Each draft retains its loaded context across reruns. Its widget namespace includes
issue, document, SHA, manual generation and a monotonically changing draft generation.
Source changes and explicit reload create a fresh namespace; A to B to A cannot
resurrect abandoned input. New sources start blank. Concurrent evidence displays a
warning while preserving the draft and rejecting stale save.

An explicit copy action copies only values, periods and units from the identified
previous revision. It clears citations and requires deterministic field, period,
unit, peer-column and text matching against the selected hash-verified PDF before
save. Ordinary hand-entered evidence keeps the existing human-attestation boundary.

## Alternatives and consequences

Timestamps and profile IDs cannot serve as generations because backdated evidence
still changes authority. Queue order cannot identify the current prospectus.
SELECT FOR UPDATE alone does not serialize SQLite. The existing no-op state UPDATE
works for both supported databases, with PostgreSQL row-lock verification required
separately from SQLite's wider writer lock. Short serialized reads trade throughput
for a coherent snapshot; this remains a single-writer research application.

Migration `20260927ipo014` follows `20260921ipo013`, preserves immutable history,
partial indexes and ownership checks, and can downgrade without rewriting history.
