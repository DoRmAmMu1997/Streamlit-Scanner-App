# Agent guide — Streamlit Scanner App

> This is the shared onboarding/working agreement for **AI coding agents** (Claude Code,
> Codex, and friends) working in this repo. Claude Code loads it via `CLAUDE.md` (which
> imports this file); Codex loads `AGENTS.md` directly. Humans are welcome too.
>
> **Read this once at the start of a task, then follow the links — don't re-derive what
> the design docs already explain.**

---

## 1. What this project is

A **Streamlit stock-scanner app** for the Indian market. It fetches daily candles from
DhanHQ, runs pluggable **screeners** (deterministic technical strategies plus a few
AI-assisted ones), ranks the shortlist with a deterministic score, persists every run to a
**scan-history** database (SQLite locally, Postgres in production), and exposes history,
comparison, validation, audit, and health pages. It is developed ticket-style (e.g.
`SCAN-002`, `RANK-001`, `OBS-003`) by multiple AI agents, **one PR per ticket**.

Start with the **[High-Level Design](docs/architecture/high-level-design.md)** and the
architecture index **[docs/architecture/README.md](docs/architecture/README.md)** — the HLD
component map links to a Low-Level Design (LLD) for every subsystem.

---

## 2. Skill activation (mandatory workflow)

This team drives work through skills. **Invoke them — do not wing it.** (These are Claude
Code slash-commands/skills; Codex agents should apply the same discipline with their
equivalents.)

| When you are… | Invoke | Why |
|---|---|---|
| **Starting any task** (even "simple" ones) | `/using-superpowers` | Establishes how to find and apply the right skills for the whole lifecycle. Run it first. |
| **About to build a feature / change behavior** | `anthropic-skills:brainstorming` | Explore intent & design before code. |
| **Designing** anything non-trivial (schema, service, algorithm, boundary) | `/system-design` **and** `/architecture` | Produce or update an ADR under `docs/architecture/` and weigh trade-offs before building. |
| **Writing or refactoring code** | `/karpathy-guidelines` | Surgical, minimal changes; surface assumptions; define verifiable success criteria. |
| **Implementing a feature/bugfix** | `/test-driven-development` | Write the failing test first. |
| **Reviewing a design or a diff** (always, before finishing) | `/code-review` **and** `/security-review` | Catch quality issues, security flaws, vulnerabilities, and improvements. |
| **About to claim "done"** | `/verification-before-completion` | Evidence (passing gates) before assertions. |

Rule of thumb: **design → `/architecture` + `/system-design`; code → `/karpathy-guidelines`;
review → `/code-review` + `/security-review`; everything → `/using-superpowers` first.**

---

## 3. Repository map

| Path | What lives here |
|---|---|
| `app.py` | Streamlit entrypoint + CLI prefetch launcher. Re-exports moved UI helpers for tests (see §4). |
| `backend/` | All data/infra/domain logic. **Never imports Streamlit.** |
| `screeners/` | One file per screener (strategy logic). See [adding-a-screener](docs/adding-a-screener.md). |
| `ui/` | Streamlit pages + shared display helpers. Imports `backend`, never `app`. |
| `Dependencies/` | Credentials template + DhanHQ token helper (not a Python package). |
| `migrations/` | Hand-written Alembic migrations for the scan-history schema. |
| `tests/` | pytest suite: unit, golden snapshots, and policy/guard tests. |
| `docs/` | Architecture (HLD + LLDs), operations runbook, screener guide. |

`backend/` subpackages (each has an LLD under `docs/architecture/components/`):

| Subpackage | Purpose |
|---|---|
| `storage/` | ORM models, engine/sessions, and the **repository** (the only place that builds SQL). |
| `scanning/` | `run_scan()` lifecycle + strict result/provenance contract (`service.py`, `result_contract.py`). |
| `validation/` | Forward-return calculators + aggregate validation metrics (VALID-*). |
| `scoring/` | Deterministic `final_score` ranking (RANK-002). |
| `audit/` | Best-effort, secret-safe audit recorder (OBS-003). |
| `observability/` | Structured, secret-safe logging (`log_event`, `EVENT_*`). |
| `security/` | Secret redaction + prompt-injection quarantine. |
| `config/` | Typed runtime settings from env (`get_settings`, `AppSettings`, `SettingsError`). |
| `fundamentals/`, `technical/`, `sixty_seven/` | The three AI-assisted subsystems. |
| `jobs/` | Headless CLIs (daily scan, forward-return computation). |
| `admin/`, `auth/`, `notifications/`, `data_quality/` | Config overrides, OIDC gate, alerts, candle-quality receipts. |
| `screener_registry.py`, `scanner_base.py`, `indicators.py`, `daily_data_loader.py`, `universe_*` | Screener framework, indicators, candle cache, universe management. |

---

## 4. Layering rules (enforced — don't break these)

1. **`backend/` never imports Streamlit.** Backend must be reusable by headless jobs and
   testable without a Streamlit ScriptRunContext.
2. **`ui/` imports `backend`, never `app.py` and never sibling pages.** Shared display
   helpers live in `ui/common.py` to avoid cycles.
3. **`app.py` re-exports moved helpers** (see `app.py` re-export block) so tests can reach
   them as `app.<name>` and monkeypatch page renderers like `app._render_history_page`.
4. **All database access goes through `backend/storage`** (the repository pattern). UI/
   services/jobs call repository helpers and never build `select(...)`, open an engine, or
   create a session. A CI guard
   ([`tests/test_repository_layer_boundary.py`](tests/test_repository_layer_boundary.py))
   rejects raw SQL/engine/session construction anywhere outside `backend/storage`. The
   repository **never opens its own session — the caller owns the
   transaction**; see [storage-persistence](docs/architecture/components/storage-persistence.md).

---

## 5. Coding conventions

- `from __future__ import annotations` at the top of modules.
- Modern typing: `X | None` (not `Optional[X]`), `list[...]`/`Mapping[...]`, keyword-only
  args after `*`, `cast()` for narrowing, lazy in-function imports to break cycles.
- **`Decimal` for money/prices**, never `float`.
- **Docstrings:** Google-style, with **"Beginner note:"** paragraphs that explain *why* for
  a junior reader. Keep inline-comment density high on non-obvious logic. Your changes
  should read like the surrounding code.
- Ruff: line-length **120**, rules `E,W,F,I,B,UP,C4,SIM,RUF` (config in `pyproject.toml`).
- mypy scope is `app.py backend screeners ui Dependencies` (tests are not yet type-checked,
  but **are** linted and compiled).

---

## 6. The CI gate suite (run locally before every PR)

CI (`.github/workflows/quality-and-security.yml`) runs the matrix Python **3.11 + 3.12**.
Reproduce it locally — these are the exact commands; **all must pass**:

```bash
# install pinned deps (requirements = what; constraints = exact verified versions)
python -m pip install -r requirements.txt -r requirements-dev.txt -c constraints.txt

python -m pre_commit validate-config .pre-commit-config.yaml
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=87
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt

# CI also builds + smoke-tests the deployment image:
docker build --tag streamlit-scanner-app:ci .
docker compose config
docker compose up --build --wait --wait-timeout 180
docker compose down --volumes --remove-orphans
```

Coverage floor is **87%** (measured ~89%). `pre-commit` hooks are **non-rewriting**
(check-only, no `--fix`) so commits stay author-reviewed.

---

## 7. Hard rules that fail CI if you miss them

- **DB/ORM change ⇒ ship the Alembic migration in the *same commit*.**
  `tests/test_scan_storage_migrations.py` asserts `alembic upgrade head` builds the *exact*
  schema as `Base.metadata`. Adding/removing a table also means updating the **hardcoded
  table-name sets** in that test. Migrations are hand-written under `migrations/versions/`.
- **Changing a CI command ⇒ co-update `tests/test_supply_chain_policy.py`** (it asserts the
  exact CI command strings, the Python matrix, and the `constraints.txt` pin list).
- **Adding a dependency ⇒ pin it in `constraints.txt`** (and keep `requirements*.txt` in
  sync). On any branch, `git diff origin/main HEAD -- constraints.txt pyproject.toml` must
  be empty unless the change is a deliberate, reviewed bump.
- **Changing a deterministic screener's output ⇒ regenerate goldens** with
  `UPDATE_GOLDEN=1 python -m pytest tests/test_screener_golden_outputs.py` and **review the
  JSON diff** before committing.
- **No raw DB access outside `backend/storage`** (see §4 guard).

---

## 8. Multi-agent workflow discipline

Concurrent agents share one checkout and `main` moves fast, so:

- **Never work in the shared checkout.** `git worktree add` an isolated worktree, branch per
  ticket (e.g. `feat/<ticket>-<slug>`), stack PRs only when genuinely sequential.
- **Update local `main` first** (`git fetch origin main:main`), then branch off it. When a PR
  merges underneath an in-flight branch, merge `origin/main` in and **re-run all gates**.
- **One PR per ticket.** GitHub blocks self-approval (the bot runs as the PR author), so post
  reviews as `event: "COMMENT"` with the verdict in the body, not `APPROVE`.

---

## 9. Windows / PowerShell gotchas (this dev machine)

- Prefer **PowerShell**; the Bash tool can misbehave here.
- **Commit/PR text:** PowerShell here-strings get arg-split by git. Write the message to a
  temp file and use `git commit -F <file>` / `gh pr create --body-file <file>`.
- `Out-File` defaults to **UTF-16** — write files with the Write tool or BOM-less UTF-8.
- **Mermaid** breaks on `;` inside `sequenceDiagram` message/note text (GitHub renders these
  docs). Validate with `npx -y @mermaid-js/mermaid-cli@11 -i <file>.md -o out.svg` and delete
  the generated SVGs.

---

## 10. Testing conventions

- DB fixtures live in `tests/conftest.py`: `db_session` / `session_factory` (in-memory) and
  `file_db_engine` / `file_session_factory` (file-backed, production-like pragmas). Reuse
  them — don't hand-roll engines.
- **Golden/snapshot tests** (`tests/test_screener_golden_outputs.py`) catch screener output
  drift; regenerate with `UPDATE_GOLDEN=1` (see §7).
- **UI tests monkeypatch the module that actually reads `st`** (e.g. `ui.health_page.st`).
- Coverage floor 87%; new code needs tests or it drags the gate down.
- Policy/guard tests are first-class here — see `tests/test_supply_chain_policy.py` and
  `tests/test_scan_storage_migrations.py` for the pattern when you need to lock in an invariant.

---

## 11. Security model

Secret redaction on every output sink (`backend/security/redaction.py`,
`normalize_secret_safe_json`), SSRF guards on server-side fetches, a prompt-injection
quarantine for untrusted AI evidence (`backend/security/prompt_injection.py`), a durable
secret-safe audit trail (`backend/audit/recorder.py`), and a fail-closed Google-OIDC + email
allowlist gate. Full details and the **accepted residual risks**:
[docs/architecture/components/security.md](docs/architecture/components/security.md). Run
`/security-review` on any change that touches external input, auth, crypto, or persistence.

---

## 12. Design-doc index — get redirected, don't re-derive

- **Start here:** [HLD](docs/architecture/high-level-design.md) ·
  [architecture index](docs/architecture/README.md) ·
  [component LLDs](docs/architecture/components/)
- **Persistence / schema:** [scan-run-persistence](docs/architecture/scan-run-persistence.md) ·
  [scan-002 handoff](docs/architecture/scan-002-handoff.md) ·
  [storage-persistence LLD](docs/architecture/components/storage-persistence.md)
- **Scan lifecycle & provenance:** [scan-service-and-provenance](docs/architecture/components/scan-service-and-provenance.md)
- **Scoring / ranking:** [rank-001 design](docs/architecture/rank-001-final-scoring-model.md) ·
  [rank-002 handoff](docs/architecture/rank-002-handoff.md) ·
  [scoring LLD](docs/architecture/components/scoring.md)
- **Validation / forward returns:** [valid-001 design](docs/architecture/valid-001-forward-return-validation.md) ·
  [valid-002 handoff](docs/architecture/valid-002-handoff.md) ·
  [validation LLD](docs/architecture/components/validation.md)
- **Data quality / acquisition:** [data-quality](docs/architecture/components/data-quality.md) ·
  [data-acquisition](docs/architecture/components/data-acquisition.md)
- **AI subsystems:** [fundamentals-ai](docs/architecture/components/fundamentals-ai.md) ·
  [technical-analysis-ai](docs/architecture/components/technical-analysis-ai.md) ·
  [sixty-seven-ka-funda-ai](docs/architecture/components/sixty-seven-ka-funda-ai.md)
- **Observability / audit / config:** [observability](docs/architecture/components/observability.md) ·
  [audit-log](docs/architecture/components/audit-log.md) ·
  [obs-003 design](docs/architecture/obs-003-audit-log.md) ·
  [configuration](docs/architecture/components/configuration.md)
- **Screener framework:** [screener-framework](docs/architecture/components/screener-framework.md) ·
  [screener-catalog](docs/architecture/components/screener-catalog.md) ·
  [indicators](docs/architecture/components/indicators.md)
- **Deploy / ops:** [deployment-runtime](docs/architecture/components/deployment-runtime.md) ·
  [operations runbook](docs/operations.md)
- **Decision/audit history:** [audit-2026-06 register](docs/architecture/audit-2026-06.md)
  (read before re-flagging "findings" — documents known false alarms). ADRs live under
  `docs/architecture/` — e.g. the
  [repository-boundary ADR](docs/architecture/refactor-002-repository-boundary.md) (REFACTOR-002).

---

## 13. Common commands

```bash
# Run the app (prefetch then Streamlit)
python app.py                 # or: streamlit run app.py

# Headless daily scan
python -m backend.jobs.run_daily_scan

# Forward-return validation job
python -m backend.jobs.compute_forward_returns
```

See the [operations runbook](docs/operations.md) for scheduling, Docker/Compose, Render
deployment, SQLite↔Postgres, and credential rotation.
