"""AUTH-003 — tests for the pure role/capability policy.

These cover ``backend.auth.roles`` end to end without any Streamlit or database:
the resolution precedence, the capability hierarchy, and the never-raises parser.
"""

from __future__ import annotations

import pytest

from backend.auth.roles import (
    CREATE_WATCHLIST,
    EXPORT_RESULTS,
    MANAGE_IPO_DATA,
    MANAGE_ROLES,
    MANAGE_UNIVERSES,
    MIN_ROLE,
    MODIFY_CONFIG,
    REFRESH_DATA,
    RUN_SCAN,
    VIEW_AUDIT_LOG,
    VIEW_HEALTH,
    VIEW_RESULTS,
    Role,
    resolve_role,
    role_has_capability,
)

ADMIN_ONLY = {
    REFRESH_DATA,
    MANAGE_UNIVERSES,
    MODIFY_CONFIG,
    VIEW_HEALTH,
    VIEW_AUDIT_LOG,
    MANAGE_ROLES,
    MANAGE_IPO_DATA,
}
ANALYST_AND_UP = {RUN_SCAN, EXPORT_RESULTS, CREATE_WATCHLIST}
EVERYONE = {VIEW_RESULTS}


# ---------------------------------------------------------------------------
# resolve_role precedence
# ---------------------------------------------------------------------------


def test_auth_disabled_resolves_to_admin_owner():
    """Local dev with the gate off is a full-access owner regardless of the table."""
    role = resolve_role(
        "anyone@example.com",
        in_admin_env=False,
        table_role=Role.VIEWER,
        auth_required=False,
    )
    assert role is Role.ADMIN


def test_admin_env_is_a_floor_over_a_lower_table_role():
    """An ADMIN_EMAILS member cannot be demoted by a table write (anti-lockout)."""
    role = resolve_role(
        "boss@example.com",
        in_admin_env=True,
        table_role=Role.VIEWER,
        auth_required=True,
    )
    assert role is Role.ADMIN


def test_table_role_is_honoured_when_not_a_bootstrap_admin():
    """The database is the source of truth for everyone who is not an env admin."""
    assert (
        resolve_role(
            "viewer@example.com",
            in_admin_env=False,
            table_role=Role.VIEWER,
            auth_required=True,
        )
        is Role.VIEWER
    )
    assert (
        resolve_role(
            "promoted@example.com",
            in_admin_env=False,
            table_role=Role.ADMIN,
            auth_required=True,
        )
        is Role.ADMIN
    )


def test_no_table_row_falls_back_to_analyst_default():
    """An authorized user with no assignment keeps today's scan/export access."""
    role = resolve_role(
        "newcomer@example.com",
        in_admin_env=False,
        table_role=None,
        auth_required=True,
    )
    assert role is Role.ANALYST


# ---------------------------------------------------------------------------
# capability hierarchy
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", sorted(EVERYONE | ANALYST_AND_UP | ADMIN_ONLY))
def test_admin_holds_every_capability(capability: str):
    assert role_has_capability(Role.ADMIN, capability) is True


@pytest.mark.parametrize("capability", sorted(EVERYONE | ANALYST_AND_UP))
def test_analyst_holds_viewer_and_analyst_capabilities(capability: str):
    assert role_has_capability(Role.ANALYST, capability) is True


@pytest.mark.parametrize("capability", sorted(ADMIN_ONLY))
def test_analyst_lacks_admin_capabilities(capability: str):
    assert role_has_capability(Role.ANALYST, capability) is False


@pytest.mark.parametrize("capability", sorted(ANALYST_AND_UP | ADMIN_ONLY))
def test_viewer_lacks_analyst_and_admin_capabilities(capability: str):
    assert role_has_capability(Role.VIEWER, capability) is False


def test_viewer_holds_only_view_results():
    assert role_has_capability(Role.VIEWER, VIEW_RESULTS) is True


def test_unknown_capability_is_denied_to_everyone():
    for role in Role:
        assert role_has_capability(role, "definitely_not_a_capability") is False


def test_capability_map_covers_the_three_tiers():
    """Guard against a capability silently losing its minimum-role entry."""
    assert set(MIN_ROLE) == EVERYONE | ANALYST_AND_UP | ADMIN_ONLY


# ---------------------------------------------------------------------------
# Role.parse — never raises
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("viewer", Role.VIEWER),
        ("ANALYST", Role.ANALYST),
        ("  Admin  ", Role.ADMIN),
        (Role.ADMIN, Role.ADMIN),
    ],
)
def test_parse_accepts_known_names_case_insensitively(value, expected):
    assert Role.parse(value) is expected


@pytest.mark.parametrize("value", [None, "", "superuser", "owner", 42, "  "])
def test_parse_returns_none_for_unknown_or_missing(value):
    assert Role.parse(value) is None
