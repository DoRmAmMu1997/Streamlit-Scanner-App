# IPO-002 - SEBI filing ingestion

## Decision

IPO-002 adds a backend-only command that inventories official SEBI DRHP, RHP,
and final-offer listing rows. It creates or updates IPO-001 issue/document facts;
it never downloads a prospectus, parses a PDF, calculates a score, or renders UI.

The three source categories are fixed in code. `backend/ipo/sources/sebi.py` is
the only IPO module allowed to perform HTTP or parse hostile HTML. The headless
orchestrator is `backend/jobs/scan_ipo_filings.py`; transaction policy remains in
`backend/ipo/repository.py`; SQLAlchemy statements remain in `backend/storage`.

## Source contract

`fetch_sebi_filings(category, from_date, to_date)` reads SEBI's AJAX listing
pages and returns frozen `SebiFiling` rows. A row contains the category, SEBI's
filing publication date, visible title, outer filing-detail URL, and category
listing URL.

Nested abridged-prospectus/PDF anchors are ignored. The detail page URL - not a
PDF URL - is stored as `document_url`; the category listing is `source_url`.
These dates are publication dates, not investor application open/close dates.

The client uses HTTPS SEBI hosts only, explicit redirect validation, 5-second
connect and 20-second read timeouts, bounded 2/5/10-second retries, a polite
0.5-second delay between pages, a 2 MiB response limit, HTML content-type checks,
and a 200-page cap. Any malformed filing-like row is parse loss and fails its
category instead of silently producing a partial inventory.

## Identity and lifecycle

Titles are Unicode NFKC-normalized. Filing/addendum/corrigendum markers are
removed from display names; company keys additionally case-fold, normalize
punctuation/whitespace and `&`, and canonicalize common corporate suffixes.
Only an explicit `SME` title token selects `sme`; every other newly discovered
issue remains `unknown` until stronger evidence exists.

The metadata fingerprint is SHA-256 over canonical JSON containing company key,
document type, filing date, and canonical document URL. It is an ingestion
identity, not a PDF content hash.

Issue statuses advance monotonically:

```text
drhp_filed -> rhp_filed -> open -> closed -> listed
```

DRHP, RHP, and final-offer rows target `drhp_filed`, `rhp_filed`, and `closed`
respectively. An older/replayed filing cannot regress a later state.

## Matching and transactions

Issues match by unique nullable `sebi_company_key`. A single unclaimed legacy
row with the same case-insensitive display name may be claimed once; ambiguous
legacy matches are not guessed. Documents match first by unique nullable
`record_hash`, then by canonical URL. A fingerprint or URL already owned by a
different issue is a validation conflict and never reparents the row.

Each category is persisted in one transaction. A conflict rolls back that whole
category, while categories already committed remain durable. The command still
returns nonzero if any fetch, parse, or persistence category fails. Structured
logs and a system audit row record only bounded context and exception class;
response bodies and exception messages are never persisted.

## Polling and recovery

The default upper bound is today. The lower bound is seven days before the
newest stored `filing_date`, deliberately overlapping prior runs; an empty
database starts 30 days back. `--from-date` supplies an explicit inclusive lower
bound and `--full-history` removes it. Re-running a window is idempotent.

After a partial failure, correct connectivity, upstream HTML, or the ownership
conflict and rerun the same command. The overlap and fingerprints safely recover
the failed category without duplicating successful categories.

## Schema and downgrade safety

Migration `20260630ipo002` adds nullable `ipo_issues.sebi_company_key` plus
nullable `ipo_documents.filing_date` and `record_hash`, with unique indexes and
a hash-length check. Existing manual/IPO-001 rows remain valid with NULL values.
It also extends the issue-type check with `unknown`.

Downgrade is allowed for databases that still contain only IPO-001-compatible
data. If IPO-002 identities exist, it refuses before DDL rather than silently
discarding fingerprints or reclassifying `unknown` issues.
