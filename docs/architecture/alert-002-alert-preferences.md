# ADR: Alert preferences on the OBS-003 runtime-config rail (ALERT-002)

**Status:** Accepted
**Date:** 2026-06-25
**Deciders:** Codex (ticket owner), Claude (reviewer / implementer)

## Context

ALERT-001 ([notifications LLD](components/notifications.md)) sends **one** daily-scan
summary to **one** configured destination (a Telegram chat and/or an email recipient)
after the headless job runs. It has no explicit on/off switch (it fires whenever a
channel's credentials are present), always includes the top-10 results, and its
destination is environment-only — not changeable without a redeploy.

ALERT-002 asks for operator preferences: (1) enable/disable, (2) summary-only vs full
result content, (3) an admin-configurable destination. The forces: the delivery model is
**a single shared alert to a single destination**, so there is no coherent notion of
per-user delivery; and the repo already has an OBS-003 runtime-config rail (`app_config`
overrides + the admin settings page + `apply_config_overrides`) that validates, persists,
applies-live, and audits changes to whitelisted **non-secret** keys.

## Decision

Model all three preferences as **app-level settings owned by an admin**, carried on the
existing OBS-003 runtime-config rail — no new store, no new tables.

- Add `alerts_enabled` (default **on**, preserving ALERT-001 behaviour) and `alert_content`
  (`summary`/`full`, default **full**) to `NotificationSettings`; the service skips a
  disabled alert and the report/renderer omit the per-stock list for `summary`.
- Make `ALERT_ENABLED`, `ALERT_CONTENT`, and the two **non-secret destinations**
  (`TELEGRAM_CHAT_ID`, `ALERT_EMAIL_TO`) admin-editable via `EDITABLE_CONFIG_KEYS`.
  Enable/content are select boxes; destinations are validated **free-text** inputs — for
  which `EditableSetting` gains an optional empty `choices` (→ text input).
- Channel **credentials** (`TELEGRAM_BOT_TOKEN`, `SMTP_PASSWORD`, `SMTP_HOST/USER`) stay
  environment-only, so the plaintext `app_config` table never stores a secret.
- The headless daily job now calls `apply_config_overrides()` after schema bootstrap, so
  an admin's change actually reaches the cron alert (the Streamlit app already did this).

## Options considered

### Option A — App-level prefs on the OBS-003 rail (chosen)
| Dimension | Assessment |
|-----------|------------|
| Complexity | Low — reuses validate/persist/apply/audit + the admin form |
| Cost | No new tables/migrations; stdlib-only |
| Fit | Matches the single-alert/single-destination model |

**Pros:** Minimal, consistent, audited; secrets stay out of the DB.
**Cons:** Preferences are global, not per-user (acceptable — see Option B).

### Option B — Per-user preferences
| Dimension | Assessment |
|-----------|------------|
| Complexity | High — new per-user store + per-user delivery |
| Fit | Poor — there is one destination, so per-user content can't be honoured |

**Rejected:** only coherent if alerts also became per-user delivery, a much larger change
outside this ticket's intent.

### Option C — A bespoke notification-settings store/page
**Rejected:** duplicates the OBS-003 validate/persist/apply/audit machinery for no gain.

## Consequences

- **Easier:** an admin toggles alerts, picks summary/full, and repoints the destination
  from the UI, audited via `config_changed`, with no redeploy.
- **Harder:** a future *secret* destination would need a different mechanism (by design,
  secrets are not runtime-editable).
- **Revisit when:** multiple destinations or per-recipient delivery is needed (would
  reopen Option B), or a new channel adds its own non-secret destination key.

## Action items
1. [x] `alerts_enabled` / `alert_content` in `NotificationSettings` + dispatch/report/render.
2. [x] Free-text `EditableSetting` + the four `EDITABLE_CONFIG_KEYS` entries.
3. [x] Daily job applies config overrides.
4. [x] Tests + docs ([notifications LLD](components/notifications.md), operations, env examples).
