# IPO-008 - One-command screener orchestration and fingerprint idempotency

## Decision

`python -m backend.jobs.run_ipo_screener` runs the whole deterministic
pipeline: (1) SEBI filing inventory (delegating to the IPO-002 job), (2)
DRHP/RHP downloads into the verified cache, (3) optional IPO-009 web
enrichment, (4) — only with `--extract` — IPO-010 AI extraction proposals,
and (5) a re-score of every issue. It mirrors the `scan_ipo_filings`
template exactly: argparse, full dependency injection, per-unit failure
isolation, frozen outcome dataclasses with an `exit_code` contract, and
bounded `[ipo-screener] key=value` summary lines that never carry evidence.

The scoring stage lives in `backend/ipo/scoring/service.py::rescore_issue`
so the dashboard's re-score button and the job run literally the same code.
An issue without a verified manual profile reports `insufficient_inputs`
and writes nothing — missing data never becomes a fabricated score.

## Inputs fingerprint (the idempotency anchor)

Before persisting, the service computes a SHA-256 over exactly what scoring
consumed: the three rule versions (`ipo-006-v1`, factor and flag versions),
the extraction id + source SHA-256, the issue's updated-at/status/price
band, the newest subscription snapshot identity, every enrichment signal id,
and two *time-derived* facts — the set of GMP signals still inside the
staleness window and whether the issue is inside its near-close demand
window. Hashing derived facts instead of the clock keeps re-runs no-ops
until the passage of time would actually change a factor or flag. When the
newest stored evaluation carries the same model version and fingerprint the
service reports `skipped_unchanged`; the fingerprint is stored on
`ipo_scores.inputs_fingerprint` (legacy ipo-001-v1 rows keep `NULL`).

## Failure and configuration semantics

- Every stage isolates per unit (one document, one issue, one query batch);
  a failure is counted, printed as a typed line, and drives exit code 1.
- A missing `SERPAPI_API_KEY` is a configuration state, not a failure: the
  first probe prints one `enrichment=skipped_no_key` line, the rest of the
  run proceeds, and the exit code stays 0.
- AI extraction is behind `--extract` (default off) so schedulers and CI can
  never spend Claude plan credit by accident; duplicate pending proposals
  count as skips, not errors.
- `--issue-id` (repeatable) narrows downloads, enrichment, extraction, and
  scoring for targeted re-runs; `--skip-scan/--skip-download/--skip-enrich`
  gate their stages.

## Summary grammar

```
[ipo-screener] recommended issue_id=12 score=81.25 type=high_conviction confidence=high company=Acme Ltd
[ipo-screener] not_recommended issue_id=9 score=44.00 type=skip confidence=high flags=very_expensive_valuation company=Bar Ltd
[ipo-screener] insufficient_data issue_id=15 missing=manual_extraction company=Baz Ltd
[ipo-screener] totals evaluated=3 skipped_unchanged=4 insufficient=1 failed=0 downloads_failed=0 proposals=0 exit_code=0
```

Evaluated issues whose verdict type is "Insufficient verified data" print as
`insufficient_data` with their missing factors, keeping the operator's view
of data gaps in one grammar.

## Testing

`tests/test_ipo_scoring_service.py` proves the real round trip on the
file-backed engine: evaluated -> skipped_unchanged -> re-opened by a price
band, subscription, or GMP change, plus clock-independence of the
fingerprint. `tests/test_run_ipo_screener_job.py` pins stage gating,
`--extract` targeting, isolation and exit codes, the no-key skip, and the
CLI wiring.
