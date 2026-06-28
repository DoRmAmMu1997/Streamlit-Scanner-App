# AUTH-003 — Basic role model · Handoff brief (build plan)

| | |
|---|---|
| **Ticket** | AUTH-003 — Add basic role model (viewer / analyst / admin) |
| **Type / Priority** | Story · P1 |
| **Status** | Implemented and review-hardened on `claude/auth-003-role-model` |
| **Owner / Reviewer** | Claude (design **and** implementation this cycle) / Codex review |
| **Depends on** | AUTH-001/002 ([`backend/auth/session.py`](../../backend/auth/session.py)) · OBS-001/003 · SCAN-002 (storage + migrations) |
| **Unblocks** | per-feature gating · watchlists (WATCH-*) · self-service access management |

> Goal (ticket): *Separate viewer, analyst, and admin capabilities.*
> Acceptance: db-driven role config · admin-only features gated · unauthorized attempts logged · tests
> cover role permission checks.

**Read first:** [`auth-003-role-model.md`](auth-003-role-model.md) is the **contract** (role model,
capability map, `user_roles` schema, resolution precedence, security rules). This brief is the *build
plan*. Where they disagree, the design wins — flag it in §7.

---

## 0. What already exists (your starting point)

The whole gate is built; AUTH-003 layers role resolution + capability checks on top.

- **The gate** — `require_authorized_user(st_module)` and the pure
  `is_email_authorized(email, *, allowed, admins, production)` in
  [`backend/auth/session.py`](../../backend/auth/session.py). One call at the top of `main()` protects
  everything below it.
- **Identity** — `AuthenticatedUser(email, name, is_admin)` (frozen dataclass, same file). `is_admin` is
  already set from `ADMIN_EMAILS` and read in `app.py` — this is the hook the LLD reserved for AUTH-003.
- **Config** — `AppSettings` in [`backend/config/settings.py`](../../backend/config/settings.py):
  `allowed_emails` / `admin_emails` are normalized `frozenset[str]`; `_parse_email_set`, `safe_dict`,
  and `validate_production_settings` are the patterns to extend.
- **Logging** — `log_event(...)` + `EVENT_*` constants in
  [`backend/observability/__init__.py`](../../backend/observability/__init__.py) (see `EVENT_AUTH_DENIED`).
- **Audit** — `record_audit_event` / `record_audit_event_once` in
  [`backend/audit/recorder.py`](../../backend/audit/recorder.py); the denial pattern is right there in
  `require_authorized_user` (`login_denied`).
- **Storage** — ORM models + repository + engine/session in
  [`backend/storage/`](../../backend/storage/); hand-written Alembic migrations under
  [`migrations/versions/`](../../migrations/versions/) (e.g. `20260617obs003_create_audit_logs.py`).
  `ensure_database_schema()` builds the schema.
- **Enforcement site** — [`app.py`](../../app.py) `main()` builds the view list and appends the admin
  pages behind `is_admin`; `_record_admin_page_access` audits admin-page opens.

**Boundary:** AUTH-003 delivers the role model + `user_roles` table + resolution + capability
enforcement + the admin Roles page + tests + the LLD update. It does **not** build watchlists (reserve
the capability only) or per-object ACLs.

---

## 1. File plan

| File | Action |
|---|---|
| `backend/auth/roles.py` | **New** — `Role` enum, capability constants, `MIN_ROLE` map, pure `role_has_capability`, `resolve_role`. No Streamlit/DB. |
| `backend/storage/models.py` | **Edit** — add the `UserRole` ORM model (design §4). |
| `migrations/versions/<date>auth003_create_user_roles.py` | **New, same commit** — hand-written create-table for `user_roles` (+ downgrade). |
| `tests/test_scan_storage_migrations.py` | **Edit** — add `"user_roles"` to the **two** hard-coded table-name sets (drift guard, §5). |
| `backend/storage/__init__.py` (+ repository module) | **Edit** — `get_user_role(email)`, `set_user_role(email, role, assigned_by)`, `list_user_roles()`, `count_admins()`. |
| `backend/auth/session.py` | **Edit** — add `role: Role` to `AuthenticatedUser` (`is_admin` derived); call `resolve_role` in `require_authorized_user`; widen authorization with table membership; add `require_capability`. |
| `backend/config/settings.py` | **Edit (small)** — `DEFAULT_ROLE` constant (`analyst`); add to `safe_dict`. |
| `backend/observability/__init__.py` | **Edit** — `EVENT_ROLE_DENIED` (+ audit names `role_denied`, `role_changed`). |
| `app.py` | **Edit** — build the view list by capability; gate **Run** + **Export** (UI hide **and** handler `require_capability`); switch admin-page gating to capabilities; append the new **Admin roles** view. |
| `ui/roles_page.py` | **New** — admin-only list + assign UI; calls the repository; records `role_changed`; guards last-admin/self-demotion. |
| `tests/test_auth_roles.py` | **New** — pure resolution + capability matrices, `require_capability` denial (log+audit). |
| `tests/test_auth_session.py` · `tests/test_settings.py` · `tests/test_*` (repo/UI) | **Edit/New** — role wiring, `DEFAULT_ROLE`, repository round-trip, Roles-page guards. |
| `docs/architecture/components/authentication.md` | **Edit** — interface (§3), decisions (§4), config (§6), extension points (§8); link the new role model. HLD only if system-wide. |

---

## 2. Code skeletons

### 2.1 `backend/auth/roles.py` — the pure policy (easiest to test)
```python
"""AUTH-003 — roles, capabilities, and the pure role-resolution decision.

No Streamlit, no DB, no env reads. The hierarchy (viewer < analyst < admin) plus one
capability→minimum-role map is the entire policy, so every check is a plain comparison and
every branch of resolve_role is a one-line unit test (design §3, §5.2).
"""
from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    VIEWER = 0
    ANALYST = 1
    ADMIN = 2

    @classmethod
    def parse(cls, value: str | None) -> "Role | None":
        """Map a stored name to a Role, or None for unknown/missing (never raise)."""
        try:
            return cls[str(value).strip().upper()]
        except (KeyError, AttributeError):
            return None


# Capability → minimum role. Capabilities are the unit of enforcement (design §3.2).
VIEW_RESULTS = "view_results"
RUN_SCAN = "run_scan"
EXPORT_RESULTS = "export_results"
CREATE_WATCHLIST = "create_watchlist"      # reserved; feature not built
REFRESH_DATA = "refresh_data"
MANAGE_UNIVERSES = "manage_universes"
MODIFY_CONFIG = "modify_config"
VIEW_HEALTH = "view_health"
VIEW_AUDIT_LOG = "view_audit_log"
MANAGE_ROLES = "manage_roles"

MIN_ROLE: dict[str, Role] = {
    VIEW_RESULTS: Role.VIEWER,
    RUN_SCAN: Role.ANALYST,
    EXPORT_RESULTS: Role.ANALYST,
    CREATE_WATCHLIST: Role.ANALYST,
    REFRESH_DATA: Role.ADMIN,
    MANAGE_UNIVERSES: Role.ADMIN,
    MODIFY_CONFIG: Role.ADMIN,
    VIEW_HEALTH: Role.ADMIN,
    VIEW_AUDIT_LOG: Role.ADMIN,
    MANAGE_ROLES: Role.ADMIN,
}


def role_has_capability(role: Role, capability: str) -> bool:
    """True when `role` meets the minimum role for `capability` (hierarchy = ⊇)."""
    minimum = MIN_ROLE.get(capability)
    return minimum is not None and role >= minimum


def resolve_role(
    email: str,
    *,
    in_admin_env: bool,
    table_role: Role | None,
    default_role: Role,
    auth_required: bool,
) -> Role:
    """Resolve the effective role. Precedence (design §5.2):
    auth-disabled owner → ADMIN_EMAILS floor → table role → default (analyst).
    """
    if not auth_required:
        return Role.ADMIN          # local dev / auth off → full-access owner
    if in_admin_env:
        return Role.ADMIN          # env bootstrap floor — anti-lockout, can't be demoted via table
    if table_role is not None:
        return table_role
    return default_role            # no row → analyst (preserve current access)
```

### 2.2 `backend/storage/models.py` — the `UserRole` model
```python
class UserRole(Base):
    """AUTH-003 durable role assignment (design §4). One row per user; email is the key."""
    __tablename__ = "user_roles"

    email: Mapped[str] = mapped_column(String, primary_key=True)
    role: Mapped[str] = mapped_column(String, nullable=False)   # 'viewer'|'analyst'|'admin'
    assigned_by: Mapped[str | None] = mapped_column(String, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    __table_args__ = (
        CheckConstraint("role in ('viewer','analyst','admin')", name="ck_user_roles_role"),
    )
```
> Match the column types/timestamp idiom of the **neighbouring** models (`AuditLog`, `AppConfig`) in
> that file — copy their `Mapped`/`mapped_column`/`func.now()` style verbatim rather than the sketch
> above if they differ.

### 2.3 Migration (hand-written, same commit)
```python
# migrations/versions/<date>auth003_create_user_roles.py
revision = "<date>auth003"
down_revision = "<current head>"   # resolve against `alembic heads` at implementation time

def upgrade() -> None:
    op.create_table(
        "user_roles",
        sa.Column("email", sa.String(), primary_key=True),
        sa.Column("role", sa.String(), nullable=False),
        sa.Column("assigned_by", sa.String(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.CheckConstraint("role in ('viewer','analyst','admin')", name="ck_user_roles_role"),
    )

def downgrade() -> None:
    op.drop_table("user_roles")
```
The drift guard (`test_migration_matches_orm_metadata`) asserts `alembic upgrade head` == `Base.metadata`
**exactly** (columns/constraints/indexes) — keep the model and migration byte-aligned, then run the
migration tests locally before anything else.

### 2.4 `AuthenticatedUser` + `require_capability` (in `session.py`)
```python
@dataclass(frozen=True)
class AuthenticatedUser:
    email: str
    name: str | None = None
    role: Role = Role.VIEWER
    @property
    def is_admin(self) -> bool:        # back-compat: every existing reader keeps working
        return self.role is Role.ADMIN
```
- In `require_authorized_user`: after the authorization check, look up the table role (repository),
  compute `in_admin_env = email in admins`, and `role = resolve_role(...)`; return
  `replace(user, email=email, role=role)`. Widen the authorization test so a `user_roles` row also
  authorizes entry (design §5.1) — keep the pure decision injectable for tests.
- `require_capability(st_module, user, capability)`: on miss → `log_event(EVENT_ROLE_DENIED, WARNING,
  email, required_capability, role)` + `record_audit_event_once(dedup_key=f"_audit_role_denied:{email}:{capability}", event="role_denied", ...)` → `st.error(generic)` → `_stop`.

### 2.5 `app.py` enforcement (defense-in-depth)
```python
# view list by capability (replaces the is_admin extend)
if role_has_capability(user.role, VIEW_HEALTH):   # admin tier
    view_options.extend(["Admin health", "Admin settings", "Audit log", "Admin roles"])
# Run button (sidebar) and Download CSV: render only if capable …
if role_has_capability(user.role, RUN_SCAN): ...   # show Run
# … AND re-check in the handler before acting:
def _execute_screener(...):
    require_capability(st, authenticated_user, RUN_SCAN)
    ...
```

---

## 3. Tests (acceptance lives here)

`tests/test_auth_roles.py` — pure, no DB/Streamlit:
- **Resolution precedence matrix** — auth-off→ADMIN; in_admin_env→ADMIN even when `table_role=VIEWER`
  (floor); table_role honoured; no row→`default_role` (analyst). ✅ *db-driven + preserve-access*
- **Capability matrix** — for each role × capability, assert `role_has_capability` matches the design
  §3.2 table (admin all-true; viewer only `VIEW_RESULTS`). ✅ *admin gating*
- **`Role.parse`** — known names map; unknown/None → `None` (never raises). ✅ *bad-write safety*
- **`require_capability` denial** — fake `st`; assert it stops, emits `EVENT_ROLE_DENIED` (`caplog`),
  and writes one `role_denied` row (`file_session_factory`); dedup → one row on repeat. ✅ *attempts logged*

`tests/` (DB) — reuse `file_session_factory`/`db_session` from [`tests/conftest.py`](../../tests/conftest.py):
- **Repository round-trip** — `set_user_role` then `get_user_role`; reassignment bumps `updated_at`;
  CHECK rejects a bad role string. ✅ *db-driven*
- **Migration drift green** — `test_scan_storage_migrations.py` passes with `user_roles` added to both
  table-name sets. ✅ *schema integrity*

`tests/test_auth_session.py` (edit) — `require_authorized_user` returns the resolved role; a
`user_roles` row authorizes entry; `is_admin` stays correct.

`ui`/`app` — **admin-only Roles page**: a non-admin is denied (`require_capability`); an assignment
records `role_changed`; the page refuses last-admin removal / self-demotion. ✅ *no self-escalation*

Coverage stays **≥ 84%** (CI gate) — the pure `roles.py` carries the policy and is cheap to cover.

---

## 4. Decisions to preserve (don't drift from the design)

- **Hierarchy** `admin ⊇ analyst ⊇ viewer`; enforce **capabilities**, never `role == "admin"` (§3).
- **DB store is the runtime source of truth; `ADMIN_EMAILS` is a bootstrap floor** that cannot be
  demoted via the table (§5.2) — the anti-lockout guarantee.
- **Default = analyst; auth-off = owner** — AUTH-003 is non-breaking; `viewer` is opt-in (§5.2).
- **Defense-in-depth** — UI hides *and* the handler re-checks (§5.4).
- **Denials log + audit** via the existing OBS-001/003 idiom, deduped per session (§6); never leak the
  lists/table.
- **`is_admin` stays** as a derived property (back-compat).
- **Migration ships with the model** in one commit; update the two drift-test table sets (§5 gotcha).

## 5. Gotchas

1. **Migration-drift guard is unforgiving.** A model-only change is red CI. Add the model **and** the
   migration in the same commit; `alembic upgrade head` must rebuild `Base.metadata` exactly. Then add
   `"user_roles"` to **both** hard-coded sets (`test_alembic_upgrade_and_downgrade_use_temp_sqlite`,
   `test_ensure_database_schema_creates_tables_and_short_circuits`).
2. **Local `data/scanner.db` can be stale** (stamped at head, missing the new table). If you hit
   `no such table: user_roles`, rebuild per the project workflow (delete `scanner.db`+`-wal`/`-shm` with
   the app stopped, then `ensure_database_schema()`).
3. **`is_admin` is now derived** — don't also store it; set `role` and let the property answer.
4. **Auth-off dev path** — `app.py` creates the synthetic `local-owner@localhost` admin. Production
   still forbids disabling auth; local admin pages and audit attribution remain usable.
5. **Don't leak the table.** `role_denied`/logs carry the actor + attempted capability only.
6. **UI hides, handler enforces.** Never rely on a hidden button alone. For **CSV export** there is no
   post-click handler — `st.download_button` builds its payload at render time, so guard `EXPORT_RESULTS`
   **before constructing the export bytes and rendering the button** (design §8.10), not after.
7. **Re-resolve the role every run; never cache it in `st.session_state`.** `require_authorized_user`
   runs each rerun → look the table up each run so a demotion/revocation is honored on the target's next
   interaction (design §8.8). Caching the resolved `Role` opens a stale-authorization window.
8. **Role lookup state is explicit.** `missing` uses the analyst default; `unavailable`/`invalid`
   restrict independently authorized users to viewer and deny table-only entry (design §8.9).
9. **Lint/type/scan scope** — `ruff`/`mypy`/`bandit` cover `backend`/`app.py`/`ui`; `ruff` also `tests`.
   Keep `backend/auth/roles.py` clean; no `# type: ignore` without a reason. Migrations are out of lint
   scope.
10. **Supply chain** — add no new runtime dep (none needed). `git diff origin/main HEAD -- constraints.txt
   pyproject.toml` must be empty.

## 6. Verification (run before requesting review)
```powershell
python -m pytest -q --cov=backend --cov=screeners --cov=ui --cov-fail-under=84
python -m compileall -q app.py backend screeners ui tests
python -m ruff check app.py backend screeners ui Dependencies tests
python -m mypy
python -m bandit -r app.py backend screeners ui Dependencies -q
python -m pip_audit -r constraints.txt
alembic upgrade head   # schema change → exercise the migration round-trip
```
(Gates are identical to CI. The `alembic` step matters this time — AUTH-003 changes the schema.)

## 7. Resolved implementation decisions
- **Roles page placement** — standalone **Admin roles** view.
- **Default role** — constant `analyst`; no environment knob.
- **Admin protection** — refuse self-demotion/self-revocation and lock admin rows before the
  last-admin check plus mutation.
- **Authorization widening** — a valid `user_roles` row authorizes entry; failed/invalid lookups do not.
- **Failure policy** — env admins remain admin, independently authorized users become viewer, and
  table-only users are denied while the role store is unavailable.
- **Viewer charts** — persisted History results reconstruct cache-only charts; no live data fetch.
- **Exports** — `EXPORT_RESULTS` gates live, History, Comparison, and Validation payload construction.
