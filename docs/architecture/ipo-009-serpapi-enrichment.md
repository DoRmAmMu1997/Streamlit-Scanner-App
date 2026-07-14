# IPO-009 - Optional SerpAPI enrichment under strict trust rules

## Decision

`backend/ipo/sources/enrichment.py` runs seven fixed discovery query
templates (GMP, news, promoter reputation, litigation red flags, anchor
commentary, brokerage reviews, peer discovery) through the shared
`backend.sixty_seven.search_client.SerpApiClient` and persists one
`ipo_enrichment_signals` row per type. The adapter lives under
`backend/ipo/sources/` — the only reviewed network zone in the IPO domain —
and reuses the existing client because it is already settings-driven,
SSRF-free (one fixed endpoint; result links are data, never fetched), and
redaction-aware. Extracting the client into a shared package is a noted
follow-up, not part of this change.

## Trust rules (structural, not advisory)

- **Web results can never override official documents or supply a
  financial-statement number.** Signals are typed records with no path into
  the manual-extraction contract or the ratio engine; they feed only the
  optional GMP/sentiment factor and the litigation caution flag.
- **Every snippet is prompt-injection scanned before storage** (the shared
  TEST-003 engine). A hit replaces the entry's text with the blocked-evidence
  marker, sets `quarantined=true` on the row, and logs a payload-free
  warning; quarantined rows are ignored by both consumers.
- **Red-flag evidence is keyword matches only.** The collector records which
  allowlisted fragments (fraud, probe, investigation, litigation, sebi
  order, ...) matched; the caution flag reads those matches and never the
  snippet text.
- **GMP parsing is conservative.** A text must explicitly mention GMP;
  percent readings win; rupee readings convert only when the issue price is
  known; the median across entries becomes `parsed_value`, otherwise `NULL`.
  The factor weight is 5/100 and every reason string carries the
  "(low-confidence web source; never overrides document evidence)" note.
- **No key, no problem.** A missing `SERPAPI_API_KEY` degrades to one
  graceful skip; the screener stays fully functional (the GMP factor is
  simply missing, which only lowers verdict confidence).
- Rows are stamped `confidence='low'` and
  `source_policy='serpapi-low-confidence-v1'` forever, and each batch
  persists atomically per issue with per-type query isolation.

## Testing

`tests/test_ipo_enrichment.py` pins the no-key skip, the quarantine round
trip (hostile text never reaches storage), the GMP regex table including the
rupee-to-percent conversion and the no-price-band case, red-flag keyword
capture, per-type failure isolation, and the typed not-found error.
