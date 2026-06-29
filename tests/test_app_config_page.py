"""Tests for the OBS-003 admin runtime-config page (ui.config_page).

Focus: the admin guard (security) and the submit/feedback flow with the config
service fully faked so no real database or environment write happens here.
"""

from __future__ import annotations

from backend.admin import ConfigUpdateResult
from backend.auth.roles import Role
from backend.auth.session import AuthenticatedUser
from backend.config.settings import SettingsError
from ui import config_page


class _FakeForm:
    """Context manager standing in for ``st.form``."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeStreamlit:
    """Minimal Streamlit surface used by the config page renderer tests."""

    def __init__(self, *, submit: bool, text_values: dict[str, str] | None = None):
        self._submit = submit
        self._text_values = text_values or {}
        self.successes: list[str] = []
        self.infos: list[str] = []
        self.errors: list[str] = []
        self.selectboxes: list[str] = []
        self.text_inputs: list[str] = []

    def subheader(self, *_args, **_kwargs):
        pass

    def caption(self, *_args, **_kwargs):
        pass

    def form(self, *_args, **_kwargs):
        return _FakeForm()

    def selectbox(self, label, choices, index=0, **_kwargs):
        self.selectboxes.append(label)
        return choices[index]

    def text_input(self, label, value="", **_kwargs):
        self.text_inputs.append(label)
        return self._text_values.get(label, value)

    def form_submit_button(self, *_args, **_kwargs):
        return self._submit

    def success(self, text, **_kwargs):
        self.successes.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))

    def error(self, text, **_kwargs):
        self.errors.append(str(text))


def test_config_page_rejects_non_admin(monkeypatch):
    fake_st = _FakeStreamlit(submit=False)
    monkeypatch.setattr(config_page, "st", fake_st)

    config_page._render_config_page(
        AuthenticatedUser("person@example.com", "Person", role=Role.ANALYST)
    )

    assert fake_st.errors == ["Admin access is required to change settings."]


def test_config_page_rejects_auth_disabled_session(monkeypatch):
    fake_st = _FakeStreamlit(submit=False)
    monkeypatch.setattr(config_page, "st", fake_st)

    config_page._render_config_page(None)

    assert fake_st.errors == ["Admin access is required to change settings."]


def test_config_page_does_nothing_until_submitted(monkeypatch):
    fake_st = _FakeStreamlit(submit=False)
    monkeypatch.setattr(config_page, "st", fake_st)
    monkeypatch.setattr(
        config_page,
        "update_config_value",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("must not save before submit")
        ),
    )

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert fake_st.successes == []
    assert fake_st.infos == []


def test_config_page_reports_a_change_on_submit(monkeypatch):
    fake_st = _FakeStreamlit(submit=True)
    monkeypatch.setattr(config_page, "st", fake_st)
    monkeypatch.setattr(
        config_page,
        "update_config_value",
        lambda key, value, *, updated_by: ConfigUpdateResult(
            key=key, old_value="WARNING", new_value=value, changed=True
        ),
    )

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert fake_st.successes  # at least one "old -> new" confirmation
    assert fake_st.errors == []


def test_config_page_reports_no_changes(monkeypatch):
    fake_st = _FakeStreamlit(submit=True)
    monkeypatch.setattr(config_page, "st", fake_st)
    monkeypatch.setattr(
        config_page,
        "update_config_value",
        lambda key, value, *, updated_by: ConfigUpdateResult(
            key=key, old_value=value, new_value=value, changed=False
        ),
    )

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert fake_st.infos == ["No changes to save."]


def test_config_page_surfaces_validation_errors(monkeypatch):
    fake_st = _FakeStreamlit(submit=True)
    monkeypatch.setattr(config_page, "st", fake_st)

    def boom(key, value, *, updated_by):
        raise SettingsError(f"bad value for {key}")

    monkeypatch.setattr(config_page, "update_config_value", boom)

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert fake_st.errors
    assert fake_st.successes == []


def test_config_page_renders_alert_preference_controls(monkeypatch):
    # ALERT-002: enable/content are choice select boxes; the destinations are
    # free-text inputs (validated on save). All four must appear on the form.
    fake_st = _FakeStreamlit(submit=False)
    monkeypatch.setattr(config_page, "st", fake_st)

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    assert "Daily alerts enabled" in fake_st.selectboxes
    assert "Alert content" in fake_st.selectboxes
    assert "Telegram chat id" in fake_st.text_inputs
    assert "Alert email recipient" in fake_st.text_inputs


def test_config_page_does_not_echo_destination_values_after_save(monkeypatch):
    destination = "private-recipient@example.com"
    old_destination = "old-recipient@example.com"
    fake_st = _FakeStreamlit(
        submit=True,
        text_values={"Alert email recipient": destination},
    )
    monkeypatch.setattr(config_page, "st", fake_st)

    def fake_update(key, value, *, updated_by):
        old_value = old_destination if key == "ALERT_EMAIL_TO" else value
        return ConfigUpdateResult(
            key=key,
            old_value=old_value,
            new_value=value,
            changed=key == "ALERT_EMAIL_TO",
        )

    monkeypatch.setattr(config_page, "update_config_value", fake_update)

    config_page._render_config_page(
        AuthenticatedUser("admin@example.com", "Admin", role=Role.ADMIN)
    )

    feedback = "\n".join(fake_st.successes)
    assert "Alert email recipient" in feedback
    assert destination not in feedback
    assert old_destination not in feedback
