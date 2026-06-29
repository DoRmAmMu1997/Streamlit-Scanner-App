# OBS-003 — Audit log

| | |
|---|---|
| **Ticket** | OBS-003 — Add audit log table |
| **Type / Priority** | Story · P1 |
| **Owner / Reviewer** | Claude / Codex |
| **Status** | Implemented (schema + recorder + admin pages + tests landed) |
| **Branch** | `claude/obs-003-audit-log` |
| **Depends on** | SCAN-002 (DB layer), SEC-002 (redaction), OBS-001 (structured logging), AUTH-002 (admin identity) |

---

## 1. Context & goal

The app records *scan* runs (`scan_runs` / `scan_results`) but kept **no record of
who did what**: sign-ins, manual scans, configuration changes, CSV exports, and
admin-page access all left no durable trace. OBS-003 adds a durable, queryable
**audit log** so an operator can later answer *"who exported that file?"* or
*"when was the log level changed, and by whom?"*

The acceptance criteria are: audit events include the **user email**, a
**timestamp**, **action metadata**, and **sensitive values are redacted**.

This document is the OBS-003 design deliverable (sibling of
[scan-run-persistence.md](scan-run-persistence.md)). The runtime LLD lives at
[components/audit-log.md](components/audit-log.md).

---

## 2. Scope

**In scope:**
- Two tables on the existing SQLAlchemy `Base`: `audit_logs` (the trail) and
  `app_config` (runtime overrides for the admin config form).
- A thin, best-effort, secret-safe **recorder** (`backend/audit`).
- Recording **seven** events: `login_success`, `login_denied`,
  `manual_scan_started`, `data_refresh_started`, `config_changed`,
  `export_downloaded`, `admin_page_accessed`.
- A minimal **admin config form** (gives `config_changed` a real trigger) and an
  admin **audit log viewer** page.

**Out of scope (note for later tickets):**
- Retention / rotation policy, log-tamper protection / signing, encryption at rest.
- Editing security/infra/secret settings via the config form (OBS-003 initially
  exposed only the non-secret operational `LOG_LEVEL` / `LOG_FORMAT` keys).
- Exporting the audit log itself.
- UI de-duplication is best-effort, not a security control — audit completeness is
  not relied on for authorization.

---

## 3. The schema

The two tables are independent of `scan_runs` (no foreign keys): an audit row can
describe an action that is not tied to any single scan.

```
audit_logs    — append-only trail of user actions
app_config    — key/value runtime overrides (one row per setting)
```

### 3.1 `audit_logs` — one row per recorded user action

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `id` | BigInt PK¹ | no | PK | Surrogate key. |
| `created_at` | DateTime(tz) | no | ✓ | UTC time the action occurred (ORM default). |
| `event` | String(50) | no | ✓ | Event name (e.g. `login_success`). |
| `user_email` | String(320) | yes | ✓ | Actor; **NULL** for system events (startup data refresh). |
| `metadata_json` | JSON | yes | — | Redacted, JSON-safe action metadata. |

¹ `BigInteger` on Postgres, `Integer` on SQLite — the shared SCAN-001
`BigIntPrimaryKey` variant.

### 3.2 `app_config` — runtime overrides for the admin config form

| Column | Type | Null | Index | Purpose |
|---|---|---|---|---|
| `key` | String(64) | no | PK | Env var name being overridden (e.g. `LOG_LEVEL`). |
| `value` | Text | yes | — | Raw env-style override value. |
| `updated_at` | DateTime(tz) | no | — | UTC last-change time. |
| `updated_by` | String(320) | yes | — | Admin email who set the override. |

---

## 4. Design decisions (and why)

### 4.1 `user_email` is nullable; system actions store NULL
`data_refresh_started` fires in the startup prefetch, **before** Streamlit and the
auth gate boot, so there is no signed-in user. Rather than invent a fake actor, a
system action stores `user_email = NULL`; the viewer renders those as `system`.

### 4.2 `event` is a String, not an enum
A new tracked action is one constant in `backend/observability` — no migration and
no Postgres `ALTER TYPE`. The constants make typos an `ImportError` at the call
site, which is the safety the enum would otherwise provide.

### 4.3 `metadata_json`, not `metadata`
SQLAlchemy's `DeclarativeBase` reserves the `metadata` attribute, so the column
attribute is `metadata_json` (mirroring `scan_runs.params_json`).

### 4.4 Redaction at the persistence boundary (the AC)
Metadata is routed through the existing public redactor
`backend.scanning.result_contract.normalize_secret_safe_json` — the *same* helper
that protects `scan_runs.params_json`. It masks credential-named keys and redacts
secret-shaped strings, then returns strict JSON. The recorder applies it before
**both** sinks (the DB row and the OBS-001 log event), so a token can never become
durable audit evidence. This satisfies "sensitive values are redacted" for *every*
event, not just config changes.

### 4.5 Best-effort recording
A failure to persist an audit row must never break the user's action (a login, a
scan, a download). The recorder swallows DB errors (logging a redacted warning) and
returns `False`, mirroring how scan persistence is best-effort. First-run event
call sites that can fire before normal UI reads (`data_refresh_started`,
`login_success`, `login_denied`) try `ensure_database_schema()` before recording so
fresh databases get the `audit_logs` table before the durable write.

### 4.6 Two sinks per event
The recorder writes the `audit_logs` row **and** emits an OBS-001 `log_event`, so
audit actions also appear in the live structured-log stream a deployment ships to a
log aggregator — with no second redaction implementation.

### 4.7 Streamlit-rerun de-duplication
Streamlit re-runs the whole script on every interaction, so level-triggered events
(`login_success`, `admin_page_accessed`) would otherwise record a row per rerun.
A small framework-free helper `should_record_once(session_state, key)` records them
exactly once per browser session. Audit-critical level-triggered events use
`record_audit_event_once(...)`, which marks the session key only after the durable
row is written so a transient DB failure can retry on the next rerun.
Button/download/form events are edge-triggered and need no dedup.

### 4.8 Minimal, non-secret config form
`config_changed` had no trigger because settings are env-driven and frozen. OBS-003
adds an admin form that edits a **whitelist** of operational keys (`LOG_LEVEL`,
`LOG_FORMAT`) only. Values are validated with the *startup* parsers, stored in
`app_config`, applied into `os.environ` (so the env-reading `get_settings()` picks
them up live), and re-applied on startup via `apply_config_overrides`. Auth/infra
keys and secrets are excluded so the form can never become an auth-bypass lever or
a secret store.

**Later extension (ALERT-002).** The same whitelist now includes alert enable/content
and the non-secret Telegram/email destinations. Destination values remain plaintext
operational config, but are masked when copied into audit/log metadata or save feedback.

---

## 5. Events → call sites

New event constants live in `backend/observability` (reusing the existing
`EVENT_DATA_REFRESH_STARTED`).

| Event | Call site | Trigger / dedup |
|---|---|---|
| `login_success` | `app.py` `main()` after `require_authorized_user` | Once per session (session-state key per email). |
| `login_denied` | `backend/auth/session.py` denial branch | Once per session per email; the OBS-001 `auth_denied` log stays. |
| `manual_scan_started` | `app.py` Run-button (`pending_run`) | Edge-triggered. Metadata: `screener_key`, `universe_key`. |
| `data_refresh_started` | `app.py` `prefetch_data_assets()` | System event (`user_email=NULL`); schema bootstrap before best-effort write. |
| `config_changed` | `backend/admin/config_service.update_config_value` | Form submit. Metadata: `setting`, `old_value`, `new_value` (redacted). |
| `export_downloaded` | live results + History + Comparison + Validation CSVs | `st.download_button` returns True on click. Metadata: `file_name`, `row_count`, `kind`. AUTH-003 renders/builds these only for `EXPORT_RESULTS`. |
| `admin_page_accessed` | `app.py` before each admin view | First access per page per session. Metadata: `page`. |

---

## 6. Model boundaries

```
UI / app.py / ui/*          ← edge-trigger events; owns Streamlit session_state dedup
backend/auth/session.py     ← login_denied (uses injected st_module.session_state)
        │ plain values (event, user_email, metadata)  — NO Streamlit in backend/
        ▼
backend/audit (recorder)    ← redact metadata, best-effort write + log_event
        │
        ▼
backend/storage/repository  ← create_audit_log_entry / get_recent_audit_logs / *_config_override
        │
        ▼
backend/storage/models      ← AuditLog, AppConfig (same Base as ScanRun/ScanResult)
```

This mirrors the SCAN-001 layering: the UI never writes SQL, `backend/` never
imports Streamlit, and the repository is the only place that builds queries.

---

## 7. Acceptance-criteria mapping

| Acceptance criterion | How it is satisfied |
|---|---|
| Audit events include user email | `audit_logs.user_email`; system events store NULL by design (§4.1). |
| Audit events include timestamp | `audit_logs.created_at` (tz-aware UTC, indexed). |
| Audit events include action metadata | `audit_logs.metadata_json` (per-event context). |
| Sensitive values are redacted | `normalize_secret_safe_json` at the recorder boundary (§4.4), unit-tested with a secret-bearing example. |

---

## 8. Verification

- `tests/test_audit_repository.py` — model/repository round-trip, filters, id
  tie-break, system event, **metadata redaction**.
- `tests/test_audit_recorder.py` — dual-sink, redaction, best-effort swallow,
  `should_record_once` and success-only `record_audit_event_once` dedup.
- `tests/test_config_service.py` — validate/persist/apply/audit, invalid + unchanged
  + non-editable cases, `apply_config_overrides`.
- `tests/test_app_audit_page.py` / `tests/test_app_config_page.py` — admin guard +
  render flows.
- `tests/test_scan_storage_migrations.py` — upgrade/downgrade + ORM-vs-migration
  schema equality now cover `audit_logs` / `app_config`.

Full gate suite (identical to CI) is green: `pytest` (872 passed, 87% coverage ≥
84% floor), `ruff`, `mypy`, `bandit`, `compileall`.

---

## 9. Files in this change

| File | Change |
|---|---|
| `backend/storage/models.py` | +`AuditLog`, +`AppConfig`. |
| `backend/storage/repository.py` | + audit/config CRUD helpers. |
| `backend/storage/__init__.py` | Re-export new models + helpers. |
| `backend/observability/__init__.py` | + OBS-003 event constants. |
| `backend/audit/` | **New** — best-effort, secret-safe recorder. |
| `backend/admin/` | **New** — runtime config override service. |
| `ui/audit_page.py`, `ui/config_page.py` | **New** — admin viewer + config form. |
| `migrations/versions/20260617obs003_create_audit_logs.py` | **New** — create both tables. |
| `app.py`, `backend/auth/session.py`, `ui/history_page.py` | Wire the seven events. |
| `tests/test_audit_*.py`, `tests/test_config_service.py`, `tests/test_app_*_page.py` | **New** tests. |
| `docs/architecture/*` + `README.md` | This doc, the audit-log LLD, HLD/index/cross-refs. |

---

## 10. Notes for the reviewer (Codex)

- **Editable config whitelist is deliberately tiny.** ALERT-002 later added four
  notification keys; secrets and auth/infra keys still stay out (§4.8).
- **`data_refresh_started` is the only system event.** It is recorded from the CLI
  prefetch path with `user_email=NULL`; the viewer renders it as `system`.
- **Dedup is per session, not global.** Two browser sessions for the same user each
  record one `login_success`. That is intended (a session = a sign-in). The dedup
  marker is written only after durable persistence succeeds, so failed audit writes
  are eligible to retry.
