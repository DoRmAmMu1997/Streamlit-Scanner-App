"""Tests for the Streamlit OIDC authentication gate.

These tests avoid opening a browser or contacting Google. Instead they pass a
small fake ``st`` object into the auth helper and assert which Streamlit methods
would have been called.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from types import SimpleNamespace

import pytest

from backend.auth import session
from backend.auth.roles import RUN_SCAN, VIEW_RESULTS, Role
from backend.auth.session import (
    AuthenticatedUser,
    is_email_authorized,
    require_authenticated_user,
    require_authorized_user,
    require_capability,
)
from backend.observability import EVENT_AUTH_DENIED, EVENT_ROLE_DENIED


class _StopCalled(RuntimeError):
    """Raised by the fake Streamlit object when st.stop() is invoked.

    Real ``st.stop()`` interrupts the current Streamlit script run. Raising a
    test-only exception gives us the same "nothing below this line should run"
    signal in ordinary pytest code.
    """


class _FakeSidebar:
    """Minimal context manager for code that writes into ``with st.sidebar``."""

    def __init__(self, owner: _FakeStreamlit):
        self.owner = owner

    def __enter__(self):
        self.owner.sidebar_opened += 1
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeStreamlit:
    """Small fake for the st.login/st.user/st.logout auth surface.

    Beginner note:
    Streamlit widgets return ``True`` only on the rerun caused by a click. The
    ``clicked_labels`` set lets each test choose which fake button is "clicked"
    without needing a real UI.
    """

    def __init__(
        self,
        *,
        user: object | None = None,
        secrets: dict | None = None,
        clicked_labels: set[str] | None = None,
    ):
        self.user = user or SimpleNamespace(is_logged_in=False)
        self.secrets = _google_auth_secrets() if secrets is None else secrets
        self.clicked_labels = clicked_labels or set()
        self.buttons: list[dict[str, object]] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.captions: list[str] = []
        self.login_calls: list[str | None] = []
        self.logout_calls = 0
        self.sidebar_opened = 0
        self.sidebar = _FakeSidebar(self)

    def button(self, label, *_, on_click=None, args=None, **kwargs):
        """Record button configuration and trigger callbacks for clicked labels."""
        self.buttons.append({"label": label, "kwargs": kwargs})
        if label in self.clicked_labels and on_click is not None:
            on_click(*(args or ()))
        return label in self.clicked_labels

    def login(self, provider=None):
        self.login_calls.append(provider)

    def logout(self):
        self.logout_calls += 1

    def stop(self):
        raise _StopCalled()

    def error(self, message):
        self.errors.append(str(message))

    def warning(self, message):
        self.warnings.append(str(message))

    def caption(self, message):
        self.captions.append(str(message))


def _google_auth_secrets() -> dict:
    """Return the minimum secrets shape expected from .streamlit/secrets.toml."""
    return {
        "auth": {
            "redirect_uri": "http://localhost:8501/oauth2callback",
            "cookie_secret": "cookie-secret",
            "google": {
                "client_id": "google-client",
                "client_secret": "google-secret",
                "server_metadata_url": (
                    "https://accounts.google.com/.well-known/openid-configuration"
                ),
            },
        }
    }


@pytest.fixture(autouse=True)
def _authlib_installed(monkeypatch):
    """Default every test to "Authlib is installed" so the login path is exercised.

    The real runtime dependency is declared in requirements.txt/constraints.txt.
    Forcing it here keeps these fake-Streamlit tests deterministic regardless of
    whether Authlib happens to be installed in the local environment. The missing
    dependency case overrides this fixture explicitly.
    """
    monkeypatch.setattr(session, "_is_authlib_available", lambda: True)


@pytest.fixture(autouse=True)
def _no_role_table(monkeypatch):
    """Default every gate test to "no user_roles assignment", DB-free.

    AUTH-003 made ``require_authorized_user`` consult the ``user_roles`` table for
    both authorization and role. These tests exercise the AUTH-002 env-allowlist
    behaviour, so stub the lookup to ``None`` (no row); the dedicated table-path
    tests below override it explicitly. This keeps the env-allowlist tests from
    touching the database.
    """
    monkeypatch.setattr(session, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(session, "session_scope", lambda: nullcontext(object()))
    monkeypatch.setattr(session, "get_user_role", lambda *_args: None)


def test_unauthenticated_user_sees_google_login_and_cannot_continue():
    """A signed-out user should see only the login button, then the run stops."""
    fake_st = _FakeStreamlit()

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    assert fake_st.buttons == [{"label": "Log in with Google", "kwargs": {"type": "primary"}}]
    assert fake_st.login_calls == []
    assert fake_st.logout_calls == 0


def test_login_button_uses_named_google_provider():
    """Clicking the login button should start Streamlit's configured Google flow."""
    fake_st = _FakeStreamlit(clicked_labels={"Log in with Google"})

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    assert fake_st.login_calls == ["google"]


def test_authenticated_user_email_is_returned_and_logout_button_is_wired():
    """A signed-in user should expose email state and a working logout control."""
    fake_st = _FakeStreamlit(
        user=SimpleNamespace(is_logged_in=True, email="sunny@example.com", name="Sunny"),
        clicked_labels={"Log out"},
    )

    user = require_authenticated_user(fake_st)

    assert user == AuthenticatedUser(email="sunny@example.com", name="Sunny")
    assert fake_st.captions == ["Signed in as sunny@example.com"]
    assert fake_st.logout_calls == 1
    assert fake_st.sidebar_opened == 1


def test_missing_production_auth_config_fails_closed(monkeypatch):
    """Production must stop before login if Google SSO settings are missing."""
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _FakeStreamlit(secrets={})

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    assert fake_st.errors
    assert "Google SSO is not configured" in fake_st.errors[0]
    assert fake_st.buttons == []
    assert fake_st.login_calls == []


def test_logged_in_user_without_email_cannot_continue():
    """Future allowlist/role checks need an email, so missing email is rejected."""
    fake_st = _FakeStreamlit(user=SimpleNamespace(is_logged_in=True, name="No Email"))

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    assert fake_st.errors == ["Your login did not provide an email address."]


def test_missing_authlib_dependency_blocks_login(monkeypatch):
    """Without Authlib, st.login would crash, so the gate must not offer it."""
    monkeypatch.setattr(session, "_is_authlib_available", lambda: False)
    fake_st = _FakeStreamlit()

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    # No login button is rendered, so the user never hits the StreamlitAuthError
    # Streamlit raises when Authlib is missing; they get a setup message instead.
    assert fake_st.buttons == []
    assert fake_st.login_calls == []
    assert any("Authlib" in message for message in fake_st.errors)


def test_authenticated_user_email_is_normalized_to_lowercase():
    """A mixed-case email must collapse to one stable lowercase identity key."""
    fake_st = _FakeStreamlit(
        user=SimpleNamespace(is_logged_in=True, email="Sunny@Example.COM", name="Sunny"),
        clicked_labels={"Log out"},
    )

    user = require_authenticated_user(fake_st)

    assert user == AuthenticatedUser(email="sunny@example.com", name="Sunny")
    assert fake_st.captions == ["Signed in as sunny@example.com"]


def test_logged_in_user_with_unverified_email_cannot_continue():
    """An email the provider marks unverified must not be accepted as identity."""
    fake_st = _FakeStreamlit(
        user=SimpleNamespace(
            is_logged_in=True, email="spoofed@example.com", email_verified=False
        )
    )

    with pytest.raises(_StopCalled):
        require_authenticated_user(fake_st)

    assert fake_st.errors == ["Your Google account email is not verified."]


# ---------------------------------------------------------------------------
# AUTH-002: email allowlist + admin identification
# ---------------------------------------------------------------------------


def _signed_in(email: str, name: str = "User") -> _FakeStreamlit:
    """A fake Streamlit whose user is signed in with a verified Google email."""
    return _FakeStreamlit(user=SimpleNamespace(is_logged_in=True, email=email, name=name))


def test_is_email_authorized_admin_is_always_allowed():
    """An admin gets in even when the allowlist is empty and prod is locked down."""
    assert is_email_authorized(
        "boss@example.com",
        allowed=frozenset(),
        admins=frozenset({"boss@example.com"}),
        production=True,
    )


def test_is_email_authorized_checks_membership_when_allowlist_set():
    """With a populated allowlist, only listed emails pass."""
    allowed = frozenset({"a@example.com"})
    assert is_email_authorized("a@example.com", allowed=allowed, admins=frozenset(), production=False)
    assert not is_email_authorized("b@example.com", allowed=allowed, admins=frozenset(), production=False)


def test_is_email_authorized_empty_allowlist_dev_permits_prod_denies():
    """Empty allowlist: development permits everyone; production fails closed."""
    assert is_email_authorized("anyone@example.com", allowed=frozenset(), admins=frozenset(), production=False)
    assert not is_email_authorized("anyone@example.com", allowed=frozenset(), admins=frozenset(), production=True)


def test_is_email_authorized_normalizes_case_and_whitespace():
    """A padded mixed-case email matches a lowercased allowlist entry."""
    allowed = frozenset({"sunny@example.com"})
    assert is_email_authorized("  Sunny@Example.COM ", allowed=allowed, admins=frozenset(), production=True)


def test_require_authorized_user_allows_listed_email(monkeypatch):
    """A signed-in, allow-listed user is returned (non-admin) with no error."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com, friend@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("sunny@example.com", "Sunny")

    user = require_authorized_user(fake_st)

    # No table row + not an admin → the analyst default (preserves AUTH-002 access).
    assert user == AuthenticatedUser(
        email="sunny@example.com", name="Sunny", role=Role.ANALYST
    )
    assert user.is_admin is False
    assert fake_st.errors == []


def test_require_authorized_user_flags_admin(monkeypatch):
    """An ADMIN_EMAILS member is allowed (even if absent from ALLOWED_EMAILS) and flagged."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "Boss@Example.com")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("BOSS@example.com", "Boss")

    user = require_authorized_user(fake_st)

    assert user.email == "boss@example.com"
    assert user.is_admin is True
    assert fake_st.errors == []


def test_require_authorized_user_normalizes_email_at_authorization_boundary(
    monkeypatch,
):
    """Authorization should not depend on the authentication helper's casing.

    ``get_authenticated_user`` already lowercases real Streamlit emails. This
    test protects ``require_authorized_user`` itself by replacing the
    authentication step with a fake that returns a mixed-case email. If that
    upstream helper ever changes, admin tagging should still compare one
    normalized identity key against the normalized ``ADMIN_EMAILS`` set.

    Beginner note:
    ``monkeypatch.setattr`` temporarily swaps the real sign-in helper for the
    small lambda below. That lets this unit test focus on the authorization
    boundary without building a full fake Google login flow.
    """
    # Make the app behave like production with no general allowlist. In that
    # mode, the only way through should be membership in ADMIN_EMAILS.
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("SCANNER_ENV", "production")

    # Simulate an authenticated user object whose email was not lowercased by
    # the previous AUTH-001 step. require_authorized_user should still normalize
    # the value before comparing it with ADMIN_EMAILS.
    monkeypatch.setattr(
        session,
        "require_authenticated_user",
        lambda _st: AuthenticatedUser(email="BOSS@example.com", name="Boss"),
    )
    fake_st = _FakeStreamlit()

    user = require_authorized_user(fake_st)

    # The returned app state should use the same canonical email key that made
    # the authorization decision, and the user should be clearly marked admin.
    assert user.email == "boss@example.com"
    assert user.is_admin is True
    assert fake_st.errors == []


def test_require_authorized_user_denies_unlisted_email(monkeypatch):
    """A signed-in user not on a populated allowlist is stopped with an error."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("intruder@example.com")

    with pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    assert any("not authorized" in message.lower() for message in fake_st.errors)


def test_require_authorized_user_emits_auth_denied_event(monkeypatch, caplog):
    """OBS-001: a denied sign-in emits auth_denied carrying the offending email."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("intruder@example.com")

    with caplog.at_level(logging.WARNING), pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_AUTH_DENIED
    ]
    assert len(events) == 1
    assert events[0]["email"] == "intruder@example.com"
    assert events[0]["reason"] == "not_on_allowlist"


def test_require_authorized_user_bootstraps_schema_before_denied_audit(
    monkeypatch,
):
    """OBS-003 should make the durable denial row possible before stopping."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("intruder@example.com")
    fake_st.session_state = {}
    calls: list[str] = []

    def record_denied_once(**kwargs):
        assert kwargs["session_state"] is fake_st.session_state
        assert kwargs["dedup_key"] == "_audit_login_denied:intruder@example.com"
        assert kwargs["event"] == "login_denied"
        assert kwargs["user_email"] == "intruder@example.com"
        calls.append("audit")
        return True

    monkeypatch.setattr(
        session, "ensure_database_schema", lambda: calls.append("schema") or True, raising=False
    )
    monkeypatch.setattr(session, "record_audit_event_once", record_denied_once, raising=False)
    monkeypatch.setattr(
        session,
        "record_audit_event",
        lambda **_kwargs: calls.append("legacy_audit"),
    )

    with pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    # Role lookup prepares the table first; the denied-audit path repeats the
    # idempotent bootstrap immediately before its own durable write.
    assert calls == ["schema", "schema", "audit"]


def test_require_authorized_user_empty_allowlist_denies_in_production(monkeypatch):
    """Empty allowlist in production fails closed for non-admins."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    fake_st = _signed_in("anyone@example.com")

    with pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    assert any("not authorized" in message.lower() for message in fake_st.errors)


def test_require_authorized_user_empty_allowlist_permits_in_development(monkeypatch):
    """Empty allowlist in development permits any signed-in user."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "development")
    fake_st = _signed_in("anyone@example.com", "Dev User")

    user = require_authorized_user(fake_st)

    assert user.email == "anyone@example.com"
    assert user.is_admin is False


def test_require_authorized_user_requires_email_via_auth_gate(monkeypatch):
    """Authorization can't run without an email: the AUTH-001 gate stops first."""
    monkeypatch.setenv("ALLOWED_EMAILS", "sunny@example.com")
    monkeypatch.setenv("SCANNER_ENV", "development")
    fake_st = _FakeStreamlit(user=SimpleNamespace(is_logged_in=True, name="No Email"))

    with pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    assert fake_st.errors == ["Your login did not provide an email address."]


# ---------------------------------------------------------------------------
# AUTH-003 — table-driven entry + role, and the capability guard
# ---------------------------------------------------------------------------


def test_user_roles_row_authorizes_entry_and_sets_role(monkeypatch):
    """A user_roles row grants sign-in even with an empty env allowlist in prod."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    # Only the database grants this email access; it is on neither env list.
    monkeypatch.setattr(
        session,
        "_lookup_table_role",
        lambda _email: session._RoleLookupResult("found", Role.VIEWER),
    )
    fake_st = _signed_in("tabled@example.com", "Tabled")

    user = require_authorized_user(fake_st)

    assert user.email == "tabled@example.com"
    assert user.role is Role.VIEWER
    assert user.is_admin is False
    assert fake_st.errors == []


def test_admin_env_floor_overrides_a_lower_table_role(monkeypatch):
    """An ADMIN_EMAILS member stays admin even if the table says viewer."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("SCANNER_ENV", "production")
    monkeypatch.setattr(
        session,
        "_lookup_table_role",
        lambda _email: session._RoleLookupResult("found", Role.VIEWER),
    )
    fake_st = _signed_in("boss@example.com", "Boss")

    user = require_authorized_user(fake_st)

    assert user.role is Role.ADMIN
    assert user.is_admin is True


def test_role_lookup_reports_unavailable_when_schema_bootstrap_fails(monkeypatch):
    """A migration/bootstrap failure must not look like an ordinary missing row."""
    monkeypatch.setattr(session, "ensure_database_schema", lambda: False)
    monkeypatch.setattr(session, "get_user_role", lambda *_args: None)

    result = session._lookup_table_role("viewer@example.com")

    assert getattr(result, "state", None) == "unavailable"


def test_role_lookup_reports_missing_for_absent_assignment(monkeypatch):
    """An ordinary missing row remains distinct from a storage failure."""
    monkeypatch.setattr(session, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(session, "get_user_role", lambda *_args: None)
    monkeypatch.setattr(session, "session_scope", lambda: nullcontext(object()))

    result = session._lookup_table_role("unassigned@example.com")

    assert result.state == "missing"
    assert result.role is None
    assert result.authorizes_entry is False


def test_role_lookup_reports_invalid_for_unknown_stored_role(monkeypatch):
    """A corrupt role value must remain distinguishable from an absent assignment."""
    monkeypatch.setattr(session, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(session, "get_user_role", lambda *_args: "superuser")
    monkeypatch.setattr(session, "session_scope", lambda: nullcontext(object()))

    result = session._lookup_table_role("viewer@example.com")

    assert getattr(result, "state", None) == "invalid"


def test_allowlisted_user_falls_back_to_viewer_when_role_lookup_is_unavailable(
    monkeypatch,
):
    """A database outage must never elevate an explicitly assigned viewer."""
    monkeypatch.setenv("ALLOWED_EMAILS", "viewer@example.com")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    monkeypatch.setattr(session, "ensure_database_schema", lambda: False)
    monkeypatch.setattr(session, "get_user_role", lambda *_args: None)
    fake_st = _signed_in("viewer@example.com", "Viewer")

    user = require_authorized_user(fake_st)

    assert user.role is Role.VIEWER


@pytest.mark.parametrize("lookup_state", ["unavailable", "invalid"])
def test_table_only_user_is_denied_when_role_lookup_cannot_be_trusted(
    monkeypatch, lookup_state
):
    """A stale remembered table grant cannot survive an unavailable/invalid read."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "")
    monkeypatch.setenv("SCANNER_ENV", "production")
    monkeypatch.setattr(
        session,
        "_lookup_table_role",
        lambda _email: session._RoleLookupResult(lookup_state),
    )
    fake_st = _signed_in("table-only@example.com", "Table only")

    with pytest.raises(_StopCalled):
        require_authorized_user(fake_st)

    assert fake_st.errors == [
        "You are not authorized to access this app. "
        "Ask the administrator to add your email to the allowlist."
    ]


@pytest.mark.parametrize("lookup_state", ["unavailable", "invalid"])
def test_env_admin_stays_admin_when_role_lookup_cannot_be_trusted(
    monkeypatch, lookup_state
):
    """The ADMIN_EMAILS floor remains the independent break-glass authority."""
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("SCANNER_ENV", "production")
    monkeypatch.setattr(
        session,
        "_lookup_table_role",
        lambda _email: session._RoleLookupResult(lookup_state),
    )

    user = require_authorized_user(_signed_in("boss@example.com", "Boss"))

    assert user.role is Role.ADMIN


def test_require_capability_allows_when_role_has_capability():
    """A capable role passes straight through with no error and no stop."""
    fake_st = _FakeStreamlit()
    assert require_capability(fake_st, role=Role.VIEWER, capability=VIEW_RESULTS) is None
    assert require_capability(fake_st, role=Role.ANALYST, capability=RUN_SCAN) is None
    assert require_capability(fake_st, role=Role.ADMIN, capability=RUN_SCAN) is None
    assert fake_st.errors == []


def test_require_capability_denies_logs_and_audits(monkeypatch, caplog):
    """A role below the minimum is logged, audited, shown a message, and stopped."""
    fake_st = _FakeStreamlit()
    audited: list[dict] = []
    monkeypatch.setattr(
        session, "record_audit_event", lambda **kwargs: audited.append(kwargs) or True
    )

    with caplog.at_level(logging.WARNING), pytest.raises(_StopCalled):
        require_capability(
            fake_st, role=Role.VIEWER, capability=RUN_SCAN, email="viewer@example.com"
        )

    # Generic message — never reveals the policy or the role table.
    assert any("permission" in message.lower() for message in fake_st.errors)
    # OBS-001 structured log event.
    events = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_ROLE_DENIED
    ]
    assert len(events) == 1
    assert events[0]["required_capability"] == RUN_SCAN
    assert events[0]["role"] == "viewer"
    # OBS-003 durable audit (best-effort path: the fake has no session_state).
    assert len(audited) == 1
    assert audited[0]["event"] == EVENT_ROLE_DENIED
    assert audited[0]["user_email"] == "viewer@example.com"
    assert audited[0]["metadata"]["actual_role"] == "viewer"
    assert audited[0]["metadata"]["required_capability"] == RUN_SCAN
