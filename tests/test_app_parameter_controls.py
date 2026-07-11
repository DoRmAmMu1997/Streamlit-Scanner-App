"""Render-path tests for ui/parameter_controls.py (REF-003).

The sidebar parameter-override helpers moved out of app.py with no direct
tests. House pattern: monkeypatch ``ui.parameter_controls.st`` with a
recording fake and drive the real helpers — session-state key namespacing,
widget dispatch per default type, the reset button, and the override merge.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.screener_registry import ScreenerDefinition
from ui import parameter_controls


class _RerunCalled(Exception):
    """Test stand-in for Streamlit's RerunException.

    The real ``st.rerun()`` raises to abort the script run, so the fake must
    raise too — otherwise the code under test would continue past the reset
    branch in a way production never does.
    """


class _FakeExpander:
    def __init__(self, owner: _FakeStreamlit, label: str):
        owner.expanders.append(label)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.session_state: dict = {}
        self.expanders: list[str] = []
        self.captions: list[str] = []
        self.checkboxes: list[tuple[str, str]] = []
        self.number_inputs: list[dict] = []
        # Programmed return for the reset button (True = user clicked).
        self.button_clicked = False
        self.rerun_calls = 0

    def expander(self, label, **_kwargs):
        return _FakeExpander(self, str(label))

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def button(self, _label, **_kwargs):
        return self.button_clicked

    def checkbox(self, label, *, key):
        self.checkboxes.append((str(label), key))

    def number_input(self, label, *, key, **kwargs):
        self.number_inputs.append({"label": str(label), "key": key, **kwargs})

    def rerun(self):
        self.rerun_calls += 1
        raise _RerunCalled()


def _definition(default_params: dict) -> ScreenerDefinition:
    return ScreenerDefinition(
        key="demo",
        name="Demo screener",
        description="test",
        universe="nifty_100",
        timeframe="1d",
        lookback_days=100,
        default_params=default_params,
        module_name="demo",
        run=lambda **_kwargs: pd.DataFrame(),
    )


@pytest.fixture()
def fake_st(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setattr(parameter_controls, "st", fake)
    return fake


def test_param_state_key_namespaces_by_screener_and_param():
    """`discount_pct` on screener A must never collide with screener B's."""
    key_a = parameter_controls._param_state_key("screener_a", "discount_pct")
    key_b = parameter_controls._param_state_key("screener_b", "discount_pct")
    assert key_a == "param_override::screener_a::discount_pct"
    assert key_a != key_b


def test_no_tunable_params_skips_the_expander_entirely(fake_st):
    parameter_controls._render_parameter_overrides(_definition({}))
    assert fake_st.expanders == []


def test_first_render_seeds_state_and_dispatches_widget_per_type(fake_st):
    """bool -> checkbox (checked BEFORE int: bool is an int subclass),
    int -> stepped number_input, float -> 4-decimal number_input."""
    defaults = {"use_filter": True, "period": 20, "discount_pct": 0.14}

    parameter_controls._render_parameter_overrides(_definition(defaults))

    assert fake_st.expanders == ["Tune parameters"]
    # Every default was seeded into session_state under its namespaced key.
    for param_key, default_value in defaults.items():
        state_key = parameter_controls._param_state_key("demo", param_key)
        assert fake_st.session_state[state_key] == default_value
    # The bool went to a checkbox even though isinstance(True, int) is True.
    assert fake_st.checkboxes == [("use_filter", "param_override::demo::use_filter")]
    assert [entry["label"] for entry in fake_st.number_inputs] == ["period", "discount_pct"]
    assert fake_st.number_inputs[0]["step"] == 1
    assert fake_st.number_inputs[1]["format"] == "%.4f"


def test_rerender_preserves_user_edited_values(fake_st):
    """Seeding must only happen on first render — an edited value survives."""
    state_key = parameter_controls._param_state_key("demo", "period")
    fake_st.session_state[state_key] = 55  # user edit from a previous rerun

    parameter_controls._render_parameter_overrides(_definition({"period": 20}))

    assert fake_st.session_state[state_key] == 55


def test_reset_button_pops_overrides_and_reruns(fake_st):
    defaults = {"period": 20, "discount_pct": 0.14}
    for param_key in defaults:
        state_key = parameter_controls._param_state_key("demo", param_key)
        fake_st.session_state[state_key] = 99  # user edits to discard
    fake_st.button_clicked = True

    with pytest.raises(_RerunCalled):
        parameter_controls._render_parameter_overrides(_definition(defaults))

    assert fake_st.rerun_calls == 1
    # All override keys were removed so the next render reseeds the defaults.
    assert not any(key.startswith("param_override::") for key in fake_st.session_state)


def test_apply_param_overrides_merges_only_declared_keys(fake_st):
    """Random session_state keys must not leak into screener params."""
    fake_st.session_state[parameter_controls._param_state_key("demo", "period")] = 55
    # A key that LOOKS like an override but is not declared in default_params.
    fake_st.session_state["param_override::demo::rogue"] = 1
    fake_st.session_state["unrelated"] = "noise"

    params = {"period": 20, "discount_pct": 0.14}
    returned = parameter_controls._apply_param_overrides(
        _definition({"period": 20, "discount_pct": 0.14}), params
    )

    assert returned is params  # mutated in place and returned for chaining
    assert params == {"period": 55, "discount_pct": 0.14}


def test_apply_param_overrides_without_state_returns_params_unchanged(fake_st):
    params = {"period": 20}
    parameter_controls._apply_param_overrides(_definition({"period": 20}), params)
    assert params == {"period": 20}
