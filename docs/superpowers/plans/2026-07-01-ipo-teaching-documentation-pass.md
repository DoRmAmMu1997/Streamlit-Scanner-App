# IPO Subsystem Teaching-Documentation Pass Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add detailed, beginner-friendly docstrings and rationale comments throughout the complete IPO-001/IPO-002/IPO-003 subsystem without changing runtime behavior.

**Architecture:** Treat documentation as a checked contract. First strengthen the AST guard so nested helpers cannot escape coverage, then improve one responsibility group at a time: pure domain policy, persistence orchestration, external SEBI I/O, migrations, and tests. Shared files receive IPO-specific edits only.

**Tech Stack:** Python 3.11+, `ast`, dataclasses, SQLAlchemy 2, Alembic, requests, Beautiful Soup, pytest, Ruff, mypy, Bandit.

---

## Execution status

- [x] Structural AST guard expanded and red/green behavior demonstrated.
- [x] Production, persistence, source, job, migration, and test teaching pass complete.
- [x] Executable-AST comparison confirms no runtime behavior changed outside the guard.
- [x] Focused and full local verification complete.
- [x] Commit, push to PR #84, and monitor the final hosted checks.

## File map

- `backend/ipo/models.py`, `scorecard.py`, `verdict.py`: typed domain contracts and deterministic policy.
- `backend/ipo/repository.py`: transaction-owning public IPO façade.
- `backend/storage/ipo_repository.py`, IPO classes in `backend/storage/models.py`: SQL and ORM-only persistence boundary.
- `backend/ipo/sources/sebi.py`, `backend/ipo/documents/downloader.py`: hostile external metadata/PDF boundaries.
- `backend/jobs/scan_ipo_filings.py`: CLI orchestration and category-level failure isolation.
- `backend/config/settings.py`, `backend/observability/__init__.py`: IPO-specific integration points only.
- `migrations/versions/*ipo*.py`: schema construction, portability, and guarded downgrade rationale.
- `tests/test_ipo*.py`, `tests/test_scan_ipo_filings_job.py`, IPO cases in `tests/test_scan_storage_migrations.py`: executable teaching examples and structural policy.

### Task 1: Make documentation coverage structural

**Files:**
- Modify: `tests/test_ipo_contract_policy.py`

- [ ] **Step 1: Extend the definition collector to nested symbols**

Replace the top-level/class-only collector with an AST walk that includes nested
functions and methods while excluding lambdas:

```python
DocumentedDefinition = ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef


def _documented_definitions(tree: ast.AST) -> list[DocumentedDefinition]:
    """Return every named executable definition covered by the teaching policy.

    ``ast.walk`` intentionally includes nested fake callbacks used by tests.
    Those helpers often encode the most important failure or concurrency setup,
    so allowing them to remain anonymous would leave a beginner at the hardest
    part of the example without guidance.
    """
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    ]
```

- [ ] **Step 2: Include all IPO-owned files and only IPO symbols in shared files**

Create explicit helpers that return full-file targets (`backend/ipo/**/*.py`,
`backend/storage/ipo_repository.py`, `backend/jobs/scan_ipo_filings.py`, IPO
migrations, `tests/test_ipo*.py`, and `tests/test_scan_ipo_filings_job.py`). Add
named shared-file targets for the IPO ORM classes and IPO migration tests rather
than requiring unrelated storage/settings code to be rewritten.

- [ ] **Step 3: Run the guard and capture the expected red state**

Run:

```powershell
python -m pytest -q tests/test_ipo_contract_policy.py::test_ipo_owned_code_and_tests_keep_beginner_friendly_docstrings
```

Expected: FAIL listing currently undocumented nested test helpers. This proves
the stronger guard detects the known gap before documentation is added.

- [ ] **Step 4: Commit the red policy guard with the later documentation batch**

Do not commit a deliberately failing branch. Keep the guard and make it green in
Tasks 2-6 before the first implementation commit.

### Task 2: Teach the pure domain and policy layer

**Files:**
- Modify: `backend/ipo/models.py`
- Modify: `backend/ipo/scorecard.py`
- Modify: `backend/ipo/verdict.py`
- Modify: `backend/ipo/__init__.py`

- [ ] **Step 1: Expand domain model docstrings**

For every enum, frozen DTO, record, validator, and serializer, explain:

- whether the object accepts external/source input or represents trusted output;
- normalization performed in `__post_init__` despite `frozen=True`;
- why records are detached from ORM sessions;
- which fields distinguish filing identity from downloaded-byte identity;
- which exception communicates public validation failure.

Use detailed sections where useful:

```python
def _optional_record_hash(value: object | None) -> str | None:
    """Validate IPO-002's filing-record fingerprint.

    ``record_hash`` identifies one SEBI listing event; it is not the digest of
    downloaded PDF bytes. ``None`` keeps manually entered legacy documents valid,
    while a supplied value must be a complete lowercase SHA-256 hexadecimal
    string so persistence can safely use it for idempotency.

    Raises:
        IpoValidationError: If a supplied fingerprint is not 64 hex characters.
    """
```

- [ ] **Step 2: Explain score arithmetic and fail-closed verdicts**

Document why factor weights total 100, why missing factors score zero without
reweighting, why Decimal half-up rounding is used, why mandatory-factor absence
overrides an otherwise high score, and how confidence is derived independently
from the binary recommendation.

- [ ] **Step 3: Run pure-policy tests**

Run:

```powershell
python -m pytest -q tests/test_ipo_models.py tests/test_ipo_scorecard.py tests/test_ipo_verdict.py
```

Expected: all tests pass; comments/docstrings must not alter values or ordering.

### Task 3: Teach persistence and transaction ownership

**Files:**
- Modify: `backend/ipo/repository.py`
- Modify: `backend/storage/ipo_repository.py`
- Modify: `backend/storage/models.py` (IPO classes only)

- [ ] **Step 1: Replace generic repository helper docstrings**

Each public CRUD/evaluation method must explain parent scoping, detached return
records, idempotent delete behavior, ordering, and relevant typed exceptions.
Each private value/record adapter must explain the DTO-to-column or ORM-to-domain
direction and why the conversion exists.

- [ ] **Step 2: Explain transaction boundaries and compare-and-set behavior**

Add rationale comments around:

- closing the first session before DNS/HTTP/filesystem work;
- atomically matching issue id, document id, URL, and type after download;
- preserving a corrected row when source identity changes;
- database authority when a best-effort audit sink fails;
- immutable score/recommendation insertion as one transaction.

- [ ] **Step 3: Expand storage helper and ORM class documentation**

Storage helper docstrings describe query filters, ordering, row ownership, flush
semantics, and return meaning. IPO ORM class docstrings explain the table's role,
important constraints, relationship/cascade behavior, and why flexible JSON or
nullable provenance is used.

- [ ] **Step 4: Run repository and persistence tests**

Run:

```powershell
python -m pytest -q tests/test_ipo_repository.py tests/test_ipo_persistence_models.py
```

Expected: all tests pass, including source-change and failure rollback cases.

### Task 4: Teach hostile external-input boundaries

**Files:**
- Modify: `backend/ipo/sources/sebi.py`
- Modify: `backend/ipo/documents/downloader.py`
- Modify: `backend/ipo/documents/__init__.py`
- Modify: `backend/jobs/scan_ipo_filings.py`
- Modify: IPO-specific comments in `backend/config/settings.py`
- Modify: IPO-specific comments in `backend/observability/__init__.py`

- [ ] **Step 1: Expand SEBI ingestion explanations**

Document fixed category URLs, Unicode/company normalization, outer-detail-anchor
selection, parse-loss detection, canonical record hashing, retry/pagination caps,
manual redirect validation, and inclusive date filtering. Explain that IPO-002
stores metadata only and never downloads the linked PDF.

- [ ] **Step 2: Expand downloader explanations**

Document exact host/DNS/port validation, direct-PDF versus iframe flow, response
ownership, decompressed-byte limits, `%PDF-` magic, incremental SHA-256, cache-hit
rehashing, symlink/path containment, temporary cleanup, fsync, and atomic rename.
Every helper's docstring must state its trusted input/output boundary and stable
error category where relevant.

- [ ] **Step 3: Expand job and integration explanations**

Document watermark overlap, empty-database defaults, per-category transactions,
partial-success exit semantics, durable safe audits, and why the downloader is
not called by the scan job. Constants/properties in shared configuration and
observability files receive nearby IPO-specific rationale only.

- [ ] **Step 4: Run source, downloader, and job tests**

Run:

```powershell
python -m pytest -q tests/test_ipo_sebi_source.py tests/test_ipo_sebi_ingestion.py tests/test_ipo_document_downloader.py tests/test_scan_ipo_filings_job.py
```

Expected: all runnable tests pass; a real symlink test may skip on Windows when
the host does not grant symlink creation, while its platform-independent ordering
test must pass.

### Task 5: Teach schema evolution and downgrade safety

**Files:**
- Modify: every `migrations/versions/*ipo*.py`
- Modify: IPO-focused tests in `tests/test_scan_storage_migrations.py`

- [ ] **Step 1: Document migration helpers and DDL sections**

Explain SQLite/PostgreSQL primary-key portability, UTC/numeric/text choices,
check/unique/index purpose, cascading ownership, additive nullable IPO-002/003
columns, and why content SHA-256 is intentionally not indexed.

- [ ] **Step 2: Document guarded downgrade decisions**

Explain why IPO-002 refuses to discard claimed identity and why IPO-003 refuses
to discard populated cache provenance or nondefault status before any DDL runs.

- [ ] **Step 3: Run migration parity and downgrade tests**

Run:

```powershell
python -m pytest -q tests/test_scan_storage_migrations.py tests/test_ipo_persistence_models.py
python -m alembic heads
```

Expected: tests pass and Alembic prints exactly `20260630ipo003 (head)`.

### Task 6: Turn tests into readable executable examples

**Files:**
- Modify: every `tests/test_ipo*.py`
- Modify: `tests/test_scan_ipo_filings_job.py`
- Modify: IPO cases/helpers in `tests/test_scan_storage_migrations.py`

- [ ] **Step 1: Document every nested callback and fake**

Add meaningful docstrings to nested `fetcher`, `ingestion`, `runner`, resolver,
audit recorder, verdict builder, session factory, and downloader functions. State
which dependency is replaced, what state is captured, and which failure/success
path the helper simulates.

- [ ] **Step 2: Replace generic test docstrings**

Each test docstring should name the invariant and why it matters. For example:

```python
def test_scorecard_rounds_half_up_to_two_decimal_places() -> None:
    """Use financial-style half-up rounding instead of Python's half-even rule.

    The public JSON score is part of a stable investment receipt. Pinning the
    tie behavior prevents the same factor inputs from producing a surprising
    one-paisa difference across later refactors.
    """
```

- [ ] **Step 3: Add inline arrange/act rationale only where setup is non-obvious**

Explain hostile-response construction, transaction tracking, ownership conflicts,
rollback induction, and expected cache sharing. Do not add comments such as
"create issue" directly above `create_issue(...)`.

- [ ] **Step 4: Run the structural guard and complete IPO suite**

Run:

```powershell
python -m pytest -q tests/test_ipo_contract_policy.py tests/test_ipo_models.py tests/test_ipo_scorecard.py tests/test_ipo_verdict.py tests/test_ipo_persistence_models.py tests/test_ipo_repository.py tests/test_ipo_sebi_models.py tests/test_ipo_sebi_source.py tests/test_ipo_sebi_ingestion.py tests/test_ipo_document_downloader.py tests/test_scan_ipo_filings_job.py tests/test_scan_storage_migrations.py
```

Expected: the teaching-policy audit reports no missing definitions and the full
IPO-focused set passes.

- [ ] **Step 5: Commit the complete teaching pass**

```powershell
git add backend/ipo backend/storage/ipo_repository.py backend/storage/models.py backend/jobs/scan_ipo_filings.py backend/config/settings.py backend/observability/__init__.py migrations/versions tests/test_ipo*.py tests/test_scan_ipo_filings_job.py tests/test_scan_storage_migrations.py
git commit -m "docs(ipo): deepen subsystem teaching comments" -m "Co-authored-by: Codex <codex@openai.com>"
```

### Task 7: Verify behavior preservation and publish

**Files:**
- Verify all modified files.
- Update: `docs/superpowers/plans/2026-07-01-ipo-teaching-documentation-pass.md` checkboxes.

- [ ] **Step 1: Inspect the diff for accidental runtime changes**

Run:

```powershell
git diff origin/main...HEAD -- backend/ipo backend/storage backend/jobs migrations/versions
git diff --check
```

Expected: only docstrings, comments, formatting, and the AST policy test change;
no expression, SQL, enum, constant, branch, call, or migration operation changes.

- [ ] **Step 2: Run full local verification**

Run:

```powershell
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m compileall -q app.py backend screeners ui Dependencies tests migrations
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
```

Expected: every command exits 0, coverage remains at least 84%, and the dependency
audit reports no known vulnerabilities.

- [ ] **Step 3: Commit plan completion, push, and monitor PR #84**

```powershell
git add docs/superpowers/plans/2026-07-01-ipo-teaching-documentation-pass.md
git commit -m "docs(ipo): close teaching pass checklist" -m "Co-authored-by: Codex <codex@openai.com>"
git push
gh pr checks 84 --watch --interval 10
```

Expected: Python 3.11, Python 3.12, and Docker image/Compose checks all complete
successfully for the final pushed commit.
