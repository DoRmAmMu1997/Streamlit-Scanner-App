# LLD — UI pages (`ui/`)

| | |
|---|---|
| **Component** | Scan-history page + validation dashboard + shared UI helpers |
| **Source** | [`ui/history_page.py`](../../../ui/history_page.py), [`ui/validation_page.py`](../../../ui/validation_page.py), [`ui/common.py`](../../../ui/common.py) |
| **Layer** | UI (`ui/`) |
| **Status** | Stable (SCAN-004 history · REF-001 split · VALID-003B/004 validation dashboard) |
| **Related** | [HLD](../high-level-design.md) · [app-orchestration.md](app-orchestration.md) · [storage-persistence.md](storage-persistence.md) · [scan-service-and-provenance.md](scan-service-and-provenance.md) · [charts-visualization.md](charts-visualization.md) · [health-monitoring.md](health-monitoring.md) · [security.md](security.md) · [audit-log.md](audit-log.md) |

> The `ui/` package also contains [`chart_cache.py`](charts-visualization.md) (charts), [`health_page.py`](health-monitoring.md) (admin health), and the OBS-003 admin pages [`audit_page.py` + `config_page.py`](audit-log.md) (Audit log viewer + runtime settings form) — documented in their own LLDs. The scanner page itself lives in [`app.py`](app-orchestration.md). This LLD covers the **scan-history page**, the **validation dashboard**, and the **shared display helpers** in `ui/common.py`.

## 1. Purpose & responsibilities

- **`history_page.py`** — the SCAN-004 **read-only** audit view: filter recorded runs, list them, click one to inspect its persisted results + download CSV. Pure data-shaping helpers are separated from rendering so they unit-test without a browser.
- **`validation_page.py`** — the VALID-003B/004 **read-only** Validation / Signal Performance dashboard: filter stored forward-return metrics by screener / universe / horizon / signal-date and render the screener-level summary table, return distribution, win rate by horizon, benchmark-relative rows, monthly signal counts, sector concentration, best/worst signals, and CSV export. It reads through `summarize_validation_dashboard()` only — no raw SQL — and never triggers a forward-return compute pass from the UI. Same pure-helper / render split as the history page.
- **`ui/common.py`** — display helpers needed by both the scanner page and the history page (which must not import each other or `app.py`): CSV-injection escaping, secret-redaction wrapper, BUY/SELL emoji badges, decimal column config, and provenance-column hiding.

## 2. Position in the system

```mermaid
flowchart TD
    APP["app.main(): view == Scan history"] --> RHP["_render_history_page()"]
    RHP --> FILTERS["filters from history (distinct keys), not the live registry"]
    RHP --> REPO["get_latest_scan_runs + count_scan_results_for_runs"]
    REPO --> ROWS["_history_run_row (plain dicts, inside session)"]
    ROWS --> TABLE["runs table (keyed by filter signature)"]
    TABLE --> DETAILS["_render_history_run_details: metrics + error + results + CSV"]
    COMMON["ui/common: _csv_safe / _redact_secrets / _emoji_rating / _decimal_column_config"] --> RHP
    COMMON --> APP
```

## 3. Public interface

### `history_page.py`
`_render_history_page()` (the view) · `_render_history_run_details(row, *, symbol_filter="")` · pure helpers `_history_filter_kwargs(...)` (widgets → repository filters), `_history_filter_signature(...)` (filter hash for table widget key), `_history_run_row(run, shortlisted)`, `_history_runs_frame(rows, *, error_redactor)`, `_format_utc_timestamp`, `_format_run_duration`, `_as_utc`. Status badges `_HISTORY_STATUS_BADGES`; preview cap `_HISTORY_ERROR_PREVIEW_CHARS=80`.

### `validation_page.py`
`_render_validation_page()` (the view) · pure helpers `_validation_filter_kwargs(...)` (widgets → dashboard kwargs), `_validation_summary_frame(summary)` (summary → 18-column display frame), `_validation_distribution_frame(...)`, `_validation_horizon_frame(...)`, `_validation_benchmark_frame(...)`, `_validation_time_series_frame(...)`, `_validation_sector_frame(...)`, `_validation_best_worst_frame(...)`, `_validation_summary_csv(...)`, `_format_pct(value)` (4-dp `Decimal` → `"x.xx%"`, `None` → em-dash), `_format_signal(signal)` (best/worst → `"SYMBOL x.xx% (date)"`). Column contract `_SUMMARY_COLUMNS`. Empty states: no rows yet / no rows for filters / no computed rows yet / benchmark-excess unavailable.

### `ui/common.py`
`_csv_safe(df)` / `_escape_cell` (formula-injection escaping) · `_redact_secrets(text)` (wraps `redact_text` + `auth_secret_values`) · `_emoji_rating(df)` (BUY/SELL badges) · `_decimal_column_config(df)` (2-dp display) · `_drop_provenance(df)` (drops the internal `provenance` **and** `provenance_json` columns from the table + CSV — machine-readable evidence, not display data).

## 4. Key design decisions & trade-offs

| Decision | Rationale | Alternative rejected |
|---|---|---|
| **Pure helpers split from rendering** | History and validation filter/frame helpers test without Streamlit, while render-level tests still prove the service-call plumbing. | Inline in the render fn — untestable and easier to miswire. |
| **Filter options from history, not the live registry** | A deleted/renamed screener's history stays inspectable; a broken screener module can't take down the audit view. | Use `discover_screeners` — couples audit to live code. |
| **Convert ORM → plain dicts inside the session** | After `session_scope()` closes, touching lazy attrs (esp. `run.results`) raises `DetachedInstanceError`; capture everything while open. | Pass ORM objects to render — detached errors. |
| **Table keyed by filter signature** | Streamlit keeps selection by widget key; a new filter set mints a fresh table so a stale row-2 selection can't open the wrong run. | Reuse key — wrong-run selection. |
| **Read-only + relies on WAL** | SCAN-002 enabled SQLite WAL so this view stays usable while a scan (e.g. the daily job) writes concurrently. | Block on writer — page hangs. |
| **Redact full message *before* truncating** | Truncating a long bare secret first could leave a prefix the exact-value redactor no longer matches. | Truncate then redact — partial leak. |
| **`symbols_scanned=None` shows "—"** | Pre-SCAN-004 runs didn't store it; an em-dash beats a misleading `0`. | Show 0 — wrong. |
| **CSV formula-injection escaping** | A cell starting `=`/`+`/`-`/`@`/tab executes when opened in Excel/Sheets; prefix with `'` (idempotent). | Raw CSV — spreadsheet code execution. |
| **`OperationalError` → friendly migrate hint** | Fresh/outdated DB is the common cause; tell the operator to run `alembic upgrade head`. | Raw traceback — confusing. |

## 5. Failure modes

- DB not migrated → caught `OperationalError`, shows the `alembic upgrade head` hint. The validation page catches both the filter-option read and the later metrics-summary read because a partially migrated DB can fail at either point.
- No runs / no match → contextual info message.
- Stale selection index → bounds-checked, render skipped (with the signature key as belt-and-braces).
- Failed/partial run → full **redacted** error shown prominently; results table still attempted.

## 6. Testing

- [`tests/test_app_history_page.py`](../../../tests/test_app_history_page.py) — filter mapping, signature stability, row shaping, run details, redaction/truncation order.
- [`tests/test_app_validation_page.py`](../../../tests/test_app_validation_page.py) — VALID-003B/004 percentage/signal formatting, filter mapping, summary/dashboard frame shaping, render-level `summarize_validation_dashboard` plumbing, CSV-safe export/audit, friendly schema-error handling, and empty-state copy. View wiring (selector options + dispatch) is covered in [`tests/test_app_orchestration.py`](../../../tests/test_app_orchestration.py).
- `ui/common` helpers are exercised via the scanner and history page tests (`test_app_orchestration.py`, golden CSV checks).

## 7. Extension points

A new history filter = a widget + a branch in `_history_filter_kwargs` (+ the repository filter) + include it in the signature. A new shared display helper belongs in `ui/common.py`, never imported across page modules.
