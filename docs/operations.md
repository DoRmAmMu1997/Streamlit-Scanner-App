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

`config/daily_scans.yaml` is the committed Render/default schedule. It contains
no secrets, enables deterministic screeners, and keeps AI-heavy jobs disabled by
default. Copy/edit it or point `--config` at another file when a deployment needs
a custom schedule.

Exit code `0` means every scheduled scan finished without a fatal error;
non-zero means at least one scan failed, which is what schedulers should alert
on. Results land in the same scan-history database the UI reads (SCAN-002),
so runs show up on the Scan history page automatically.

## Computing forward-return validation rows

The validation dashboard is read-only: it shows rows already present in
`signal_forward_returns` and never starts a Dhan-backed compute pass from a
browser rerun. Run the VALID-004 job after daily scans, or on a separate
schedule, to fill missing/pending measurements:

```bash
python -m backend.jobs.compute_forward_returns --limit 500
```

Useful options:

```bash
python -m backend.jobs.compute_forward_returns --limit 1000 --as-of 2026-06-19
python -m backend.jobs.compute_forward_returns --horizon 20 --horizon 60
```

Exit code `0` means the batch ran and committed its best-effort results;
pending and insufficient rows are normal validation states, not scheduler
failures. Exit code `1` means setup or the batch boundary failed (for example,
credentials, schema, or database/session setup), and the scheduler should alert.

The command bootstraps the database schema, builds the same Dhan-backed
`DailyDataLoader` used by scans, calls the idempotent
`compute_pending_forward_returns()` service, and prints a secret-safe summary.
It does not re-run screeners, mutate scan history, or enable any AI jobs. Render
does not get a second cron service in this task; deployments can schedule this
command explicitly when they are ready for validation backfills.

## Inventorying official SEBI IPO filings

The consolidated component boundary is documented in the
[IPO Screener LLD](architecture/components/ipo-screener.md). Run the backend-only
IPO-002 inventory command manually or from a scheduler:

```bash
python -m backend.jobs.scan_ipo_filings
```

The default inclusive window starts seven days before the newest stored SEBI
filing publication date and ends today. A new database starts 30 days back. Use
an explicit window when replaying an incident, or request the full archive
deliberately:

```bash
python -m backend.jobs.scan_ipo_filings --from-date 2026-06-01 --to-date 2026-06-30
python -m backend.jobs.scan_ipo_filings --full-history
```

The command scans fixed official DRHP, RHP, and final-offer categories. It stores
the SEBI filing-detail URL and publication date but does **not** download or parse
PDFs. Each category commits atomically and independently. Exit code `0` means all
three categories succeeded; exit code `1` means at least one failed even if the
others committed. Failed categories emit structured lifecycle logs and durable
system audit events containing only category/date context and exception class.

Recovery is a normal rerun: correct the network/upstream parsing problem or
resolve the reported issue/document ownership conflict, then run the same date
window again. The seven-day overlap plus deterministic fingerprints makes the
operation idempotent. Avoid `--full-history` in routine schedules because it is
an explicit archive-import mode bounded by the client's 200-page safety cap.

## Downloading a registered IPO prospectus

IPO-003 intentionally adds no command-line job or scheduler hook. A backend
caller that already knows the parent issue and document ids explicitly invokes
the typed repository service:

```python
from backend.ipo import download_document

result = download_document(issue_id=42, document_id=108)
print(result.file_path, result.content_sha256, result.cache_hit)
```

Only registered DRHP/RHP rows are eligible. The service validates SEBI DNS,
every redirect, detail-page iframe shape, response type/size, PDF magic, and
cache-path containment. It then stores the file beneath
`DATA_DIR/ipo/documents/<sha256>.pdf`; `page_count` stays null because this sprint
does not parse PDFs. Repeating the call rehashes a candidate cache file and uses
it without HTTP only when the bytes still match the stored digest.

On failure, the row becomes `download_failed` with no trusted file/hash metadata.
The durable audit contains only ids, document type, and a safe error code. Fix
the network/upstream/storage condition and call the service again. Do not delete
hash-named files manually to repair database state: one file may be shared by
multiple rows, and orphan cleanup is intentionally a later maintenance feature.
If the filing URL or document type is corrected during a transfer, the service
returns `source_changed`, preserves the revised row as `not_downloaded`, and
requires a fresh explicit call. This is expected provenance protection rather
than a cache failure. A secondary audit-store outage does not change the database
status or the stable error returned to the caller.

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

## Daily scan notifications (ALERT-001)

After the daily scan finishes, the job can send a summary (status, symbols
scanned, shortlisted count, top-10 ranked results, failures, and an app link) to
**Telegram and/or email**. Both channels are **opt-in**: each fires only when all
of its variables are set, and a send failure is logged but never changes the
job's exit code. A failed/aborted run (bad config, DB down, crash) sends a
failure alert too.

Set the variables for the channel(s) you want. Use `Dependencies/.env` for a
local Python run, the root `.env` for Docker Compose, or the Render dashboard
`sync: false` values on the `scanner-daily-scan` cron service:

```bash
APP_URL=https://your-scanner.example.com      # used for the "Open the app" link

# Preferences (ALERT-002). Defaults: on, full. An admin can also change these
# (and the destination) at runtime from Admin settings — see the note below.
ALERT_ENABLED=true                            # set false to silence alerts without removing creds
ALERT_CONTENT=full                            # full = include top results; summary = counts only

# Telegram (bot token from @BotFather; numeric chat id from getUpdates)
TELEGRAM_BOT_TOKEN=123456:AA...
TELEGRAM_CHAT_ID=987654321

# Email over SMTP (STARTTLS, e.g. port 587). SMTP_FROM defaults to SMTP_USER.
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=you@example.com
SMTP_PASSWORD=app-password
ALERT_EMAIL_TO=you@example.com
```

`TELEGRAM_BOT_TOKEN` and `SMTP_PASSWORD` are masked by the shared redactor, and
the message body is built only from non-secret summary fields, so secrets never
appear in a notification. Leave a channel's variables unset to disable it; with
none set, the scan runs normally and notification is skipped.

**Runtime preferences (ALERT-002).** An admin can change `ALERT_ENABLED`,
`ALERT_CONTENT`, and the non-secret destinations `TELEGRAM_CHAT_ID` /
`ALERT_EMAIL_TO` from the **Admin settings** page without a redeploy; changes are
validated, audited, applied immediately, and replayed on the next start (the daily
job included). Destination values are masked in audit/log/save feedback even though
`app_config` stores them for delivery. Channel credentials stay environment-only.

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

## AI output validation attempts (AI-004)

Malformed or incomplete AI verdict JSON is retried within a small, bounded
attempt budget:

```env
SCANNER_AI_MAX_ATTEMPTS=2
```

The default `2` means one initial attempt plus one validation retry. Setting
1 disables validation retries; larger values are clamped to `1`-`3`. Raising
the value can recover another transient formatting failure, but every additional
attempt re-runs the agentic loop and consumes Agent SDK credit. SDK, CLI,
usage-limit, and unsafe-research failures are never retried because another
model call cannot repair them.

The AI agents also **quarantine untrusted research evidence** (TEST-003): scraped
Screener.in / SerpAPI / concall text is scanned for model-directed instructions
before it reaches the model, and on a hit the agent fails closed with a
`PromptInjectionEvidence` error rather than producing a verdict. These surface as
ordinary `error` AI evaluations in `ai_evaluations`; the hostile text is never
logged.

---

## Candle data quality (DATA-001)

Every OHLCV frame is validated at the loader boundary before a screener runs, so
malformed or stale vendor data cannot produce a false signal. This changes how a
run's status should be read:

- **Fatal findings** (high < low, NaN/inf, duplicate dates, negative volume,
  missing columns) **quarantine** that symbol's frame: it is dropped from the
  scan and the run is downgraded to `PARTIAL` — or `FAILED` if no usable symbols
  remain. So a `PARTIAL` run may mean "bad data for some symbols", not a bug.
- **Warning findings** (stale latest candle, large calendar gaps, suspicious
  overnight price jumps) do **not** block scanning; they are recorded only.

Where to look:

- **Admin health page** → *Candle data quality* shows the newest persisted
  receipt (checked/usable counts + a capped, redacted findings sample).
- **Logs**: `candle_data_quality_warning` / `candle_data_quality_failed` events
  carry the finding codes (never raw prices).
- **Database**: each run's `scan_runs.data_quality_json` holds the full receipt
  for that run.

Stale findings tolerate a normal long weekend (see `STALE_LATEST_TOLERANCE_DAYS`),
so a routine off-day run does not flag the whole universe as stale.

---

## Audit log (OBS-003)

Important user actions — sign-ins (`login_success` / `login_denied`), manual scans
(`manual_scan_started`), the startup data refresh (`data_refresh_started`), config
changes (`config_changed`), CSV exports (`export_downloaded`), and admin-page
access (`admin_page_accessed`) — are recorded with the actor email, a UTC
timestamp, and redacted metadata.

Where to look:

- **Audit log page** (admins) → top view selector → *Audit log*: filter by event,
  email, and row limit. System actions (the startup refresh) appear as `system`.
- **Logs**: the same events are emitted to the structured log stream.
- **Database**: the `audit_logs` table (same database as scan history). It is
  covered by the same backup as scan history (see *Backing up scan history*); no
  separate step.

Recording is best-effort: a database hiccup never blocks the user's action. First
run paths bootstrap the schema before audit writes, so a missing row means the
database could not be made ready or the write failed, not that the action failed.

**Runtime settings (admins).** The *Admin settings* page edits `LOG_LEVEL`,
`LOG_FORMAT`, and the ALERT-002 preferences/destinations at runtime; changes are
validated, stored in `app_config`, applied immediately, replayed on restart, and
recorded as `config_changed` (with destination values masked). Credentials and
auth/infra settings are intentionally not editable there — change those via
environment variables and restart.

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

---

## Docker / container deployment

### Local production mode with Docker Compose

Use Compose when you want the app and database to run locally the same way they
would in a small production deployment:

```bash
cp .env.example .env
cp .streamlit/secrets.example.toml .streamlit/secrets.toml
```

Edit `.env` with real Dhan credentials and either `ADMIN_EMAILS` or
`ALLOWED_EMAILS`, then edit `.streamlit/secrets.toml` with the Google OIDC
client details. Secrets stay in local files: `docker-compose.yml` mounts
`.streamlit/secrets.toml` read-only at `/app/.streamlit/secrets.toml` instead of
baking it into the image.

Start the stack:

```bash
docker compose up --build
```

The two long-lived services are:

- `scanner-ui` - builds the local `Dockerfile`, listens on
  `${SCANNER_UI_PORT:-8501}:8501`, runs with `APP_ENV=production`,
  `AUTH_REQUIRED=true`, and stores app-generated files under `/data`.
- `postgres` - runs `postgres:16-bookworm` on the private Compose network with
  no host port. The UI reaches it through
  `postgresql+psycopg://...@postgres:5432/...`.

The volumes are intentionally separate:

- `scanner-data` backs `/data` for candle caches, fundamentals caches, and any
  local fallback artifacts.
- `postgres-data` backs `/var/lib/postgresql/data` so scan history survives
  container replacement.

Stop the stack without deleting those volumes:

```bash
docker compose down
```

Delete the containers and both named volumes when you deliberately want a clean
local production environment:

```bash
docker compose down --volumes
```

Run the daily job against the same Postgres database and `/data` volume:

```bash
docker compose run --rm scanner-ui python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml
```

ALERT-001 notifications work from this Compose job too. Fill the optional
notification section in the root `.env`; `docker-compose.yml` passes those
values into `scanner-ui`. Leave a channel blank to keep it disabled.

Troubleshooting notes:

- `docker compose config` prints the fully interpolated stack and catches a
  missing or malformed `.env` before containers start. Once `.env` contains real
  credentials, treat that output as sensitive and do not paste it into tickets or
  chat.
- `docker compose ps` shows whether `postgres` is healthy; the UI waits for that
  health check before booting to avoid first-start database races.
- `docker compose logs scanner-ui` is the fastest place to check production
  config failures, auth setup errors, or migration failures.
- Use `docker compose exec postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"`
  if you need a database shell; there is no host `5432` exposure by design.

### Single-container image checks

Build the production image from the repository root:

```bash
docker build -t streamlit-scanner-app .
```

The image runs `streamlit run app.py` directly, listens on `0.0.0.0:8501`, and
stores runtime data under `DATA_DIR=/data`. It defaults to production and
auth-required mode so a deployed container fails closed until the required
environment and Google OIDC secrets are supplied.

For a local smoke test:

```bash
docker run --rm \
  -p 8501:8501 \
  -e APP_ENV=development \
  -e AUTH_REQUIRED=false \
  -e DATA_DIR=/data \
  -v streamlit-scanner-data:/data \
  streamlit-scanner-app
```

For production, keep `/data` on persistent storage and point `DATABASE_URL` at
Postgres. The inline `-e` values are placeholders for a manual run; prefer the
host platform's managed secret/environment injection for long-lived deployments:

```bash
docker run -d --name streamlit-scanner-app \
  -p 8501:8501 \
  -e APP_ENV=production \
  -e AUTH_REQUIRED=true \
  -e DATA_DIR=/data \
  -e DATABASE_URL=postgresql+psycopg://scanner:<password>@db-host:5432/scanner \
  -e DHAN_CLIENT_ID=<client-id> \
  -e DHAN_ACCESS_TOKEN=<access-token> \
  -e ALLOWED_EMAILS=you@gmail.com \
  -e ADMIN_EMAILS=you@gmail.com \
  -e LOG_FORMAT=json \
  -v streamlit-scanner-data:/data \
  -v /absolute/path/secrets.toml:/app/.streamlit/secrets.toml:ro \
  streamlit-scanner-app
```

Container checklist:

- `DATA_DIR=/data` must be backed by a persistent Docker volume or host mount.
- `DATABASE_URL` should be Postgres for shared/deployed use; the pinned runtime
  install includes `psycopg[binary]`.
- `.streamlit/secrets.toml` must be supplied by a read-only bind mount or the
  hosting platform's secret injection. Do not bake it into the image.
- Dhan and optional SerpAPI/Claude settings should be environment variables or
  managed secrets, not files copied into the image.
- The image health check reads `http://127.0.0.1:8501/_stcore/health`.

Run the headless daily job with the same image and runtime configuration:

```bash
docker run --rm \
  --entrypoint python \
  -e APP_ENV=production \
  -e AUTH_REQUIRED=true \
  -e DATA_DIR=/data \
  -e DATABASE_URL=postgresql+psycopg://scanner:<password>@db-host:5432/scanner \
  -e DHAN_CLIENT_ID=<client-id> \
  -e DHAN_ACCESS_TOKEN=<access-token> \
  -v streamlit-scanner-data:/data \
  -v /absolute/path/secrets.toml:/app/.streamlit/secrets.toml:ro \
  streamlit-scanner-app \
  -m backend.jobs.run_daily_scan --config config/daily_scans.yaml
```

### Backing up scan history

SQLite: copy `data/scanner.db` while the app is idle, or use the safe online
method:

```bash
sqlite3 data/scanner.db ".backup data/scanner-backup.db"
```

Compose/Postgres: keep `postgres-data` as the durable volume and back it up with
Postgres tooling rather than copying live database files:

```bash
docker compose exec postgres pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB" > scanner.sql
```

For a local restore into a fresh Compose database, start from
`docker compose down --volumes`, bring the stack back up, then load the dump with
`docker compose exec -T postgres psql -U "$POSTGRES_USER" "$POSTGRES_DB" < scanner.sql`.

The candle cache (`data/cache/daily/*.parquet`) is *re-downloadable* and does
not need backup; the scan-history database is the part you cannot regenerate.

---

## Deploying to Render (managed)

`render.yaml` (DEPLOY-003) is a Render **Blueprint** that provisions the whole
stack from one file: a Streamlit **web service**, a managed **Postgres** database,
a **persistent disk** for the web service's candle cache, and a **cron job** for
the daily scan. It reuses the same production `Dockerfile` as Docker Compose, so
Render runs the exact image you can build locally. The design rationale lives in
[the deployment-runtime LLD](architecture/components/deployment-runtime.md#9-render-managed-deployment-deploy-003).

### First deploy

1. Push the repo to GitHub/GitLab and, in the Render dashboard, create a new
   **Blueprint** pointing at it. Render reads `render.yaml` and proposes the
   `scanner-db` database, `scanner-web` web service, and `scanner-daily-scan`
   cron job.
2. Fill in the `sync: false` env vars Render prompts for (they are never
   committed): `DHAN_CLIENT_ID`, `DHAN_ACCESS_TOKEN`, `ALLOWED_EMAILS` and/or
   `ADMIN_EMAILS`, and optionally `SERPAPI_API_KEY`. For ALERT-001, set
   `APP_URL` on the cron to the public scanner URL and fill either the Telegram
   pair (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`) or the SMTP set
   (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, optional
   `SMTP_FROM`, `ALERT_EMAIL_TO`) if you want scheduled notifications. Leave
   them blank to keep alerts disabled. `DATABASE_URL` is wired
   automatically from `scanner-db` — Render emits a bare `postgresql://` URL and
   the app rewrites it to the pinned psycopg v3 driver at startup, so no manual
   editing is needed. The database uses `ipAllowList: []`, so public internet
   database connections are closed while Render services keep using the internal
   `fromDatabase` connection string.
3. Add the Google OIDC secrets as a Render **Secret File** on `scanner-web` at
   filename `streamlit-secrets.toml` (same `[auth]` + `[auth.google]` shape as
   `.streamlit/secrets.example.toml`). Render exposes Docker secret files at
   `/etc/secrets/streamlit-secrets.toml`; the Blueprint's web `dockerCommand`
   passes that path with `--secrets.files` so Streamlit can load it. Set
   `redirect_uri` to your service URL, e.g.
   `https://scanner-web.onrender.com/oauth2callback`, and add that URL to the
   Google OAuth client's authorized redirect URIs.
4. Apply the Blueprint. On first boot either service runs `alembic upgrade head`
   automatically, so the fresh Postgres is initialized.

### Persistent disk and the candle cache

The disk attaches to `scanner-web` only (Render disks are single-attach) and is
mounted at `DATA_DIR`. Both default to `/data`; change them together to
relocate the persistent path. The disk starts empty, so after the first deploy
open a **Render Shell** on `scanner-web` and seed the universe CSVs the UI needs
for screener selection:

```bash
python -c "from backend.universe_builder import refresh_universe_files; refresh_universe_files()"
```

The candle cache then warms lazily as scans run and charts are viewed; to
pre-warm the full 10-year cache, run the same prefetch the local CLI uses against
the mounted disk.

### The daily-scan cron

`scanner-daily-scan` runs on Render's UTC schedule (default `30 13 * * 1-5` =
19:00 IST weekdays). It runs on an **ephemeral** filesystem with no disk, so its
`dockerCommand` first regenerates the universe CSVs, then runs
`python -m backend.jobs.run_daily_scan --config config/daily_scans.yaml`; it
fetches candles fresh from Dhan and writes results to the **shared Postgres**,
which is what the web UI's Scan history page reads. A cold candle cache costs the
cron time, not correctness.

The referenced `config/daily_scans.yaml` is committed so the Render image always
contains the cron schedule it runs. Use it as the production default, copy/edit it
for custom deployments, or change the Blueprint command to another committed
config path. AI-heavy jobs remain disabled by default; enable them deliberately
only after setting the optional AI/search secrets and cost limits you want.
If ALERT-001 variables are present on the cron service, it sends the summary
after successful/partial scans and fatal pre-scan failures; delivery errors are
logged but never change the cron exit code.

### Troubleshooting

- **Web service won't bind**: confirm the start command serves `$PORT` (the
  Blueprint's `dockerCommand` does); the image's own `8501` CMD is overridden.
- **Production config error on boot**: `scanner-web` fails closed until
  `DHAN_*`, `AUTH_REQUIRED=true`, and an allow/admin email are set, and the OIDC
  Secret File exists as `/etc/secrets/streamlit-secrets.toml`.
- **Health check failing**: Render polls `/_stcore/health`; check
  `scanner-web` logs for a config or migration error.

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
docker build --tag streamlit-scanner-app:ci .
docker compose config
docker compose up --build --wait --wait-timeout 180
docker compose down --volumes --remove-orphans
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
