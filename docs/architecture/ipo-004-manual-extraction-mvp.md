# IPO-004 - Manual extraction MVP

## Decision

IPO-004 adds an administrator-only Streamlit form that transcribes a complete
financial profile from one already-cached DRHP/RHP. Every save appends an
immutable revision; corrections never edit earlier evidence.

The revision records the authenticated email, server-generated UTC time,
document URL, filing-record hash, cached-content SHA-256, reported units, every
entered value, and its prospectus page. The source file is rehashed without
network access before submission and its ownership/hash/path are compared again
inside the short insertion transaction.

## Persistence

- `ipo_manual_extractions` is the revision header and holds singleton balance
  sheet, issue, ownership, unit, actor, timestamp, and source-snapshot fields.
- `ipo_manual_financial_periods` contains exactly three annual revenue, EBITDA,
  and PAT rows. The domain requires three distinct dates and every value/page;
  database constraints defend ordinals, uniqueness, revenue, and pages.
- `ipo_manual_peer_valuations` contains one or more normalized peer companies.
  `metrics_json` accepts only EPS, P/E, NAV/book value, RoNW, EV/EBITDA, and
  Price/Sales; values are exact decimal strings, never floats.

Issue deletion cascades through revisions. Document deletion uses `SET NULL`,
but the immutable URL and hashes remain in the header. The guarded downgrade
refuses to drop IPO-004 while any revision exists.

## Data and scoring boundary

Reported monetary/share units are preserved. Frozen records expose exact
`Decimal` conversion to individual INR/shares for downstream callers.
`get_latest_manual_profile(issue_id)` is the raw-data bridge; IPO-004 does not
derive the seven normalized factor scores and does not call `evaluate_issue()`.
IPO-001's weights, missing-data policy, and recommendation bands are unchanged.

IPO-005 subsequently extends new submissions with sourced PBT, finance cost,
total assets, current liabilities, and post-issue shares. Historical IPO-004
revisions remain valid with those additions absent; see
[IPO-005 ratio engine](ipo-005-ratio-engine.md) for compatibility and formulas.

## Authorization and audit

`MANAGE_IPO_DATA` requires `Role.ADMIN`. The app hides the view from lower roles,
the router calls `require_capability`, and the page repeats the admin check.
Actor identity is never a form field. A successful commit emits and best-effort
persists `ipo_manual_extraction_submitted` with ids/counts only; values, text,
paths, and URLs are excluded from audit metadata.

## Failure semantics

- Missing, wrong-owner, final-offer, uncached, symlinked, traversing, oversized,
  or hash-mismatched sources fail before insertion.
- A source edit between cache verification and insertion fails the comparison
  inside the transaction.
- Header, periods, and peers commit atomically.
- Audit failure cannot roll back an already-committed revision.
- Corrections append another revision; latest ordering is `submitted_at DESC,
  id DESC`.

## Explicit exclusions

No PDF parsing, OCR, LLM extraction, automatic download, scheduler, raw-to-factor
formula, or recommendation is added. `ipo_financials` remains unchanged.
