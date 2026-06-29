# IPO-001 - IPO domain model and score contract

## Decision

IPO-001 adds a backend-only Indian IPO domain with normalized source facts,
deterministic scoring, a binary verdict, and durable evaluation history. It does
not scrape, fetch, infer factor scores from raw metrics, or render a Streamlit UI.

The score framework comes from *Indian IPO Investment Strategy*: business quality
25, financial growth 20, return ratios 15, valuation 15, QIB subscription 10,
promoter quality 10, and GMP/sentiment 5.

## Boundaries

- `backend/ipo/models.py` owns immutable DTOs, enums, and validation.
- `backend/ipo/scorecard.py` applies the fixed weights without network or database access.
- `backend/ipo/verdict.py` applies score bands, confidence, and fail-closed policy.
- `backend/ipo/repository.py` owns typed transactions and detached return objects.
- `backend/storage/models.py` owns ORM table shapes; `backend/storage/ipo_repository.py`
  owns every SQLAlchemy read/write operation.

This split preserves the repository-boundary rule: application/domain code never
constructs SQL and the storage layer never decides whether an IPO is recommended.

## Score and verdict contract

Each factor arrives as a normalized score from 0 through 100 plus an optional
evidence-based reason. Missing factors contribute zero; weights are never
renormalized. The weighted total is rounded half-up to two decimal places.

| Score/evidence | Recommendation | Recommendation type |
|---|---|---|
| 80-100 | Recommended | Apply confidently and consider holding if allotted |
| 65-79.99 | Recommended | Apply primarily for listing gains |
| Below 65 | Not Recommended | Skip |
| Any mandatory factor missing | Not Recommended | Skip |

Business quality, financial growth, return ratios, valuation, and promoter
quality are mandatory. QIB subscription and GMP/sentiment are optional because
they may not exist before demand develops. Confidence is high with all factors,
medium with one optional factor missing, and low with both optional factors or
any mandatory factor missing.

The JSON output is exactly `company_name`, `score`, `recommendation`,
`recommendation_type`, `confidence`, `reasons`, `missing_data`, and
`source_documents`.

## Persistence

Six additive tables share the existing SQLAlchemy `Base`:

- `ipo_issues`: issue identity, lifecycle, dates, INR price/amount facts, and provenance.
- `ipo_documents`: registered DRHP/RHP/supporting URLs and provenance.
- `ipo_financials`: annual/quarterly secret-safe `metrics_json` snapshots.
- `ipo_subscriptions`: timestamped QIB/NII/retail/total demand snapshots.
- `ipo_scores`: immutable factor inputs, contributions, total, missing data, and model version.
- `ipo_recommendations`: the one-to-one immutable binary verdict for a score.

Source facts support full create/read/list/update/delete operations. Evaluations
are append-only; correction creates a new score/verdict pair. A pair can be
deleted together, and deleting an issue cascades through every child table.

## Safety and failure behavior

Only public HTTP(S) provenance URLs without embedded credentials are accepted;
no DNS resolution or fetch occurs. Financial JSON passes through the shared
secret-safe normalizer. Invalid enum, amount, date, score, parent ownership, or
source-document ownership fails before commit. Score and recommendation inserts
share one transaction, so either both persist or neither does.

## Verification and extension

Tests pin arithmetic, band boundaries, missing-data overrides, JSON shape, full
CRUD, deterministic ordering, ownership, rollback, constraints, cascades,
Alembic/ORM parity, and the no-network/no-Streamlit boundary. Later ingestion
tickets may populate these contracts, but must not weaken URL safety, invent
missing evidence, or bypass `backend.storage`.
