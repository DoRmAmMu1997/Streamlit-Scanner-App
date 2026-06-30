# IPO architecture consistency pass

## Purpose

Reconcile the whole-system architecture documentation with the IPO Screener LLD
added in PR #83. The ticket-scoped IPO-001 and IPO-002 documents remain the
authoritative detailed decisions; this pass makes the HLD and cross-cutting LLDs
accurately expose those existing contracts.

## Scope

### IPO Screener LLD

- Keep the consolidated IPO-001/IPO-002 component overview.
- Separate the ingestion and evaluation paths in the position diagram. Filing
  ingestion does not automatically derive factor scores or trigger evaluation.
- Correct the module dependency table so `backend/ipo/repository.py` reflects its
  actual domain, scoring, result-normalization, and storage dependencies.
- Make the operator/scheduler boundary explicit: IPO-002 provides a runnable CLI,
  but no deployed schedule or long-lived service.

### High-level design

- Add official SEBI and the IPO filing CLI to the external-context and architecture
  diagrams.
- Update the system summary, requirements, and entrypoint wording so the backend-only
  IPO subsystem is visible without implying a Streamlit surface.
- Add one end-to-end IPO filing inventory flow showing the three independently
  committed categories and nonzero aggregate failure behavior.
- Extend cross-cutting security, observability, persistence, storage, design-decision,
  and roadmap text with the IPO-001/IPO-002 contracts.

### Cross-cutting component LLDs

- **Storage:** include `ipo_repository.py` in the source and dependency model;
  summarize its typed public boundary, IPO migration head, downgrade refusal,
  failure modes, and tests.
- **Observability:** include the IPO CLI as an entrypoint and document its four
  lifecycle events plus the durable failed-category audit behavior.
- **Security:** distinguish the shared public-URL validator from IPO-002's stricter
  exact-host, HTTPS-only, manually validated redirect policy.
- **Deployment/runtime:** document the IPO command as an explicit one-off container
  invocation, not an automatically deployed cron or daemon.
- **Operations:** link the existing IPO runbook to the consolidated LLD and retain
  its current recovery semantics.

## Non-goals

- No source-code, schema, migration, CLI, scheduler, Streamlit, or runtime behavior
  changes.
- No new IPO requirements beyond IPO-001 and IPO-002.
- No broad rewrite of unrelated subsystem documentation.
- No claim that SEBI ingestion derives scores, downloads PDFs, or is scheduled in
  the current deployment.

## Consistency rules

- The component LLD explains current structure; the two ticket documents preserve
  detailed rationale and acceptance semantics.
- Mermaid diagrams must use GitHub-compatible syntax and match real call direction.
- Relative links must resolve from their containing Markdown file.
- Event names, table names, migration revision, CLI spelling, and source boundaries
  must be copied from the implementation rather than paraphrased into new contracts.

## Verification

- Review the final diff against `backend/ipo/`, `backend/jobs/scan_ipo_filings.py`,
  `backend/observability/__init__.py`, `backend/storage/`, and the migration.
- Run `git diff --check`.
- Validate local Markdown links and Mermaid fence balance.
- Run documentation/static contract tests plus the IPO-focused test set.
- Run the repository's normal Ruff and full pytest gates before publishing.
