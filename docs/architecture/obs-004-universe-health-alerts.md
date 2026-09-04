# OBS-004 — Universe mapping-health alerts

| | |
|---|---|
| **Ticket** | OBS-004 (issue [#119](https://github.com/DoRmAmMu1997/Streamlit-Scanner-App/issues/119)) |
| **Source** | [`backend/data_quality/universe_health.py`](../../backend/data_quality/universe_health.py) · [`backend/storage/models.py`](../../backend/storage/models.py) (`UniverseHealthSnapshot`) · [`backend/jobs/run_daily_scan.py`](../../backend/jobs/run_daily_scan.py) · [`backend/notifications/`](../../backend/notifications/) · [`app.py`](../../app.py) |
| **Migration** | `20260904obs004_create_universe_health_snapshots` |
| **Status** | Shipped |
| **Related** | [data-quality.md](components/data-quality.md) · [universe-management.md](components/universe-management.md) · [observability.md](components/observability.md) · [notifications.md](components/notifications.md) · [storage-persistence.md](components/storage-persistence.md) · [audit-2026-06.md](audit-2026-06.md) |

## 1. The problem

When Dhan's instrument master stops listing a symbol — a merger, a delisting, a
ticker change — `refresh_universe_files()` writes it into the universe CSV as
`mapping_status='missing_security_id'`, and `mapped_only()` then filters it out
of every scan. That behaviour is correct: we cannot fetch candles for a security
id we do not have.

What was missing is that **nothing said so**. Before OBS-004:

- `universe_status()` / `all_universe_statuses()` had exactly one non-test
  caller, `ui/status_panel.py` — the interactive Streamlit sidebar.
- `backend/jobs/run_daily_scan.py` emitted no mapping signal at all.
- `backend/notifications/` never referenced `mapping_status`.

So in production — where the Render cron runs `refresh_universe_files()` and then
`run_daily_scan` with no human watching a sidebar — a universe could quietly
shrink indefinitely. It already had: **~3% of the Hemant Good 200 list was
unscanned** when this was found, and two more names (`JBCHEPHARM`, `GUJGASLTD`)
dropped out mid-audit without a sound.

Worth stating clearly, because it shapes the design: **the drop-outs were not a
bug.** Both symbols were genuinely absent from the 2026-08-24 instrument master
by symbol *and* company name, and the snapshot itself was intact (213,213 rows).
This is a real-world vendor event that the system should *report*, not prevent.

## 2. Design

### 2.1 Three pieces, split by requirement

| Piece | Kind | Why separate |
|---|---|---|
| `collect_universe_health()` | pure | Reads CSVs, returns counts + names. Anything may call it. |
| `detect_mapping_regressions()` | pure | Compares today to a baseline. Trivially testable, no I/O. |
| `check_universe_health(session)` | stateful | Needs a database session, because "worse than last time" needs a durable baseline. |

### 2.2 Why a database table and not a file

The Render daily-scan cron runs on an **ephemeral filesystem with no disk**
(`render.yaml` attaches the disk to the web service only). Anything written next
to the universe CSVs is gone before the next run, so a file-based baseline would
mean the alert could never fire in the one environment that needs it. The shared
Postgres is the only state that survives between runs.

`universe_health_snapshots` is **append-only** rather than one upserted row per
universe. The question that always follows "GUJGASLTD dropped out" is "when?",
and keeping the history answers it for free. The read path only ever wants the
newest row per universe, which `ix_universe_health_snapshots_key_captured`
serves directly.

### 2.3 The two quiet rules

Both exist to keep the alert credible enough that nobody mutes the channel:

1. **A universe with no previous row never regresses.** The first check has no
   baseline. Treating "absent" as zero would alert on every pre-existing unmapped
   symbol on first run.
2. **Only an increase counts.** A universe sitting at a steady three unmapped
   symbols is already-known damage and stays silent. Recovery (the count going
   down) is good news, not an alert.

### 2.4 Only the alerting path owns the baseline

This is the subtle one. Whoever *writes* the baseline defines what "last time"
means. If the morning Streamlit prefetch also recorded a snapshot, a symbol that
dropped out at 09:00 would already be part of the baseline by the time the
evening job ran — and the alert would never fire on any machine where both run.

So:

- **`app.py` prefetch** calls `collect_universe_health()` + `log_universe_health()`
  — observability only, no persistence.
- **`run_daily_scan`** calls `check_universe_health()` — collect, log, compare,
  **then** record. Reading the baseline before writing today's snapshot is what
  makes the alert fire exactly once.

### 2.5 Bounded by construction

`MAX_REPORTED_SYMBOLS = 25` caps both the stored JSON and the alert text. A
universe whose CSV went badly wrong must not be able to write an unbounded blob
into Postgres or a multi-page message into Telegram; past that point the count
alone tells the story. Only symbol strings and counts are stored — never prices,
never credentials — and the alert text still goes through `redact_text` like
every other notification.

## 3. Failure posture

Every entry point is wrapped: `_check_universe_health()` in the daily job catches
broadly and returns `()`, and the prefetch helper does the same. A universe CSV
that will not parse is a reason to warn, never a reason to skip the night's scan.
The health check can therefore never change the job's exit code — the same
contract ALERT-001 gives notifications.

## 4. What an operator sees

Structured log events on every run (`universe_health_checked`, one per universe,
carrying `rows` / `mapped` / `unmapped`), a `universe_mapping_regressed` warning
when something got worse, a printed line in the job's stdout, and a
**Universe warnings** block in the daily alert:

```
Universe warnings:
  - hemant_good_200: 6 -> 8 unmapped (+2); GUJGASLTD, JBCHEPHARM
```

That block renders even at the ALERT-002 summary-only content level. A shrinking
universe is a warning about the integrity of *this scan*, not a per-stock result,
so it is not suppressed alongside the results list.

## 5. Alternatives considered

| Option | Why not |
|---|---|
| Alert on an absolute threshold ("more than 5 unmapped") | Every universe would need its own tuned number, and a slow drift below the threshold stays invisible — which is the exact failure being fixed. |
| Store the baseline in `app_config` | That table is admin *config overrides*, read wholesale by `apply_config_overrides()`. Injecting job state into it risks a key being applied as a setting. |
| Upsert one row per universe | Cheaper, but throws away the "when did this happen?" answer for no real saving — these rows are tiny and written once a day. |
| Fail the scan when a universe shrinks | Wrong severity. A vendor delisting is normal; the scan over the remaining symbols is still valid and useful. |
| Diff the CSVs in git | Only works on a developer machine. The production cron has no checkout and no disk. |
