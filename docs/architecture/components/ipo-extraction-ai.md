# LLD - IPO Financial-Extraction AI (IPO-010)

| | |
|---|---|
| **Component** | IPO financial-extraction agent |
| **Source** | `backend/ipo/agents/financial_extractor.py`, `backend/ipo/documents/table_extractor.py`, `backend/ipo/documents/section_classifier.py` |
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
verified cache (IPO-003) -> extract_document_pages -> classify_pages
    -> propose_extraction (SDK loop over three in-process tools)
    -> host verification -> ipo_extraction_proposals (pending)
    -> admin Approve -> submit_manual_extraction path -> immutable revision
```

## 3. Public interface

| Symbol | Contract |
|---|---|
| `propose_extraction(issue_id, document_id, *, data_dir=None, model=None, run_agent=None, session_factory=...)` | Returns `IpoExtractionProposalRecord` on success or a typed `IpoExtractionErrorReceipt`; never raises to batch callers. `run_agent` is the CI/test seam — the SDK is only touched when it is `None`. |
| `EXTRACTOR_MODEL_VERSION` | `"ipo-010-extractor-v1"`, stamped on every proposal. |
| `IpoExtractionError` | Typed failure with a stable `code` (`unsupported_document`, `pending_proposal_exists`, `value_not_found`, ...). |

## 4. Key design decisions

| Decision | Why |
|---|---|
| Reuse `ai_runtime` + `ai_validation` (`run_agent_coroutine`, `extract_json_object`, `StrictAIModel`, `parse_with_retry`) | One reviewed implementation of the sync bridge, JSON extraction, strict schemas, and the bounded retry across all four agents. |
| Locked-down `ClaudeAgentOptions` (`permission_mode="dontAsk"`, `setting_sources=[]`, in-process tools only) | The model can never touch the filesystem, network, or shell; behaviour comes entirely from our prompt. |
| Values travel as decimal strings | The exact printed digits survive schema validation, host verification, storage, and reconstruction without binary float drift. |
| Host string-matches every cited number on its cited page | Verification is deterministic host code, not model self-grading; a hallucinated citation cannot reach the review queue. |
| Proposals, never records | The worst outcome of a bad run is a rejected queue item plus an error receipt — scoring only ever consumes human-attested revisions. |

## 5. Failure modes / degradation

Error-receipt style (matching the technical/67 agents): parse failures get
one bounded retry; quarantined evidence, honest `value_not_found` reports,
unverifiable drafts, unparseable PDFs, duplicate pending proposals, and SDK
unavailability all become `IpoExtractionErrorReceipt` values carrying only
stable codes and exception type names. The screener job counts them and
keeps going.

## 6. Configuration & dependencies

Model id from the shared `CLAUDE_AGENT_MODEL` reader (default
`claude-sonnet-4-6`); retry budget from `SCANNER_AI_MAX_ATTEMPTS`;
subscription auth via the bundled CLI (`ANTHROPIC_API_KEY` must stay unset).
`pdfplumber` is the only PDF dependency (already pinned); no new packages.
The job only invokes the agent behind `--extract`, so schedulers and CI
never spend plan credit by accident.

## 7. Testing

All agent tests inject `run_agent`; CI never spawns the SDK. The extractor
tests drive real pdfplumber over a byte-accurate in-test PDF, so citation
verification runs against genuinely extracted text. See
[ipo-010-ai-extraction-proposals.md](../ipo-010-ai-extraction-proposals.md).

## 8. Extension points

OCR behind the same `extract_document_pages` interface for `empty_document`
receipts; sector-specific schema variants (banks/NBFC statements) as new
strict models; auto-suggested review priorities from verifier notes.
