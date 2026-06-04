"""Tests for the Streamlit OIDC authentication gate.

These tests avoid opening a browser or contacting Google. Instead they pass a
small fake ``st`` object into the auth helper and assert which Streamlit methods
would have been called.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from backend.auth.session import AuthenticatedUser, require_authenticated_user


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
