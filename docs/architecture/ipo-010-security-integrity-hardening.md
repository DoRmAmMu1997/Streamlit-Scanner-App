# IPO-010 security and integrity hardening

| | |
|---|---|
| **Status** | Accepted |
| **Date** | 2026-07-18 |
| **Decision owners** | IPO subsystem maintainers |
| **Applies to** | IPO-006 through IPO-010 implementation in PR #108 |
| **Related** | [IPO-006](ipo-006-factor-derivation-and-verdict.md) · [IPO-008](ipo-008-screener-orchestration.md) · [IPO-009](ipo-009-serpapi-enrichment.md) · [IPO-010](ipo-010-ai-extraction-proposals.md) |

## Context

The IPO screener combines three kinds of evidence with very different trust:
official prospectus bytes, administrator-approved manual facts, and optional
AI/web observations. PR #108 correctly keeps AI proposals outside scoring
until approval, but its first implementation leaves three structural gaps:

1. PDF limits are applied after parser work has already materialized pages,
   tables, and text in the long-lived screening process.
2. Numeric value, unit, period, and citation are validated as separate fields,
   so individually valid fields can describe different source facts.
3. SerpAPI data is documented as advisory but can become a hard caution, while
   one quarantined item can suppress clean sibling observations.

The same review also found proposal freshness/transaction races and
idempotency drift from fingerprints that include volatile database ids rather
than the semantic evidence actually scored.

## Decision

### 1. Isolate hostile PDF work

`extract_document_pages()` remains the caller-facing facade. Production calls
run pdfplumber in a short-lived, spawn-safe child process. The child enforces
page, table, row, column, cell, glyph/text, and serialized-result budgets while
building its result. The parent enforces a 60-second wall-time limit, validates
the bounded wire result, and terminates the child on timeout, crash, or malformed
output.

The default limits are:

| Dimension | Limit |
|---|---:|
| PDF bytes | Existing downloader limit: 50 MiB |
| Wall time | 60 seconds |
| Pages | 800 |
| Tables per page | 20 |
| Rows per table | 250 |
| Columns per row | 50 |
| Cells per document | 100,000 |
| Characters per cell | 200 |
| Characters per page | 20,000 |
| Characters per document | 2,000,000 |
| Serialized child result | 16 MiB |
| Linux child address space | 512 MiB |

Linux applies the address-space limit before opening the PDF. Windows has no
new runtime dependency: wall time plus object/text/result limits are the
portable containment boundary. The absence of a Windows hard RSS limit is an
accepted residual risk, not a reason to leave parsing in the parent.

### 2. Make citations atomic evidence

A proposed financial value is promotable only as a typed cited fact containing
the exact finite `Decimal`, currency/unit multiplier, fiscal period, source
document SHA-256, 1-based page, original table cell or text-token identity, and
the exact printed token.

Verification parses complete tokens within their original cell/text span.
Allowed normalization is limited to formatting-equivalent notation: Indian or
western grouping separators, currency prefixes, whitespace, parentheses for a
negative value, an explicit plus sign, and insignificant trailing decimal
zeros. Rounding, substring membership, and cross-cell concatenation never
prove a citation. Units must be cited in the same table/header or bounded text
context. Missing or ambiguous binding is human-review required and cannot
receive high confidence.

Exactly three distinct fiscal-year ends are required in strictly oldest-first
order. Each adjacent pair must be 365 or 366 days apart; duplicate, reversed,
or nonannual periods are rejected.

### 3. Encode web authority and quarantine per item

Each enrichment result carries:

- authority `advisory_web`;
- status `usable` or `quarantined`;
- a secret-safe quarantine reason;
- a semantic content fingerprint; and
- optional official/manual corroboration references.

The batch state is `usable`, `partial`, or `not_evaluable`. Clean items in a
mixed batch remain available; zero usable items requires human review and is
never interpreted as a clean negative result.

Advisory web evidence may add the existing bounded GMP contribution or request
review. It cannot directly create a hard caution. Litigation/auditor hard
cautions require official or approved-manual corroboration. GMP parsing accepts
a rupee or percent value only within 40 characters of `GMP` or
`grey market premium`.

Semantically identical observations are upserted by content fingerprint. The
first and last seen instants are both retained, and freshness uses last seen.

### 4. Make review and scoring transitions atomic and semantic

Proposal submission and approval compare the recorded source SHA-256 with both
the current document row and the verified cached bytes. Approval inserts the
manual revision and children and compare-and-set transitions the proposal in
one caller-owned transaction. A lost review race rolls everything back.

The database enforces one pending proposal per document. A proposal fingerprint
binds source SHA, extraction schema/model versions, agent model, and canonical
payload. Normal extraction skips any unchanged historical attempt. A forced
run may bypass reviewed history but cannot create a second pending proposal or
persist an identical regenerated payload.

Pending proposals block document deletion. Reviewed proposals survive document
deletion through their frozen URL/SHA snapshot and a nullable `SET NULL`
document reference.

Scoring materializes one immutable `IpoFactorInputs` snapshot and fingerprints
that exact object. The fingerprint contains semantic values, source digests,
ratio receipts and formula versions, model/policy versions, and derived
freshness/near-close states; it excludes database row ids. A partial database
unique index closes concurrent evaluation insertion races.

### 5. Preserve deterministic and public behavior

The 25/20/15/15/10/10/5 weights, 80/65 recommendation bands, binary verdict,
critical-missing fail-closed rule, and five-point maximum GMP influence remain
unchanged.

The public result adds a seven-entry breakdown receipt. Each entry carries the
factor, weight, normalized score or missing state, contribution, and evidence
reason. Existing result keys remain compatible. Legacy rows without the new
receipt reconstruct the numeric portion from stored contributions and are
identified as legacy rather than silently upgraded.

High debt is fail-closed unless a structured, page-cited purpose state is
affirmatively `debt_reduction`. Negated, ambiguous, missing, or legacy free
text cannot suppress the caution.

## Alternatives considered

### In-process parser guards only

Progressive guards are still required inside the parser, but they cannot
terminate a native parser call that hangs or allocates before returning.
Keeping the long-lived worker and parser in one failure domain was rejected.

### Patch each value/unit/enrichment consumer independently

Local comparisons would close the current reproductions but leave the
cross-field and source-precedence invariants as caller conventions. Typed facts
and one authority policy were selected so later consumers cannot recreate the
same class of bug.

### New parsing or enrichment service

A network service would create deployment, authentication, and operational
work disproportionate to this offline sprint. A local process boundary and
versioned domain records provide the required isolation without a new service.

## Migration and compatibility

- New JSON/status/fingerprint fields are additive and versioned.
- Legacy pending extraction proposals are review-required; they are not granted
  the new confidence semantics.
- Existing reviewed evidence and evaluations remain immutable and readable.
- Existing enrichment rows default to advisory, uncorroborated, and
  `first_seen_at == last_seen_at == captured_at`.
- Database uniqueness is partial so legacy null fingerprints remain valid.
- `extract_document_pages()` and result JSON keys remain compatible; new
  arguments and fields are additive.

## Verification

The change is accepted only when regression tests demonstrate all eight
reviewed failure cases no longer reproduce, legitimate cited values and clean
mixed enrichment still work, proposal/evaluation races are atomic, exact reruns
are idempotent, the dashboard exposes seven-factor provenance without network
work, and every repository quality/security/container gate passes.

## Process note

The repository normally uses one ticket per PR. PR #108 already combines
IPO-006 through IPO-010 and the user explicitly chose to keep that shape. This
ADR records that one-off waiver; it does not change the standing convention.
