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
- **Independent verification:** every cited page must exist, and every cited
  number must literally appear on its cited page's text or tables (comma/
  currency-stripped string matching with rounding and parenthesised-negative
  variants). All verified -> high confidence; >=90% verified including all
  core values (latest-year revenue/EBITDA/PAT, net worth, shares, EPS) ->
  medium with reviewer notes; anything less fails closed and persists
  nothing. The agent may honestly report `value_not_found` instead of
  guessing, which surfaces as its own stable receipt code.

## Proposals, never evidence

A verified draft is stored as a **pending row in `ipo_extraction_proposals`**
(payload, confidence, verifier notes, agent/model provenance, source
SHA-256, one pending per document). Scoring never reads this table. In the
admin page's review section, **Approve** reconstructs the strict manual
contract from the payload and replays `submit_manual_extraction` — the
reviewer attests as `entered_by_email` and the cached PDF bytes are
re-hashed — producing a revision indistinguishable from hand-entered
evidence; **Reject** stores an attributable, reasoned, redacted record. The
lifecycle (pending rows carry no reviewer; approved rows must link their
revision) is enforced by CHECK constraints. Batch callers receive typed
`IpoExtractionErrorReceipt` values, never exceptions.

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
