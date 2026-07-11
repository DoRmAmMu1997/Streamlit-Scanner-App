"""Sidebar parameter-override widgets for the selected screener (REF-003).

Every screener declares `default_params` in its SCREENER dict. The sidebar
renders one editable widget per default so the user can A/B test parameter
tweaks (e.g. "what if discount_pct were 5% instead of 14%?") without editing
source code. Overrides live in `st.session_state` keyed by screener+param, so
switching screeners does not cross-contaminate values.

Beginner note:
This module was extracted from ``app.py`` (REF-003, the third slimming pass
after REF-001/REF-002). ``app.py`` re-exports these helpers so existing
imports of ``app._apply_param_overrides`` and friends keep working; the
implementation now lives here so it can be tested without loading the whole
entrypoint.
"""

from __future__ import annotations

from typing import Any

import streamlit as st

from backend.screener_registry import ScreenerDefinition


def _param_state_key(screener_key: str, param_key: str) -> str:
    """Stable session_state key for one (screener, parameter) override widget.

    Including both pieces ensures `discount_pct` on screener A does not
    overwrite `discount_pct` on screener B if both define one.
    """
    return f"param_override::{screener_key}::{param_key}"


def _render_parameter_overrides(selected: ScreenerDefinition) -> None:
    """Render an expandable sidebar block to tune the selected screener's params.

    Number-input widgets are bound to `st.session_state` directly via `key=`,
    so reading them back later (in `_apply_param_overrides`) does not need
    any extra plumbing.
    """
    defaults = dict(selected.default_params or {})
    if not defaults:
        # A screener without tunable params (rare) skips the expander entirely.
        return

    with st.expander("Tune parameters", expanded=False):
        st.caption(
            "Values override the screener's defaults for the **next** run. "
            "Click 'Reset to defaults' to discard your edits."
        )

        # The reset button removes any user-set keys so the next widget
        # render falls back to the screener's declared defaults. `st.rerun()`
        # gives the widgets a chance to repaint with the default values
        # immediately rather than waiting for the user's next interaction.
        if st.button(
            "Reset to defaults",
            key=f"reset_params_{selected.key}",
            help="Discard any parameter tweaks and use the screener's declared defaults.",
        ):
            for param_key in defaults:
                state_key = _param_state_key(selected.key, param_key)
                st.session_state.pop(state_key, None)
            st.rerun()

        for param_key, default_value in defaults.items():
            state_key = _param_state_key(selected.key, param_key)
            # Seed the session_state on the first render. Without this seed,
            # the number_input would use `value=default_value` only once and
            # then store its own state, which gets messy on screener switch.
            if state_key not in st.session_state:
                st.session_state[state_key] = default_value

            if isinstance(default_value, bool):
                st.checkbox(param_key, key=state_key)
            elif isinstance(default_value, int):
                # Integer parameters: step=1 keeps the widget arrows
                # incrementing cleanly. The default value (already in state)
                # tells Streamlit it is an int widget.
                st.number_input(param_key, step=1, key=state_key)
            else:
                # Float parameters: 4-decimal format covers percentages like
                # 0.0150 cleanly. The user can still type a wider value.
                st.number_input(param_key, key=state_key, format="%.4f")


def _apply_param_overrides(selected: ScreenerDefinition, params: dict[str, Any]) -> dict[str, Any]:
    """Merge any sidebar-edited values from `st.session_state` into `params`.

    `params` is mutated in place (and also returned) so the caller can chain
    if desired. Only keys declared in the screener's `default_params` are
    pulled — that keeps random session_state values from leaking through.
    """
    for param_key in selected.default_params or {}:
        state_key = _param_state_key(selected.key, param_key)
        if state_key in st.session_state:
            params[param_key] = st.session_state[state_key]
    return params
