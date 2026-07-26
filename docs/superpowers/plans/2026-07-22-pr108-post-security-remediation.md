# PR #108 Post-Security Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the seven residual Codex Security findings on PR #108, preserve the deterministic IPO contracts, and publish a fully verified combined IPO-006…010 branch.

**Architecture:** Keep the containment, typed-evidence, authority-policy, proposal-lifecycle, and deterministic-score architecture already introduced by commits `55c909b..7130cc3`. Tighten only the remaining authority/resource boundaries: evidence must be semantically bound to its field and source context, debt/GMP meaning must be clause-aware, enrichment identity must be canonical, provider bytes must be bounded before JSON decoding, and untrusted text must be escaped at every Markdown-capable IPO UI sink.

**Tech Stack:** Python 3.11/3.12, dataclasses, `Decimal`, Pydantic, SQLAlchemy/Alembic, requests, Streamlit, pytest, Ruff, mypy, Bandit, Docker/Compose.

## Global Constraints

- Work only in `C:\Users\Sunny\Desktop\Coding Practice\Algo Trading\wt-ipo-6-10` on `feat/ipo-006-010-screener-agent`.
- Use test-driven development: add each regression first, run it and observe the expected failure, then implement the smallest complete fix.
- Add no runtime dependency; use the repository's existing libraries and callers' existing transaction ownership.
- AI output and SerpAPI output remain untrusted. Raw model fields cannot become approved evidence or scoring authority without host-created source binding.
- Preserve the seven factor weights `25/20/15/15/10/10/5`, recommendation thresholds `80/65`, binary verdict, critical-missing fail-closed behavior, and five-point maximum GMP influence.
- Preserve public fields additively; evidence schema `cited-financial-fact/v1` becomes legacy/review-required rather than being silently reinterpreted.
- Preserve manual human entry as approved evidence; the stricter source-proof requirement applies to AI-created extraction proposals.
- Keep PR #108 combined for IPO-006 through IPO-010 under the user-approved one-ticket convention waiver.
- Every commit includes `Co-authored-by: Codex <codex@openai.com>`.

---

### Task 1: Bind numeric and narrative proposal evidence to source meaning

**Files:**
- Modify: `backend/ipo/models.py`
- Modify: `backend/ipo/agents/financial_extractor.py`
- Modify: `backend/ipo/repository.py`
- Modify: `backend/ipo/__init__.py`
- Test: `tests/test_ipo_financial_extractor.py`
- Test: `tests/test_ipo_extraction_review.py`

**Interfaces:**
- Produces: `CitedTextEvidence(field_name, document_sha256, page_number, location, source_text, confidence, verification_reasons)` with `to_payload()`.
- Produces: proposal payload schema `cited-financial-fact/v2` containing complete `cited_financial_facts` and one `cited_text_evidence` record for `objects_of_issue`.
- Preserves: `extract_financial_proposal()` and proposal submission/approval public signatures.

- [ ] **Step 1: Add failing semantic-binding tests**

```python
def test_equal_value_in_wrong_financial_row_is_not_verified():
    # A table contains the same number in Revenue and Net Worth rows.
    # A proposal assigning the Revenue cell to Net Worth must not receive a fact.
    assert "net_worth" not in verified_fact_fields


def test_period_value_requires_matching_column_header():
    # The same value occurs under FY2023 and FY2024.
    # A FY2024 proposal citing the FY2023 cell must be rejected.
    assert extraction_is_rejected


def test_unit_must_share_the_value_table_or_bounded_text_context():
    # "10 million shares" elsewhere on the page cannot prove million INR.
    assert extraction_is_rejected


def test_objects_of_issue_requires_exact_source_span():
    # Model-written narrative that is absent from the cited page is not evidence.
    assert extraction_is_rejected
```

- [ ] **Step 2: Verify the new tests fail against `7130cc3`**

Run: `python -m pytest -q tests/test_ipo_financial_extractor.py tests/test_ipo_extraction_review.py`

Expected: the new cases fail because page-wide numeric/unit matching and an uncited narrative are currently accepted.

- [ ] **Step 3: Implement host-created semantic source matches**

```python
@dataclass(frozen=True)
class _VerifiedSource:
    source_token: str
    location: str
    verification_reasons: tuple[str, ...]


def _matching_numeric_source_for_fact(
    label: str,
    value: str,
    page: ExtractedPage,
    proposal: _ProposalModel,
) -> _VerifiedSource | None:
    """Match only a cell/line whose row label, period header, and unit context prove the fact."""
```

For table values, require a field-label synonym in the same row, the proposed annual period in the same column header for period facts, and the selected unit in that same table's header/context. For text-line values, require the label and value in the same line plus the period in the same bounded text block when applicable. Never fall back to another cell, line, table, or page-wide unit text.

- [ ] **Step 4: Bind `objects_of_issue` to an original source span**

Require the model to return an exact prospectus excerpt. Create `CitedTextEvidence` only when normalized text equals one original line or table cell on the cited page. Store that host-created evidence in the proposal payload and bump the schema to `cited-financial-fact/v2`.

- [ ] **Step 5: Enforce v2 evidence at submission and approval**

Parse both typed evidence collections in `_validate_cited_fact_binding()`. Require complete numeric facts and exactly one matching `objects_of_issue` text fact with the proposal SHA/page/source text/location. Reject v1 as legacy/review-required.

- [ ] **Step 6: Run focused tests and the two original financial checks**

Run: `python -m pytest -q tests/test_ipo_financial_extractor.py tests/test_ipo_extraction_review.py`

Expected: all pass; swapped labels/periods, unrelated units, and absent narratives are rejected while correctly labeled tables and exact excerpts remain accepted.

- [ ] **Step 7: Commit**

Commit message: `fix(ipo): bind extraction facts to source meaning`

### Task 2: Make debt-purpose classification sentence-aware and fail-closed

**Files:**
- Modify: `backend/ipo/scoring/factor_derivation.py`
- Test: `tests/test_ipo_factor_derivation.py`
- Test: `tests/test_ipo_caution_flags.py`

**Interfaces:**
- Preserves: `derive_debt_reduction_purpose_evidence(profile) -> DebtReductionPurposeEvidence | None`.
- Produces: only a cited, non-negated `AFFIRMATIVE` status can suppress the high-debt caution.

- [ ] **Step 1: Add failing regression cases**

```python
@pytest.mark.parametrize("text", [
    "No portion of the fresh issue proceeds, after allocation toward capital expenditure, working capital requirements, lease deposits, technology upgrades, issue expenses, and general corporate purposes, shall be applied toward repayment of borrowings.",
    "Repayment of borrowings from the net proceeds is expressly prohibited under the financing agreements.",
])
def test_negated_or_prohibited_repayment_is_not_affirmative(text):
    assert derive(text).status is DebtReductionPurposeStatus.NEGATIVE
```

Also retain positive controls for explicit repayment, `not only repayment ...`, ambiguous debt-only text, and conflicting affirmative/negative sentences.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_ipo_factor_derivation.py tests/test_ipo_caution_flags.py`

Expected: the long-prefix and suffix-prohibition cases fail as incorrectly affirmative.

- [ ] **Step 3: Replace fixed-distance negation with sentence/clause classification**

Classify each repayment match inside its complete sentence/clause. Any denial, exclusion, prohibition, or negative allocation governing that match makes it negative; `not only` is not negation. Conflicting affirmative and negative clauses remain `AMBIGUOUS`.

- [ ] **Step 4: Verify GREEN and caution behavior**

Run: `python -m pytest -q tests/test_ipo_factor_derivation.py tests/test_ipo_caution_flags.py`

Expected: all cases pass and high leverage remains fail-closed for `NEGATIVE`, `AMBIGUOUS`, or `MISSING`.

- [ ] **Step 5: Commit**

Commit message: `fix(ipo): fail closed on negated debt purposes`

### Task 3: Canonicalize enrichment, bind GMP to a clause, and bound provider bytes

**Files:**
- Modify: `backend/ipo/sources/enrichment.py`
- Modify: `backend/sixty_seven/search_client.py`
- Test: `tests/test_ipo_enrichment.py`
- Test: `tests/test_sixty_seven_search_client.py`

**Interfaces:**
- Preserves: `SerpApiClient.search(query, *, max_results=5) -> list[SearchResult]` and `collect_enrichment_signals(...)`.
- Produces: stable unique payload entries sorted by server-computed semantic hash.
- Produces: `SerpApiSearchError` for a response body over 1 MiB before JSON decoding.
- Produces: normalized string fields capped at 2,000 characters each.

- [ ] **Step 1: Add failing tests for all three residual boundaries**

```python
def test_gmp_number_must_be_in_same_clause():
    assert parse("Subscription rose 25%; GMP data unavailable.") is None
    assert parse("GMP is 25%.") == Decimal("25.00")


def test_duplicate_and_reordered_results_have_one_stable_identity():
    assert canonical_payload([low, high, high]) == canonical_payload([high, low])
    assert parsed_gmp([low, high, high]) == parsed_gmp([high, low])


def test_response_body_is_bounded_before_json_decode():
    with pytest.raises(SerpApiSearchError, match="response exceeded"):
        client.search("bounded")
```

Add controls for a missing/invalid `Content-Length`, streamed chunks crossing 1 MiB, non-string nested fields, and 2,000-character field truncation.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_ipo_enrichment.py tests/test_sixty_seven_search_client.py`

Expected: cross-clause GMP, duplicate weighting/order churn, and oversized body cases fail.

- [ ] **Step 3: Implement clause-local GMP parsing**

Split normalized text on sentence and clause delimiters (`;`, newline, `.`, `!`, `?`). Search for a GMP term and percent/rupee value only within the same clause and retain the existing 40-character maximum inside that clause.

- [ ] **Step 4: Canonicalize item identity before scoring and persistence**

Deduplicate normalized entries by their server-computed `semantic_hash`, sort by that hash, and compute GMP from the canonical clean tuple. Preserve per-item quarantine counts for batch usability while preventing duplicates from changing score or batch semantic identity.

- [ ] **Step 5: Stream and cap provider responses before decoding**

Call requests with `stream=True`; reject an advertised body over 1 MiB; otherwise read chunks into a byte buffer and stop as soon as the cumulative size exceeds 1 MiB. Decode JSON only after the bounded read. Clamp `max_results` and send that bound to SerpAPI. Accept only string result fields, truncate each to 2,000 characters, and keep existing redaction/error behavior.

- [ ] **Step 6: Verify GREEN plus repository consumers**

Run: `python -m pytest -q tests/test_ipo_enrichment.py tests/test_sixty_seven_search_client.py tests/test_sixty_seven_agent.py`

Expected: all pass; legitimate same-clause GMP and ordinary responses remain supported.

- [ ] **Step 7: Commit**

Commit message: `fix(ipo): bound and canonicalize web evidence`

### Task 4: Neutralize untrusted Markdown at all IPO UI sinks

**Files:**
- Modify: `ui/common.py`
- Modify: `ui/ipo_page.py`
- Modify: `ui/ipo_manual_page.py`
- Test: `tests/test_app_ipo_page.py`
- Test: `tests/test_app_ipo_manual_page.py`

**Interfaces:**
- Produces: `ui.common._neutralize_markdown(value: object) -> str`.
- Preserves: plain display text and all Streamlit page entry points; never enables unsafe HTML.

- [ ] **Step 1: Add failing rendering-boundary tests**

```python
def test_proposal_review_neutralizes_remote_image_markdown():
    hostile = "![x](https://invalid.example/pixel)"
    render_proposal(company_name=hostile, document_url=hostile, reason=hostile)
    assert all("![" not in text for text in markdown_capable_outputs)
```

Cover issuer names, document URLs/source labels, verifier/rejection reasons, snippets/evidence strings, and dashboard risk/positive text. Assert `unsafe_allow_html` is never enabled.

- [ ] **Step 2: Verify RED**

Run: `python -m pytest -q tests/test_app_ipo_page.py tests/test_app_ipo_manual_page.py`

Expected: the manual proposal URL/reason and issue labels retain active Markdown syntax.

- [ ] **Step 3: Move the existing helper to `ui.common` and apply it**

Escape backslash plus the complete Markdown control set before any untrusted value enters `st.caption`, `st.warning`, `st.error`, `st.success`, Markdown-formatted labels/options, or `st.markdown`. Keep structured `st.json`/`st.dataframe` values structured and do not turn on HTML rendering.

- [ ] **Step 4: Verify GREEN**

Run: `python -m pytest -q tests/test_app_ipo_page.py tests/test_app_ipo_manual_page.py`

Expected: all hostile strings render inertly and plain controls remain readable.

- [ ] **Step 5: Commit**

Commit message: `fix(ipo): neutralize untrusted markdown`

### Task 5: Close documentation, security evidence, and PR delivery

**Files:**
- Modify: `docs/architecture/ipo-010-security-integrity-hardening.md`
- Modify: `docs/architecture/components/ipo-extraction-ai.md`
- Modify: `docs/architecture/ipo-009-serpapi-enrichment.md`
- Modify: `docs/operations.md`
- Create outside the repository: `<scan_dir>/artifacts/fix_report.md`

**Interfaces:**
- Produces: a fix report mapping all seven finding IDs to regression tests, changed boundaries, exact commands, and remaining risk.
- Produces: an updated PR description and completion review with the combined-ticket waiver, migration notes, security closure, and verification evidence.

- [ ] **Step 1: Update the ADR and operational docs**

Document semantic field/period/unit binding, exact narrative spans, sentence-aware debt classification, clause-local GMP, canonical result identity, the 1 MiB response/2,000-character field caps, Markdown escaping, and the accepted Windows PDF memory-limit residual risk.

- [ ] **Step 2: Run focused IPO and security closure checks**

Run all IPO tests, the seven original local checks (which must no longer reproduce their unsafe transitions), Ruff on touched code, mypy, Bandit, migration parity, and compileall.

- [ ] **Step 3: Run all repository gates**

Run the exact commands in `AGENTS.md`: pre-commit validation, pytest with coverage at least 87%, compileall, Ruff, mypy, Bandit, pip-audit, Docker build, Compose config, and Compose smoke test.

- [ ] **Step 4: Commit documentation and verification evidence**

Commit message: `docs(ipo): close post-security remediation`

- [ ] **Step 5: Review the complete branch and publish**

Run a whole-branch code and security review from `de8f199b...HEAD`. Push the clean branch, update PR #108's description, reply to/resolve actionable review threads, watch hosted Python 3.11/3.12 and Docker checks, confirm mergeability, and post the final completion review.
