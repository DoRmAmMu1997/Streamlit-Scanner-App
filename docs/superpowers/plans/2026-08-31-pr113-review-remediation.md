# PR #113 Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every validated PR #113 security, correctness, quota-control, UI, provenance, and documentation gap before merging.

**Architecture:** Keep the fixed-endpoint SerpAPI transport responsible for bounded decoding and safe typed errors; let the IPO collector convert permanent provider states into explicit receipts; let the job own cross-issue stopping and a deterministic per-run enrichment cap. Preserve the persisted `only_active_issues` key while adding optional display metadata to the generic parameter renderer.

**Tech Stack:** Python 3.11/3.12 contracts, requests, dataclasses, Streamlit, pytest, Ruff, mypy, Bandit, pip-audit, GitHub Actions.

**Spec:** PR #113 plus review comment `pullrequestreview-5067895825`.

## Global Constraints

- Add no runtime dependency and make no schema change.
- Keep official/manual IPO evidence authoritative; SerpAPI remains advisory.
- Preserve existing exception subclass compatibility and CLI stage behavior.
- Use Google-style docstrings with `Beginner note:` rationale and inline comments for non-obvious controls.
- Add `Co-authored-by: Codex <codex@openai.com>` to the follow-up commit.

---

### Task 1: Make provider error handling fail closed

**Files:**
- Modify: `backend/sixty_seven/search_client.py`
- Modify: `backend/sixty_seven/agent.py`
- Test: `tests/test_sixty_seven_search_client.py`
- Test: `tests/test_sixty_seven_agent.py`

**Interfaces:**
- Preserve: `SerpApiClient.search(query, *, max_results=5) -> list[SearchResult]`.
- Preserve: `SerpApiSearchError` subclasses and `status_code`.
- Produce: provider-derived exception messages containing only app-owned generic text.

- [ ] **Step 1: Write failing regressions**

```python
def test_no_results_never_overrides_a_non_success_status(): ...
def test_no_results_requires_the_exact_provider_shape(): ...
def test_provider_error_text_is_not_exposed_by_the_exception(): ...
def test_research_error_text_is_prompt_injection_scanned(): ...
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_sixty_seven_search_client.py tests/test_sixty_seven_agent.py -k "no_results or provider_error or research_error"`

Expected: the non-success response returns `[]`, substring lookalikes return `[]`, provider prose appears in the exception, and the `error` field is excluded from quarantine.

- [ ] **Step 3: Implement the smallest safe boundary**

Use exact normalized no-results messages only when `200 <= status_code < 300`. Use provider prose only to select a subtype, then construct a fixed message such as `SerpAPI rejected the request.` Add `error` to `_research_payload_has_prompt_injection` as defense in depth.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest -q tests/test_sixty_seven_search_client.py tests/test_sixty_seven_agent.py`

### Task 2: Stop permanent auth failures and cap headless enrichment

**Files:**
- Modify: `backend/ipo/sources/enrichment.py`
- Modify: `backend/jobs/run_ipo_screener.py`
- Test: `tests/test_ipo_enrichment.py`
- Test: `tests/test_run_ipo_screener_job.py`

**Interfaces:**
- Add: `IpoEnrichmentOutcome.auth_failed: bool = False`.
- Add: `IpoScreenerJobOutcome.enrichment_auth_failed: bool = False`.
- Add: `IpoScreenerJobOutcome.enrichment_skipped_budget: int = 0`.
- Add: `run_ipo_screener(..., max_enrichment_issues: int | None = 25)` where `None` is uncapped.
- Add CLI: `--max-enrichment-issues N`; `0` maps to uncapped.

- [ ] **Step 1: Write failing collector/job regressions**

```python
def test_auth_rejection_stops_after_one_query(): ...
def test_auth_failure_stops_later_issues_and_exits_nonzero(): ...
def test_default_enrichment_cap_limits_only_paid_search_work(): ...
def test_zero_enrichment_cap_option_is_uncapped(): ...
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_ipo_enrichment.py tests/test_run_ipo_screener_job.py -k "auth or budget or cap"`

Expected: auth performs eight calls, no auth outcome exists, and the default job enriches more than 25 issues.

- [ ] **Step 3: Implement typed termination and budget accounting**

Catch `SerpApiAuthError` before generic continuation, mark `auth_failed`, and break. In orchestration, process enrichment only for the first `max_enrichment_issues` selected issues while every issue still reaches download/extract/score; report the skipped count. Auth termination stops later issues, increments `enrichment_failed` once, prints/logs `enrichment=auth_failed`, and therefore exits nonzero.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest -q tests/test_ipo_enrichment.py tests/test_run_ipo_screener_job.py`

### Task 3: Make the Streamlit control and provenance truthful

**Files:**
- Modify: `backend/screener_registry.py`
- Modify: `ui/parameter_controls.py`
- Modify: `screeners/ipo_screener.py`
- Test: `tests/test_screener_registry.py`
- Test: `tests/test_app_parameter_controls.py`
- Test: `tests/test_ipo_screener_module.py`

**Interfaces:**
- Add optional `ScreenerDefinition.parameter_labels: dict[str, str]` and `parameter_help: dict[str, str]` with empty defaults.
- Accept matching optional `SCREENER` metadata maps; ignore neither invalid keys nor non-string values—raise `ScreenerRegistryError`.
- Set IPO label to `Only upcoming IPOs` while retaining storage key `only_active_issues`.
- Bump `IpoScreener.SCREENER_VERSION` from `1.0.0` to `1.1.0`.

- [ ] **Step 1: Write failing metadata/render/provenance tests**

```python
def test_registry_propagates_parameter_display_metadata(): ...
def test_boolean_override_uses_custom_label_and_help(): ...
def test_ipo_metadata_names_upcoming_filter_and_bumps_version(): ...
```

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_screener_registry.py tests/test_app_parameter_controls.py tests/test_ipo_screener_module.py`

- [ ] **Step 3: Add the additive metadata contract and render it**

Validate that label/help keys are declared defaults and values are non-empty strings. Fall back to the existing raw key when metadata is absent, preserving every existing screener.

- [ ] **Step 4: Verify GREEN**

Run the same three test modules and confirm old parameter-control tests remain unchanged.

### Task 4: Reconcile docs and close the PR

**Files:**
- Modify: `docs/architecture/components/sixty-seven-ka-funda-ai.md`
- Modify: `docs/architecture/ipo-009-serpapi-enrichment.md`
- Modify: `docs/architecture/ipo-011-one-button-screener.md`
- Modify: `docs/operations.md`
- Modify: PR #113 description/review threads

- [ ] **Step 1: Update documentation**

State eight fixed query types, exact successful no-results handling, generic provider-error messages, terminal auth behavior, the 25-issue/200-search default headless budget, `--max-enrichment-issues 0`, the truthful Streamlit label, and screener version `1.1.0`.

- [ ] **Step 2: Run focused and full local gates**

```text
python -m pre_commit validate-config .pre-commit-config.yaml
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=89
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
```

Run Docker/Compose gates when Docker is locally available; otherwise require the hosted Docker job.

- [ ] **Step 3: Review and publish**

Inspect the complete diff, confirm no constraints/schema drift, commit once with Codex co-authorship, push `fix/ipo-012-upcoming-only-and-serpapi-taxonomy`, watch Python 3.11/3.12, CodeQL, and Docker checks, update the PR description, reply to and resolve all review threads, and confirm the live head is mergeable.
