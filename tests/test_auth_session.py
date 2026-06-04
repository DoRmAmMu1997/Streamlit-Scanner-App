"""Tests for the Streamlit OIDC authentication gate.

These tests avoid opening a browser or contacting Google. Instead they pass a
small fake ``st`` object into the auth helper and assert which Streamlit methods
would have been called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.auth import session
from backend.auth.session import (
    AuthenticatedUser,
    is_email_authorized,
    require_authenticated_user,
    require_authorized_user,
)


class _StopCalled(RuntimeError):
    """Raised by the fake Streamlit object when st.stop() is invoked.

    Real ``st.stop()`` interrupts the current Streamlit script run. Raising a
    test-only exception gives us the same "nothing below this line should run"
    signal in ordinary pytest code.
    """


class _FakeSidebar:
    """Minimal context manager for code that writes into ``with st.sidebar``."""

    def __init__(self, owner: "_FakeStreamlit"):
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

    assert user == AuthenticatedUser(email="sunny@example.com", name="Sunny", is_admin=False)
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
    """
    monkeypatch.setenv("ALLOWED_EMAILS", "")
    monkeypatch.setenv("ADMIN_EMAILS", "boss@example.com")
    monkeypatch.setenv("SCANNER_ENV", "production")
    monkeypatch.setattr(
        session,
        "require_authenticated_user",
        lambda _st: AuthenticatedUser(email="BOSS@example.com", name="Boss"),
    )
    fake_st = _FakeStreamlit()

    user = require_authorized_user(fake_st)

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
