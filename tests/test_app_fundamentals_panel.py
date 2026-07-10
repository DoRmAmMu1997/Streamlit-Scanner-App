"""Render-path tests for ui/fundamentals_panel.py (TEST-007).

REF-002 (#97) moved the Check Fundamentals panel out of app.py into
ui/fundamentals_panel.py, which counts toward the coverage floor but had no
direct tests. These tests exercise the real render paths with the house
pattern: monkeypatch ``ui.fundamentals_panel.st`` (the module that actually
reads Streamlit) with a recording fake, and drive the module through its
public seams.

Beginner note: no browser or Streamlit runtime is involved — the fake records
what WOULD have been rendered so assertions can check captions, metrics and
error handling deterministically.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.fundamentals.fundamental_agent import (
    AgentVerdict,
    CriterionResult,
    ForwardOutlook,
    FundamentalsUsageLimitError,
    Observation,
)
from ui import fundamentals_panel


class _FakeColumn:
    """One column returned by st.columns(): records buttons and metrics."""

    def __init__(self, owner: _FakeStreamlit):
        self._owner = owner

    def button(self, label, **kwargs):
        self._owner.buttons.append((label, kwargs))
        # Pop a programmed response; default False (not clicked).
        if self._owner.button_responses:
            return self._owner.button_responses.pop(0)
        return False

    def metric(self, label, value, **kwargs):
        self._owner.metrics.append((label, value))


class _FakeSpinner:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeStreamlit:
    """Minimal Streamlit surface used by the fundamentals panel."""

    def __init__(self):
        self.session_state: dict = {}
        self.captions: list[str] = []
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.markdowns: list[str] = []
        self.metrics: list[tuple[str, object]] = []
        self.buttons: list[tuple[str, dict]] = []
        self.dataframes: list[object] = []
        self.subheaders: list[str] = []
        # Queue of return values for successive button renders, in call order.
        self.button_responses: list[bool] = []

    def divider(self):
        pass

    def subheader(self, text, **_kwargs):
        self.subheaders.append(str(text))

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def spinner(self, *_args, **_kwargs):
        return _FakeSpinner()

    def error(self, text, **_kwargs):
        self.errors.append(str(text))

    def warning(self, text, **_kwargs):
        self.warnings.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))

    def markdown(self, text, **_kwargs):
        self.markdowns.append(str(text))

    def dataframe(self, data, **_kwargs):
        self.dataframes.append(data)


def _verdict(*, mode: str = "criteria", total: int = 9) -> AgentVerdict:
    """A small but fully valid AgentVerdict for render tests."""
    return AgentVerdict(
        symbol="INFY",
        mode=mode,  # type: ignore[arg-type]
        rating=8,
        passed_criteria_count=7,
        total_criteria=total,
        criteria_breakdown=[
            CriterionResult(
                name="ROE > 15%",
                passed=True,
                measured_value="22%",
                threshold="> 15%",
                reasoning="ROE 22% per screener.in ratios.",
            )
        ],
        additional_observations=[
            Observation(
                topic="Margins",
                finding="Operating margin stable across 3 years.",
                sentiment="positive",
                evidence="OPM 21% / 21% / 22%.",
            )
        ],
        summary_comments="Solid fundamentals overall.",
        forward_outlook=ForwardOutlook(overall_summary="Steady growth expected."),
        data_freshness="2026-07-06T08:15:23+00:00",
        model_used="test-model",
    )


@pytest.fixture()
def fake_st(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setattr(fundamentals_panel, "st", fake)
    # Deterministic model/fast-mode so session keys are stable and no config
    # or environment is read during the render.
    monkeypatch.setattr(fundamentals_panel, "get_fundamentals_model", lambda: "test-model")
    monkeypatch.setattr(fundamentals_panel, "get_agent_fast_mode", lambda: False)
    return fake


def test_no_symbol_renders_nothing(fake_st, monkeypatch):
    """The panel stays hidden only when no symbol is selected."""
    fundamentals_panel._render_fundamentals_panel(None)
    assert fake_st.subheaders == []
    assert fake_st.captions == []
    assert fake_st.buttons == []


def test_eligible_symbol_gets_criteria_caption(fake_st, monkeypatch):
    """HS45/N100 symbols run criteria mode: nine-criteria caption, no verdict yet."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: True)
    fundamentals_panel._render_fundamentals_panel("INFY")
    assert fake_st.subheaders == ["Fundamentals"]
    assert any("nine user-defined criteria" in caption for caption in fake_st.captions)
    # Button rendered enabled (no cached verdict) and not clicked -> no verdict block.
    label, kwargs = fake_st.buttons[0]
    assert label == "Check Fundamentals: INFY"
    assert kwargs["disabled"] is False
    assert fake_st.metrics == []


def test_ineligible_symbol_gets_universal_caption(fake_st, monkeypatch):
    """Everything else runs universal mode and the caption explains WHY (UI-002)."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: False)
    fundamentals_panel._render_fundamentals_panel("ZOMATO")
    universal = [caption for caption in fake_st.captions if "Universal mode" in caption]
    assert universal, fake_st.captions
    assert "ZOMATO" in universal[0]
    assert "outside Hemant Super 45" in universal[0]


def test_cached_verdict_disables_primary_and_renders_block(fake_st, monkeypatch):
    """A session-cached verdict disables the primary button, offers Re-run, and
    renders the verdict block with the provenance caption (UI-002)."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: True)
    key = "fundamentals_verdict::INFY::test-model::criteria"
    fake_st.session_state[key] = _verdict().model_dump(mode="json")

    fundamentals_panel._render_fundamentals_panel("INFY")

    primary_label, primary_kwargs = fake_st.buttons[0]
    assert primary_label == "View cached verdict: INFY"
    assert primary_kwargs["disabled"] is True
    rerun_label, _ = fake_st.buttons[1]
    assert rerun_label == "Re-run analysis"
    assert any("cached in this session" in caption for caption in fake_st.captions)
    # Verdict block rendered: rating metric + criteria table + summary info.
    assert ("Fundamental rating", "8/10") in fake_st.metrics
    assert len(fake_st.dataframes) == 1
    assert fake_st.infos == ["Solid fundamentals overall."]
    # Freshness caption humanized (UI-002).
    assert any("Data fetched: 06 Jul 2026, 08:15 UTC" in c for c in fake_st.captions)


def test_click_runs_agent_in_mode_and_caches_verdict(fake_st, monkeypatch):
    """Clicking runs the agent with the symbol-deterministic mode and caches
    the verdict dict in session state."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: False)
    calls: list[tuple[str, bool, str]] = []

    class _FakeAgent:
        def check(self, symbol, *, force_refresh, mode):
            calls.append((symbol, force_refresh, mode))
            return _verdict(mode="universal", total=7)

    monkeypatch.setattr(
        fundamentals_panel, "_get_fundamental_agent", lambda model, fast: _FakeAgent()
    )
    fake_st.button_responses = [True]  # primary button clicked

    fundamentals_panel._render_fundamentals_panel("ZOMATO")

    assert calls == [("ZOMATO", False, "universal")]
    cached = fake_st.session_state["fundamentals_verdict::ZOMATO::test-model::universal"]
    assert cached["rating"] == 8
    assert ("Criteria passed", "7 / 7") in fake_st.metrics


def test_usage_limit_shows_gentle_warning_not_error(fake_st, monkeypatch):
    """Plan-limit exhaustion is an expected state: warning, no error, no cache."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: True)

    class _LimitedAgent:
        def check(self, symbol, *, force_refresh, mode):
            raise FundamentalsUsageLimitError("usage limit reached; resets soon")

    monkeypatch.setattr(
        fundamentals_panel, "_get_fundamental_agent", lambda model, fast: _LimitedAgent()
    )
    fake_st.button_responses = [True]

    fundamentals_panel._render_fundamentals_panel("INFY")

    assert fake_st.warnings and "usage limit" in fake_st.warnings[0]
    assert fake_st.errors == []
    assert "fundamentals_verdict::INFY::test-model::criteria" not in fake_st.session_state


def test_agent_failure_shows_error_and_keeps_session_clean(fake_st, monkeypatch):
    """An unexpected agent failure surfaces as st.error (redacted) and nothing
    is cached."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: True)

    class _BrokenAgent:
        def check(self, symbol, *, force_refresh, mode):
            raise RuntimeError("screener.in fetch exploded")

    monkeypatch.setattr(
        fundamentals_panel, "_get_fundamental_agent", lambda model, fast: _BrokenAgent()
    )
    fake_st.button_responses = [True]

    fundamentals_panel._render_fundamentals_panel("INFY")

    assert fake_st.errors and "Fundamental check failed" in fake_st.errors[0]
    assert "fundamentals_verdict::INFY::test-model::criteria" not in fake_st.session_state


def test_invalid_cached_verdict_is_cleared_with_error(fake_st, monkeypatch):
    """A cached dict that no longer validates is dropped, not rendered."""
    monkeypatch.setattr(fundamentals_panel, "_is_eligible_for_fundamentals", lambda s: True)
    key = "fundamentals_verdict::INFY::test-model::criteria"
    fake_st.session_state[key] = {"rating": "not-a-verdict"}

    fundamentals_panel._render_fundamentals_panel("INFY")

    assert key not in fake_st.session_state
    assert fake_st.errors and "could not be parsed" in fake_st.errors[0]


# ---------------------------------------------------------------------------
# Eligibility helpers (real path, faked universe loader)
# ---------------------------------------------------------------------------


def test_eligibility_uses_union_of_universes(monkeypatch):
    """Symbols from EITHER curated universe are eligible; others are not."""

    def _fake_load(key):
        if key == "hemant_super_45":
            return pd.DataFrame({"symbol": ["infy ", "TCS"]})
        return pd.DataFrame({"symbol": ["HDFCBANK"]})

    monkeypatch.setattr(fundamentals_panel, "load_universe", _fake_load)
    fundamentals_panel._eligible_symbols_set.clear()
    try:
        assert fundamentals_panel._is_eligible_for_fundamentals("INFY") is True
        assert fundamentals_panel._is_eligible_for_fundamentals("hdfcbank") is True
        assert fundamentals_panel._is_eligible_for_fundamentals("ZOMATO") is False
        assert fundamentals_panel._is_eligible_for_fundamentals(None) is False
        assert fundamentals_panel._is_eligible_for_fundamentals("") is False
    finally:
        fundamentals_panel._eligible_symbols_set.clear()


def test_eligibility_survives_a_missing_universe(monkeypatch):
    """A missing universe CSV must not break the UI: the other universe still counts."""

    def _fake_load(key):
        if key == "hemant_super_45":
            raise FileNotFoundError(key)
        return pd.DataFrame({"symbol": ["HDFCBANK"]})

    monkeypatch.setattr(fundamentals_panel, "load_universe", _fake_load)
    fundamentals_panel._eligible_symbols_set.clear()
    try:
        assert fundamentals_panel._is_eligible_for_fundamentals("HDFCBANK") is True
        assert fundamentals_panel._is_eligible_for_fundamentals("INFY") is False
    finally:
        fundamentals_panel._eligible_symbols_set.clear()


# ---------------------------------------------------------------------------
# _format_data_freshness (pure, UI-002)
# ---------------------------------------------------------------------------


def test_format_data_freshness_zone_aware_normalizes_to_utc():
    formatted = fundamentals_panel._format_data_freshness("2026-07-06T13:45:00+05:30")
    assert formatted == "06 Jul 2026, 08:15 UTC"


def test_format_data_freshness_naive_has_no_utc_suffix():
    formatted = fundamentals_panel._format_data_freshness("2026-07-06T08:15:00")
    assert formatted == "06 Jul 2026, 08:15"
    assert "UTC" not in formatted


def test_format_data_freshness_unparseable_renders_verbatim_backticked():
    assert fundamentals_panel._format_data_freshness("last tuesday") == "`last tuesday`"


def test_format_data_freshness_empty_is_unknown():
    assert fundamentals_panel._format_data_freshness("") == "unknown"
    assert fundamentals_panel._format_data_freshness(None) == "unknown"  # type: ignore[arg-type]
