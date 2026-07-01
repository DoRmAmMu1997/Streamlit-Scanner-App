"""AUTH-003 — roles, capabilities, and the pure role-resolution decision.

Beginner note:
A *role* is a named bundle of permissions. This module is the whole AUTH-003
policy, and it is deliberately a *leaf*: no Streamlit, no database, no environment
reads. That keeps every decision a plain function call, so the gate
(``backend.auth.session``) and the app can stay thin and every branch here is a
one-line unit test.

The model has two pieces:

- a **hierarchy** ``viewer < analyst < admin`` (an admin can do everything an
  analyst can, who can do everything a viewer can), and
- a **capability map**: each action names the *minimum* role allowed to perform
  it. Code asks ``role_has_capability(role, CAP)`` — never ``role == "admin"`` —
  so the policy lives in one table instead of scattered string comparisons.

``resolve_role`` turns "who is this signed-in email?" into one effective role,
combining the database assignment (``user_roles``) with the ``ADMIN_EMAILS``
bootstrap floor. The table lookup is injected (a ``Role | None``) rather than read
here, so this function never touches the database and tests need no fixtures.
"""

from __future__ import annotations

from enum import IntEnum


class Role(IntEnum):
    """The three access tiers, ordered so a higher value is strictly more able.

    ``IntEnum`` (not a plain ``Enum``) is the point: ``Role.ADMIN >= Role.ANALYST``
    is the hierarchy, which is exactly what ``role_has_capability`` compares. The
    stored database value is the lower-case ``.name`` (``"viewer"``), matched by
    the ``user_roles.role`` CHECK constraint and parsed back by ``Role.parse``.
    """

    VIEWER = 0
    ANALYST = 1
    ADMIN = 2

    @classmethod
    def parse(cls, value: object) -> Role | None:
        """Map a stored role name to a ``Role``; return ``None`` for unknown/missing.

        This pure parser never raises. The database lookup boundary in
        ``backend.auth.session`` distinguishes an absent row from an invalid stored
        value before it calls ``resolve_role``; invalid values therefore take the
        fail-closed viewer/deny path rather than the ordinary analyst default.
        """
        if isinstance(value, Role):
            return value
        try:
            return cls[str(value).strip().upper()]
        except (KeyError, AttributeError):
            return None


# ---------------------------------------------------------------------------
# Capabilities — the unit of enforcement
# ---------------------------------------------------------------------------
# One constant per gated action. Referencing a constant at the call site (instead
# of a bare string) turns a typo into an ImportError rather than a silent
# always-denied check.
VIEW_RESULTS = "view_results"           # history / comparison / validation / charts
RUN_SCAN = "run_scan"                   # the sidebar "Run screener" button
EXPORT_RESULTS = "export_results"       # the "Download results CSV" button
CREATE_WATCHLIST = "create_watchlist"   # reserved — the watchlist feature is not built yet
REFRESH_DATA = "refresh_data"           # any in-app data-refresh control
MANAGE_UNIVERSES = "manage_universes"   # mutating universe actions
MODIFY_CONFIG = "modify_config"         # the Admin settings page
VIEW_HEALTH = "view_health"             # the Admin health page
VIEW_AUDIT_LOG = "view_audit_log"       # the Audit log page
MANAGE_ROLES = "manage_roles"           # the Admin roles page (assign/revoke)
MANAGE_IPO_DATA = "manage_ipo_data"     # immutable manual IPO evidence submissions

# Each capability's minimum role. The hierarchy does the rest: a role holds a
# capability when it ranks at or above the minimum.
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
    MANAGE_IPO_DATA: Role.ADMIN,
}

# The role an authorized user gets when the database has no row for them. Analyst
# preserves AUTH-002 behaviour (every allow-listed user could already scan/export),
# so AUTH-003 is a non-breaking change. Kept a constant on purpose — promote it to
# a setting only if an operator ever needs viewer-by-default.
DEFAULT_ROLE = Role.ANALYST


def role_has_capability(role: Role, capability: str) -> bool:
    """Return ``True`` when ``role`` meets the minimum role for ``capability``.

    An unknown capability name has no entry in ``MIN_ROLE`` and is therefore
    denied to everyone (fail closed), which surfaces a typo as an always-false
    check instead of an accidental allow.
    """
    minimum = MIN_ROLE.get(capability)
    return minimum is not None and role >= minimum


def resolve_role(
    email: str,
    *,
    in_admin_env: bool,
    table_role: Role | None,
    default_role: Role = DEFAULT_ROLE,
    auth_required: bool,
) -> Role:
    """Resolve one signed-in email to its effective role.

    Precedence, highest first (AUTH-003 design §5.2):

    1. ``auth_required is False`` → ``ADMIN``. Local development runs with the auth
       gate disabled; treat that single-operator machine as a full-access owner so
       admin pages are reachable. Production validation forbids disabling auth.
    2. ``in_admin_env`` (the email is in ``ADMIN_EMAILS``) → ``ADMIN``. The env
       admin list is a *floor*: a bootstrap admin cannot be demoted by a table
       write, which guarantees the system is never left without an admin.
    3. ``table_role`` is the database assignment when one exists — the runtime
       source of truth for everyone who is not a bootstrap admin.
    4. Otherwise ``default_role`` (analyst), preserving current access.

    ``email`` is unused by the logic today but kept in the signature so the
    decision reads as "resolve THIS email" and so a future per-email rule has an
    obvious home.
    """
    if not auth_required:
        return Role.ADMIN
    if in_admin_env:
        return Role.ADMIN
    if table_role is not None:
        return table_role
    return default_role
