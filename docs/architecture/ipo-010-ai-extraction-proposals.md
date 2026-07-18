# IPO-010 - Automated PDF extraction as fail-closed review proposals

## Decision

IPO-010 is the parse stage IPO-003 deferred, split into three trust tiers:

1. **Deterministic extraction** — `backend/ipo/documents/table_extractor.py`
   opens one hash-verified cached PDF (pdfplumber, lazily imported, with an
   injectable seam) and returns 1-based `ExtractedPage`/`ExtractedTable`
   receipts under hostile-content caps (800 pages, 20k chars/page, 20
   tables/page, 200 chars/cell). Structural problems become stable
   `IpoDocumentParseError` codes (`unreadable_pdf`, `page_limit_exceeded`,
   `empty_document` — the scanned-PDF signal); oversized documents are
   rejected rather than truncated because truncation would silently
   invalidate page citations.
2. **Deterministic classification** — `documents/section_classifier.py`
   assigns pages to DRHP/RHP section families by reviewed anchor phrases
   (plain casefolded substring hits, catalog-order tie break). Unmatched
   pages land in an explicit OTHER bucket, never a guessed section.
3. **The agent** — `backend/ipo/agents/financial_extractor.py` runs a
   locked-down Claude Agent SDK loop (`permission_mode="dontAsk"`,
   `setting_sources=[]`, `max_turns=8`) whose only tools are three
   in-process readers over the classified pages: `list_sections`,
   `read_section`, `read_tables`. The model never sees a file path and can
   fetch nothing.

## The trust boundary is host code

- Every excerpt handed to the model passes the shared prompt-injection
  quarantine first; a hit hands the model the blocked-evidence response,
  collects the raw text in a request-local ContextVar, and fails the run
  non-retryably after the loop (re-reading the same document cannot help).
- The final message must be a single JSON object matching a strict Pydantic
  schema that mirrors `IpoManualExtractionData` field-for-field — values as
  decimal *strings* (no float drift), every value paired with a 1-based page
  citation, extra keys rejected. Malformed output earns one bounded retry
  via the shared `parse_with_retry`.
- **Independent verification:** every financial value becomes a
  `CitedFinancialFact` binding exact finite Decimal, unit multiplier, period,
  document SHA, page, original table cell/text span, source token, confidence,
  and reasons. Complete-token equality permits only formatting normalization;
  rounding, substring, and cross-cell matches fail. The unit must be cited in
  the same table/header or bounded text context, and three annual periods must
  be distinct and oldest-first. The agent may honestly report
  `value_not_found` instead of guessing.

## Proposals, never evidence

A verified draft is stored as a **pending row in `ipo_extraction_proposals`**
(payload, confidence, verifier notes, agent/model provenance, source
SHA-256, and a semantic payload fingerprint). A partial unique index enforces
one pending proposal per document. Scoring never reads this table. In the
admin page's review section, **Approve** reconstructs the strict manual
contract from the payload and replays `submit_manual_extraction` — the
reviewer attests as `entered_by_email` and the cached PDF bytes are
re-hashed — producing a revision indistinguishable from hand-entered
evidence; **Reject** stores an attributable, reasoned, redacted record. The
lifecycle (pending rows carry no reviewer; approved rows must link their
revision) is enforced by CHECK constraints. Batch callers receive typed
`IpoExtractionErrorReceipt` values, never exceptions.

Approval rehashes current cached bytes. Strict reconstruction, the immutable
revision/children, and the proposal compare-and-set commit in one transaction.
Normal extraction skips unchanged reviewed history before AI.
`--force-extract` may revisit reviewed history, but pending or identical
payloads still skip. Pending proposals block document deletion; reviewed rows
preserve URL/SHA provenance through a nullable document reference. Legacy
pending rows without cited facts remain review-required.

## Deliberate deferrals

- `ipo_documents.parse_status` keeps its IPO-003 vocabulary; the proposals
  table is the extraction-state ledger. Reworking the grouped download-
  metadata CHECK for parsed/parse-failed states was judged not worth the
  migration risk in this change.
- OCR for scanned prospectuses: `empty_document` receipts make the gap
  visible; an OCR pass can slot in behind the same extractor interface.

## Testing

`tests/test_ipo_table_extractor.py` (caps, error codes, plus a true
pdfplumber integration read over a byte-accurate PDF assembled in-test — no
binary fixture in the repo), `tests/test_ipo_section_classifier.py`,
`tests/test_ipo_financial_extractor.py` (verification tiers, bounded retry,
quarantine non-retry, receipt codes), and
`tests/test_ipo_extraction_review.py` (approve == manual revision round
trip, double-review guards, reject audit trail).

## PR #108 hardening addendum

The initial implementation detail above is superseded where it conflicts with
the accepted
[security and integrity ADR](ipo-010-security-integrity-hardening.md):

- `extract_document_pages()` now supervises a short-lived spawned worker.
  Parent wall time/cleanup and child page/table/row/column/cell/glyph/text,
  serialized-response, and Linux address-space budgets return typed success or
  review-required receipts; oversized work is rejected, never truncated.
- A recognized section heading owns continuation pages until the next heading.
  Pages before the first recognized heading stay `OTHER`, and page-safe chunks
  repeat provenance markers without splitting citation tokens.
- Numeric substring/rounded/cross-cell matching is forbidden. Each verified
  financial value is a `CitedFinancialFact` binding exact Decimal, unit,
  period, document SHA, page, original cell/text span, source token,
  confidence, and reasons. Units share the value context, and exactly three
  distinct annual periods must be oldest-first.
- A partial unique index enforces one pending proposal per document. The
  semantic fingerprint binds source SHA, schema/model versions, agent model,
  and canonical payload. `--force-extract` bypasses reviewed-history pre-skips
  only; pending and identical payloads still skip.
- Approval re-verifies current row and cached bytes, inserts the revision and
  children, and compare-and-set transitions the proposal in one transaction.
  Pending proposals block document deletion; reviewed rows retain URL/SHA
  provenance through a nullable `SET NULL` document reference.
- Legacy pending rows without citation-bound evidence remain review-required
  and are never silently upgraded.
