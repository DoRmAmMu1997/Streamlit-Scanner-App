# IPO-011 - The one-button IPO Screener

## Decision

The IPO pipeline is now dispatchable from the ordinary **Run screener**
button as a screener named **IPO Screener**, alongside the technical-analysis
and 67-ka-funda agents. It re-implements no pipeline stage: `run()` calls the
same `backend.jobs.run_ipo_screener` the CLI calls, so the button and the
terminal cannot drift apart, and the run inherits scan history, run status,
provenance receipts, and the audit trail from the existing scan lifecycle.

Three gaps stood between "a pipeline" and "a button that produces verdicts":
nothing could write a price band, extraction stopped at a human queue, and no
subscription source existed at all. This ticket closes two of them and
contains the risk of the third.

## Framework change: `requires_candles`

Every screener before this one scanned stock candles, so `_execute_screener`
unconditionally demanded Dhan credentials, a universe CSV, and a
`DailyDataLoader`. A screener may now declare `requires_candles: False` in its
`SCREENER` metadata; the dispatcher then skips all three and passes
`universe_df=None` / `data_loader=None`. The default is `True`, so every
existing screener is untouched. `run_scan` already tolerated a `None`
universe (`symbols_scanned` becomes NULL); its annotations now say so, and
RANK-002's `ScoringContext` is fed an empty frame at that single boundary
rather than having its own contract widened.

## Trust decisions

| Decision | Rationale |
|---|---|
| **Auto-approval is opt-in** (`IPO_AUTO_APPROVE_HIGH_CONFIDENCE`, default off) and only ever converts `HIGH`-confidence proposals. | `HIGH` means the host already re-resolved *every* cited value from the hash-verified PDF. `MEDIUM` carries values it could not confirm, which is exactly the case a human must adjudicate. With the switch off, behaviour is identical to the reviewed flow. |
| Autonomous approvals are attributed to a reserved automation identity (`ipo-automation@screener.local`). | An autonomous approval must never be mistakable for a human attestation in `entered_by_email` or the audit trail. |
| Web-sourced QIB demand may feed the optional 10-point factor but **can never fire** the `weak_qib_demand_near_close` hard caution. | That flag forces `Not Recommended` regardless of score. A scraped headline must not be able to reject an issue; the caution waits for official evidence and reports `not_evaluable` otherwise. |
| The subscription parser requires an explicit QIB anchor in the same clause. | Retail and overall subscription figures share the same headlines and would badly misstate institutional demand. Ambiguity yields `None`, never a guess. |
| A low-confidence snapshot is never written when official evidence exists, and an unchanged reading is never re-appended. | Scoring reads the *newest* snapshot, so appending would silently demote official data; and a new row every run would churn the scoring fingerprint so no issue could ever report `skipped_unchanged`. |
| The button is `RUN_SCAN` (analyst), like every other screener, but `draft_ai_extractions` defaults **off**. | Consistency with the screener framework, while paid model calls stay a deliberate per-run opt-in. |

## Known deviation from the approved plan

The plan also specified extracting **price band and issue dates from the RHP
as cited facts**. That is *not* in this change. It touches the most
safety-critical path in the repo — `_CITED_FACT_SCHEMA_VERSION`, the expected
fact map, and the approval-time re-resolution — and warrants its own focused
change rather than a rushed pass at the end of a large PR. Two consequences
follow, and they matter:

- Until it lands, an issue with no price band leaves `valuation` missing.
  Valuation is a **critical** factor, so those issues resolve to
  **"Insufficient verified data"**, and an autonomous run will not produce a
  positive verdict for them.
- Issue open/close dates likewise stay unset, which keeps
  `weak_qib_demand_near_close` at `not_evaluable`. That is the safe direction
  and is unchanged from today's behaviour.

The follow-up is specified in the plan file: an **optional** `issue_terms`
block on the proposal payload (price band as `CitedFinancialFact`s with
`period_end=None`), the schema version bumped to `cited-financial-fact/v3`
with v2 dual-accepted so queued proposals stay reviewable, and approval
applying the manual revision and the `update_issue` in one transaction.
Dates need a cited-date receipt type and are deliberately out of scope.

## Result rows

One row per IPO issue. `symbol` is a synthetic `IPO:{issue_id}` because
`scan_results.symbol` is a NOT NULL `String(50)` with no length guard in
`save_scan_results` — a long company name would raise `DataError` on
PostgreSQL — and the readable name travels in its own column. `rating` is the
binary recommendation (the friendly label is too long for `String(20)` and
gets its own column). Unscored issues still emit a row carrying an
`awaiting_evidence` rule, because a contract-invalid row is silently dropped
from persistence and would otherwise vanish from the operator's view.

`run_scan` runs `score_candidates` after the screener and overwrites
`final_score` with `None` for synthetic symbols, so the IPO score lives in
`ipo_score`; sorting the results table by `final_score` is meaningless for
this screener. Documented rather than worked around.

## Naming

The read-only nav view was renamed **"IPO screener" → "IPO dashboard"** so it
no longer collides with the new dropdown entry. The dashboard remains the
detail view (sections, filters, per-issue breakdown receipts); the screener
produces the run and the summary table.

## Testing

`tests/test_ipo_screener_module.py` pushes every emitted row through the real
`normalize_screener_row` the scan service uses, for both scored and unscored
issues, and pins the toggle→stage wiring, per-stage progress, issue selection,
and the 50-character symbol guard. `tests/test_ipo_auto_approval.py` pins the
default-off path, HIGH-only selection, the automation identity, and failure
isolation. `tests/test_ipo_enrichment.py` and `tests/test_ipo_caution_flags.py`
pin the parser discipline and the containment rule — including a pair proving
identical numbers differ only by provenance.
