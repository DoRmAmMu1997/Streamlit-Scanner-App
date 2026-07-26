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
  optional GMP/sentiment factor and advisory review observations. A litigation
  hard caution requires corroborated official or approved-manual authority.
- **Every result is prompt-injection scanned before storage** (the shared
  TEST-003 engine). A hit replaces only that entry with a secret-safe blocked
  marker/reason; clean siblings remain usable. Zero clean results makes the
  batch `NOT_EVALUABLE` and human-review required.
- **Red-flag observations are contextual and negation-aware.** The collector
  records normalized matched context and reason for advisory review; it never
  grants those observations hard-veto authority.
- **GMP parsing is conservative.** A percent or rupee amount must occur within
  40 characters of `GMP` or `grey market premium` in the same normalized
  sentence/clause and the same result field;
  percent readings win; rupee readings convert only when the issue price is
  known; the median across entries becomes `parsed_value`, otherwise `NULL`.
  The factor weight is 5/100 and every reason string carries the
  "(low-confidence web source; never overrides document evidence)" note.
- **Provider resources are bounded before decoding.** The client streams at
  most 1 MiB, treats `Content-Length` only as an early rejection hint, and
  decodes JSON only after the streamed bound succeeds. Result fields must be
  strings and are capped at 2,000 characters. Cleanup always runs, never masks
  the primary typed/redacted error, and preserves cancellation semantics.
- **Identity is canonical before influence.** Clean entries are deduplicated
  and sorted by the server-created semantic hash before GMP aggregation and
  persistence. Duplicate or reordered provider results therefore cannot
  overweight a value or churn the batch fingerprint. Quarantine usability is
  still calculated over every inspected provider item.
- **No key, no problem.** A missing `SERPAPI_API_KEY` degrades to one
  graceful skip; the screener stays fully functional (the GMP factor is
  simply missing, which only lowers verdict confidence).
- Rows are stamped `confidence='low'` and
  `source_policy='serpapi-low-confidence-v2'`, and each batch
  persists atomically per issue with per-type query isolation.

## Testing

`tests/test_ipo_enrichment.py` pins the no-key skip, the quarantine round
trip (hostile text never reaches storage), same-clause and title/snippet GMP
boundaries, duplicate/order-independent semantic identity, rupee-to-percent
conversion, the no-price-band case, red-flag context, per-type failure
isolation, and the typed not-found error. The shared-client tests pin the 1 MiB
streamed bound, malformed length headers, 2,000-character fields, response
cleanup, redaction, and nested cancellation precedence.

> PR #108 hardening: persisted issuer name/price are the query authority;
> optional compatibility arguments must match before network access. Quarantine
> is per result, mixed batches preserve clean siblings, and all-hostile batches
> are `NOT_EVALUABLE`. GMP values must occur in the same clause and within
> 40 characters of GMP/grey market premium. Canonical deduplication precedes
> aggregation and persistence. Red-flag observations are negation-aware and advisory;
> litigation hard cautions require corroborated official or approved-manual
> evidence. Semantic hashes preserve `first_seen_at` and refresh
> `last_seen_at` without duplicates.
