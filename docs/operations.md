# Operations guide

How to run, schedule, deploy, and maintain the scanner outside of clicking
around the Streamlit UI. Everything here assumes the repo is cloned and
dependencies are installed per the README's Setup section.

---

## Running scans headless

The daily scan command runs without Streamlit, a browser, or login:

```bash
python -m backend.jobs.run_daily_scan            # built-in deterministic set
python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml
```

Exit code `0` means every scheduled scan finished without a fatal error;
non-zero means at least one scan failed, which is what schedulers should alert
on. Results land in the same scan-history database the UI reads (SCAN-002),
so runs show up on the Scan history page automatically.

### Scheduling on Windows (Task Scheduler)

```powershell
$action = New-ScheduledTaskAction -Execute "python" `
    -Argument "-m backend.jobs.run_daily_scan" `
    -WorkingDirectory "C:\path\to\Streamlit-Scanner-App"
$trigger = New-ScheduledTaskTrigger -Daily -At 18:30
Register-ScheduledTask -TaskName "scanner-daily-scan" -Action $action -Trigger $trigger
```

### Scheduling on Linux/macOS (cron)

```cron
# Weekdays at 18:30 IST, after market close + data settlement.
CRON_TZ=Asia/Kolkata
30 18 * * 1-5  cd /path/to/Streamlit-Scanner-App && python -m backend.jobs.run_daily_scan >> /var/log/scanner-daily.log 2>&1
```

`CRON_TZ` is supported by common Linux cron implementations. If the scheduler
does not support it, including the cron shipped with macOS, set the host timezone
to `Asia/Kolkata` or translate 18:30 IST into the host timezone explicitly.

Set `LOG_FORMAT=json` for scheduled runs if a log aggregator will read the
output; the default "auto" already picks JSON when `APP_ENV=production`.

---

## Speeding up the candle prefetch (PERF-001)

`python app.py` backfills ~10 years of candles for every mapped stock before
the UI starts. By default that fetch is sequential. To overlap Dhan network
latency with local parquet writes:

```env
SCANNER_DHAN_FETCH_WORKERS=4
```

Worth knowing before turning it up:

- The **global request rate does not increase**. All workers share one request
  pacer that enforces `SCANNER_DHAN_REQUEST_DELAY_SECONDS` (default 0.5s)
  across threads, so Dhan sees the same request spacing as sequential mode.
  The win comes from overlapping wait time with parquet I/O and response
  parsing, which matters most on the first 10-year download.
- Values are clamped to 1-8. `1` (the default) is the long-standing
  sequential path, byte-for-byte.
- Scan-time loads are mostly local cache hits, so workers help them far less
  than they help the prefetch.

---

## Database: SQLite locally, Postgres when shared

Local default: `sqlite:///data/scanner.db` (WAL mode, created automatically,
migrations applied on startup). That is the right answer for one user on one
machine.

Move to Postgres when the daily job and the UI run on different machines, or
when more than one person reads scan history:

```env
DATABASE_URL=postgresql+psycopg://scanner:<password>@db-host:5432/scanner
```

The normal pinned setup installs `psycopg[binary]`, which supplies the psycopg 3
driver used by this URL. Deployment images should use the same
`requirements.txt` plus `constraints.txt` install documented in the README.

The schema is managed by Alembic; both the app and the daily job run
`alembic upgrade head` equivalent automatically at startup. To pre-provision
or debug: `python -m alembic upgrade head`.

### Backing up scan history

SQLite: copy `data/scanner.db` while the app is idle, or use the safe online
method:

```bash
sqlite3 data/scanner.db ".backup data/scanner-backup.db"
```

The candle cache (`data/cache/daily/*.parquet`) is *re-downloadable* and does
not need backup; the scan-history database is the part you cannot regenerate.

---

## Credential rotation

| Credential | Where it lives | How to rotate |
|---|---|---|
| Dhan access token | `Dependencies/.env` (`DHAN_ACCESS_TOKEN`) | Tokens expire periodically. Run `python Dependencies/dhan_token_setup.py`, which walks the OAuth flow and rewrites the token in `.env`. |
| SerpAPI key | `Dependencies/.env` (`SERPAPI_API_KEY`) | Generate a new key in the SerpAPI dashboard, replace the value, restart the app/job. |
| Google OIDC | `.streamlit/secrets.toml` | Rotate the client secret in Google Cloud Console, update `secrets.toml`, restart Streamlit. |

None of these files are committed; `.gitignore` covers them. After rotating,
no code changes are needed - settings are re-read at startup.

---

## CI gates and the multi-branch workflow

The "Quality and security" workflow runs the same gates you can run locally:

```bash
python -m pre_commit validate-config .pre-commit-config.yaml
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
```

These gates were ratcheted up over several PRs (QUAL-001/002/003, REF-001).
A consequence worth understanding when several branches are in flight at once:
**a branch forked before a gate landed can pass its own CI and still fail
after merging main**, because the new gate now checks code the gate had never
seen. That is the gate working, not a regression in the gate. The routine
that avoids surprise failures:

1. Merge (or rebase onto) current `main` before finishing a branch.
2. Run the block above locally - it is identical to CI.
3. Note that the pytest run includes `tests/test_supply_chain_policy.py`,
   which asserts the CI workflow's exact commands and the pin list in
   `constraints.txt`. If you deliberately change a CI command or add a dev
   dependency, update that policy test (and `constraints.txt`) in the same
   commit - that is the test doing its job of making such changes explicit.
