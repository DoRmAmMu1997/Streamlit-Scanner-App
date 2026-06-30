# LLD - IPO Screener (`backend/ipo/`)

| | |
|---|---|
| **Component** | Backend-only IPO ingestion, scoring, and recommendation subsystem |
| **Source** | [`backend/ipo/`](../../../backend/ipo), [`backend/ipo/sources/sebi.py`](../../../backend/ipo/sources/sebi.py), [`backend/jobs/scan_ipo_filings.py`](../../../backend/jobs/scan_ipo_filings.py), [`backend/storage/ipo_repository.py`](../../../backend/storage/ipo_repository.py), [`backend/storage/models.py`](../../../backend/storage/models.py), [`migrations/versions/`](../../../migrations/versions) |
| **Layer** | Standalone IPO domain (no Streamlit surface yet) |
| **Status** | Implemented: IPO-001 (domain + scoring), IPO-002 (SEBI filing ingestion) |
| **Related** | [HLD](../high-level-design.md) - [IPO-001 design](../ipo-001-domain-score-contract.md) - [IPO-002 design](../ipo-002-sebi-filing-ingestion.md) - [storage-persistence.md](storage-persistence.md) - [security.md](security.md) - [observability.md](observability.md) |

## 1. Purpose & responsibilities

The IPO Screener evaluates Indian IPOs from official source facts. It has two
landed halves that share one persistence model:

- **Domain & scoring (IPO-001)**: typed, framework-independent contracts, a fixed
  100-point PDF-weighted scorecard, a binary fail-closed verdict, and an immutable
  evaluation history.
- **Filing ingestion (IPO-002)**: a hardened, official-SEBI-only listing source
  and a headless job that inventories DRHP / RHP / final-offer filings into the
  issue/document tables.

**Non-responsibilities (deliberate, current scope)**
- No prospectus download or PDF parsing. IPO-002 inventories filing-detail URLs
  only.
- No factor-score inference from raw financials. The scorecard consumes already
  normalized 0-100 factor scores; deriving them is a later ticket.
- No Streamlit surface. The whole subsystem is backend-only and UI-free.
- No scraping outside `backend/ipo/sources`, and no source other than SEBI yet.

## 2. Position in the system

```mermaid
flowchart LR
    Operator["Operator / scheduler"] --> Job["scan_ipo_filings CLI"]
    SEBI[("Official SEBI listings")]
    subgraph Ingestion [Filing ingestion]
      Fetch["fetch_sebi_filings"]
      Build["build_filing_data -> IpoFilingData"]
      Ingest["ingest_filings"]
    end
    subgraph Evaluation [Explicit evaluation]
      Caller["Scoring caller with IpoScoreInput"]
      Eval["evaluate_issue"]
      Score["score_ipo"]
      Verdict["build_recommendation"]
    end
    Storage[("ipo_* tables via backend/storage")]

    Job --> Fetch
    SEBI --> Fetch --> Build --> Ingest --> Storage
    Caller --> Eval --> Score --> Verdict
    Eval -->|atomically persists score + verdict| Storage
```

The headless job orchestrates ingestion; evaluation is a separate, explicit call
that receives a complete `IpoScoreInput`. Persisted financial and subscription
facts are **not** automatically converted into factor scores, and filing ingestion
never triggers a recommendation. Both paths persist only through `backend/storage`.

## 3. Module boundaries

| Module | Responsibility | May import |
|---|---|---|
| `backend/ipo/models.py` | Frozen DTOs, enums, validation (URLs, money, hashes). | stdlib, `backend.security`, `backend.url_safety` |
| `backend/ipo/scorecard.py` | Fixed PDF weights, half-up rounding, missing-data receipt. | `models` |
| `backend/ipo/verdict.py` | Score bands, confidence, fail-closed override. | `models` |
| `backend/ipo/sources/sebi.py` | **Only** module allowed network I/O + hostile-HTML parsing. | `requests`, `bs4`, `models` |
| `backend/ipo/repository.py` | Typed transactions, ingestion identity/lifecycle, atomic evaluation. | `models`, `scorecard`, `verdict`, `scanning.result_contract`, `storage` |
| `backend/storage/ipo_repository.py` | Every SQLAlchemy statement for the `ipo_*` tables. | `sqlalchemy`, `storage.models` |
| `backend/jobs/scan_ipo_filings.py` | CLI boundary: windows, per-category loop, exit code, audits. | `ipo`, `audit`, `observability`, `storage.database` |

These rules are enforced by the AST guard
[`tests/test_ipo_contract_policy.py`](../../../tests/test_ipo_contract_policy.py):
no IPO module imports Streamlit, and only `backend/ipo/sources` may import a
network client.

## 4. Public interface

| Symbol | Contract |
|---|---|
| `score_ipo(IpoScoreInput) -> IpoScoreResult` | Applies the fixed weights; missing factors contribute zero and are never renormalized. |
| `build_recommendation(IpoScoreResult) -> IpoRecommendationResult` | Maps a score to the binary verdict + confidence; `.to_dict()` is the exact public JSON. |
| `evaluate_issue(issue_id, IpoScoreInput)` | Computes and atomically persists one immutable score/verdict pair. |
| `fetch_sebi_filings(category, from_date, to_date)` | Bounded fetch of one fixed SEBI category; returns frozen `SebiFiling` rows. |
| `build_filing_data(SebiFiling) -> IpoFilingData` | Derives display name, stable `sebi_company_key`, status, and the SHA-256 fingerprint. |
| `ingest_filings(filings, *, session_factory)` | Atomically creates/updates issues and documents for one category; returns `IpoIngestionSummary`. |
| `get_latest_filing_date()` | Global ingestion watermark (newest persisted `filing_date`). |
| CRUD: `create_/get_/list_/update_/delete_*` for issues, documents, financials, subscriptions. | Typed, detached records; never leak ORM rows. |

The scorecard and verdict tables (weights, 80/65 bands, confidence rules) are the
authority of [IPO-001 design](../ipo-001-domain-score-contract.md).

## 5. Persistence

Six additive tables share the existing `Base`; full column rationale lives in
[storage-persistence.md](storage-persistence.md).

- `ipo_issues` is the cascade root. IPO-002 adds nullable, uniquely-indexed
  `sebi_company_key` and the `unknown` issue type.
- `ipo_documents` holds registered filing URLs; IPO-002 adds nullable
  `filing_date` and a uniquely-indexed 64-char `record_hash` (length-checked).
- `ipo_financials`, `ipo_subscriptions` hold secret-safe normalized facts.
- `ipo_scores` (immutable factor inputs + total) pairs one-to-one with
  `ipo_recommendations` (immutable verdict).

Migrations are additive and nullable, so manual / IPO-001 rows stay valid. The
IPO-002 downgrade refuses to run while any SEBI identity exists rather than
silently discarding fingerprints or reclassifying `unknown` issues. Schema-drift
detection is metadata-driven, so new columns are covered automatically.

## 6. SEBI source: security posture

`backend/ipo/sources/sebi.py` is the subsystem's only network boundary and treats
every response as hostile:

- **Host lockdown**: fixed exact-match HTTPS allowlist (`sebi.gov.in`,
  `www.sebi.gov.in`); credentials and non-443 ports rejected.
- **Manual redirects**: `allow_redirects=False`; each hop's `Location` is
  re-validated through the same allowlist, capped at 3 hops.
- **Resource bounds**: 5s connect / 20s read timeouts, capped 2/5/10s retries on
  429 and 5xx, a polite inter-page delay, a streamed 2 MiB cap (plus
  `Content-Length` precheck), an HTML content-type check, and a 200-page ceiling.
- **Fail-closed parsing**: any malformed filing-like row aborts the whole
  category instead of producing a partial inventory; nested abridged-prospectus
  PDF anchors are ignored and never stored.
- **Secret-safe failures**: logs and the durable audit row store only the bounded
  date/category context and the exception **class**, never `str(exc)` or any
  response body. TLS verification is left on.

## 7. Ingestion identity & lifecycle

- **Company identity**: titles are NFKC-normalized; filing/addendum/corrigendum
  markers are stripped; the `sebi_company_key` case-folds, normalizes punctuation
  and `&`, and canonicalizes common corporate suffixes. Only an explicit `SME`
  token selects `sme`; everything else stays `unknown`.
- **Filing fingerprint**: SHA-256 over canonical JSON of `{company_key,
  document_type, filing_date, document_url}` - an ingestion identity, not a PDF
  content hash.
- **Monotonic status**: `drhp_filed -> rhp_filed -> open -> closed -> listed`.
  DRHP / RHP / final-offer rows target `drhp_filed` / `rhp_filed` / `closed`; an
  older or replayed filing never regresses a later state.
- **Matching**: issues by unique `sebi_company_key` (a single unclaimed
  same-name legacy row may be claimed once); documents by unique `record_hash`,
  then by canonical URL. A fingerprint or URL owned by a different issue is a
  validation conflict and never reparents the row.
- **Transactions**: each SEBI category is one atomic invocation; a conflict rolls
  back that category while already-committed categories stay durable.

## 8. Failure modes & recovery

- Per-category fetch/parse/persist failure -> structured `ipo_filing_category_failed`
  log + durable secret-safe audit row; other categories still commit; the command
  exits nonzero.
- Fatal pre-scan failure (schema bootstrap, invalid window) -> aborts before any
  fetch with a fatal log and nonzero exit.
- Recovery is a normal rerun: the default window starts 7 days before the newest
  stored `filing_date` (30 days back for an empty database) and ends today, so the
  overlap plus deterministic fingerprints make repeat ingestion idempotent.

## 9. Testing

- [`tests/test_ipo_models.py`](../../../tests/test_ipo_models.py),
  [`tests/test_ipo_scorecard.py`](../../../tests/test_ipo_scorecard.py),
  [`tests/test_ipo_verdict.py`](../../../tests/test_ipo_verdict.py) - domain
  contracts, weights, bands, and JSON shape.
- [`tests/test_ipo_sebi_source.py`](../../../tests/test_ipo_sebi_source.py) -
  parsing, identity normalization, redirect/host/content-type/size/page-cap guards.
- [`tests/test_ipo_sebi_models.py`](../../../tests/test_ipo_sebi_models.py),
  [`tests/test_ipo_sebi_ingestion.py`](../../../tests/test_ipo_sebi_ingestion.py) -
  filing contracts, monotonic status, legacy claim, ownership conflicts, idempotency.
- [`tests/test_scan_ipo_filings_job.py`](../../../tests/test_scan_ipo_filings_job.py) -
  windows, partial-failure exit codes, and secret-safe audits.
- [`tests/test_ipo_repository.py`](../../../tests/test_ipo_repository.py),
  [`tests/test_ipo_persistence_models.py`](../../../tests/test_ipo_persistence_models.py),
  [`tests/test_scan_storage_migrations.py`](../../../tests/test_scan_storage_migrations.py) -
  CRUD, constraints/uniqueness, cascades, migration upgrade/downgrade parity.
- [`tests/test_ipo_contract_policy.py`](../../../tests/test_ipo_contract_policy.py) -
  network-only-in-sources and UI-free import guard.

## 10. Extension points

- **More sources**: add NSE/BSE subscription or GMP adapters under
  `backend/ipo/sources/`, each behind its own host allowlist; the ingestion and
  scoring contracts stay unchanged.
- **Factor derivation**: a future ticket can turn normalized `ipo_financials` /
  `ipo_subscriptions` facts into the scorecard's 0-100 factor inputs.
- **Automation & UI**: `python -m backend.jobs.scan_ipo_filings` is manually
  runnable and scheduler-compatible, but IPO-002 does not add a scheduler,
  Render cron, Compose daemon, or Streamlit entrypoint. A future orchestration
  ticket can schedule it, and a later read-only IPO surface can render
  `IpoRecommendationResult.to_dict()`.

Any extension must preserve URL safety, never invent missing evidence, and route
all SQL through `backend/storage`.
