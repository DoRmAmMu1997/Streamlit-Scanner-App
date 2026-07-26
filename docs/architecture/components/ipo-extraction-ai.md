# LLD - IPO Financial-Extraction AI (IPO-010)

| | |
|---|---|
| **Component** | IPO financial-extraction agent |
| **Source** | `backend/ipo/agents/financial_extractor.py`, `backend/ipo/documents/table_extractor.py`, `backend/ipo_pdf_worker.py`, `backend/ipo/documents/section_classifier.py` |
| **Layer** | backend (agent adapter over the Claude Agent SDK) |
| **Status** | Implemented (IPO-010) |
| **Related** | [ipo-010-ai-extraction-proposals.md](../ipo-010-ai-extraction-proposals.md) · [fundamentals-ai.md](fundamentals-ai.md) (shared runtime patterns) · [security.md](security.md) (TEST-003 quarantine) |

## 1. Purpose

Draft one review-queue proposal — a manual-extraction-shaped payload with a
page citation on every value — from one cached, hash-verified DRHP/RHP PDF.
The agent's output is never evidence: an administrator must approve it in
the review UI before scoring can see a number.

## 2. Position in the pipeline

```
verified cache (IPO-003) -> spawned bounded PDF worker -> parse receipt
    -> page/span-aware classify_pages -> page-safe agent tools
    -> host verification -> typed CitedFinancialFact set
    -> ipo_extraction_proposals (pending, one per document)
    -> admin Approve -> atomic revision + compare-and-set review transition
```

## 3. Public interface

| Symbol | Contract |
|---|---|
| `extract_document_pages(path, budget=...)` | Compatible facade over a spawn-safe child; returns a typed success or timeout/resource/crash/malformed/empty review-required receipt. |
| `PdfExtractionBudget` | Wall-time, page/table/row/column/cell/text/glyph/serialization/address-space limits. |
| `force_extract=True` | Revisits reviewed history but cannot bypass one-pending-per-document or semantic-payload uniqueness. |
| `propose_extraction(issue_id, document_id, *, data_dir=None, model=None, run_agent=None, session_factory=...)` | Returns `IpoExtractionProposalRecord` on success or a typed `IpoExtractionErrorReceipt`; never raises to batch callers. `run_agent` is the CI/test seam — the SDK is only touched when it is `None`. |
| `EXTRACTOR_MODEL_VERSION` | `"ipo-010-extractor-v2"`, stamped on every proposal. |
| `IpoExtractionError` | Typed failure with a stable `code` (`unsupported_document`, `pending_proposal_exists`, `value_not_found`, ...). |

## 4. Key design decisions

| Decision | Why |
|---|---|
| Reuse `ai_runtime` + `ai_validation` (`run_agent_coroutine`, `extract_json_object`, `StrictAIModel`, `parse_with_retry`) | One reviewed implementation of the sync bridge, JSON extraction, strict schemas, and the bounded retry across all four agents. |
| Locked-down `ClaudeAgentOptions` (`permission_mode="dontAsk"`, `setting_sources=[]`, in-process tools only) | The model can never touch the filesystem, network, or shell; behaviour comes entirely from our prompt. |
| Values travel as decimal strings | The exact printed digits survive schema validation, host verification, storage, and reconstruction without binary float drift. |
| Host parses complete tokens in the original table cell/text span | Formatting-equivalent Indian grouping/currency/whitespace/trailing zeros is accepted, but rounding, substring, and cross-cell matches fail. |
| Field label, unit, value, period header, page, cell/span, source token, and document SHA form one typed fact | A duplicate number in another financial row, fiscal column, peer metric, or unit context cannot be substituted as high-confidence evidence. |
| `objects_of_issue` uses exact `CitedTextEvidence` | A model paraphrase is useful review context but cannot impersonate an original prospectus line or table cell. |
| Submission and approval re-resolve receipts from bounded cached-PDF pages | Public callers cannot forge otherwise well-shaped receipt metadata; the current row SHA, cached bytes, and every source location must agree twice. |
| Exactly three distinct oldest-first annual periods | Duplicate, reversed, or nonannual rows fail before review persistence. |
| Proposals, never records | The worst outcome of a bad run is a rejected queue item plus an error receipt — scoring only ever consumes human-attested revisions. |

Approval is one caller-owned transaction: strict reconstruction, cache
re-verification, revision header/children, and proposal compare-and-set either
all commit or all roll back.

## 5. Failure modes / degradation

Error-receipt style (matching the technical/67 agents): parse failures get
one bounded retry; quarantined evidence, honest `value_not_found` reports,
unverifiable drafts, unparseable PDFs, duplicate pending proposals, and SDK
unavailability all become `IpoExtractionErrorReceipt` values carrying only
stable codes and exception type names. The screener job counts them and
keeps going.

The parent also terminates and joins a timed-out/crashed child and rejects
malformed or oversized worker output. Resource exhaustion, empty/scanned PDFs,
stale source SHA, and legacy-unbound evidence are review-required rather than
partial success. Evidence schema `cited-financial-fact/v1` is legacy; only
complete v2 numeric facts plus the exact narrative fact can reach a new
proposal. Raw hostile text is never stored in failure markers.

## 6. Configuration & dependencies

Model id from the shared `CLAUDE_AGENT_MODEL` reader (default
`claude-sonnet-4-6`); retry budget from `SCANNER_AI_MAX_ATTEMPTS`;
subscription auth via the bundled CLI (`ANTHROPIC_API_KEY` must stay unset).
`pdfplumber` is the only PDF dependency (already pinned); no new packages.
The job only invokes the agent behind `--extract`, so schedulers and CI
never spend plan credit by accident.

The default worker limits are 60 seconds, 800 pages, 20 tables/page, 250
rows/table, 50 columns/row, 100,000 cells/document, 200 characters/cell,
20,000 text characters/page, 2,000,000/document, and 16 MiB serialized output.
Linux applies a 512 MiB child address-space limit. Windows uses
wall/object/text/result containment without a new `psutil` dependency.
The spawn target lives in dependency-light `backend/ipo_pdf_worker.py`, so a
fresh Linux child applies its limit before importing pdfplumber rather than
first executing the broad `backend.ipo` facade and unrelated dependencies.

## 7. Testing

All agent tests inject `run_agent`; CI never spawns the SDK. The extractor
tests drive real pdfplumber over a byte-accurate in-test PDF, so citation
verification runs against genuinely extracted text. See
[ipo-010-ai-extraction-proposals.md](../ipo-010-ai-extraction-proposals.md).

Worker tests additionally cover spawn behavior, timeout, crash,
malformed/oversized responses, cleanup, every object/text budget, and scanned
PDFs. Verifier tests pin exact field/metric label, token, unit, period header,
page, cell/text-span binding, narrative equality, submission/approval
re-resolution, period order, quarantine, stale SHA, forged receipts, and
legacy-confidence downgrade.

## 8. Extension points

OCR behind the same `extract_document_pages` interface for `empty_document`
receipts; sector-specific schema variants (banks/NBFC statements) as new
strict models; auto-suggested review priorities from verifier notes.
