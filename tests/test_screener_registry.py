from __future__ import annotations

"""Tests for the pluggable screener discovery contract."""

from types import ModuleType

import pandas as pd
import pytest

from backend.screener_registry import ScreenerRegistryError, discover_screeners, validate_screener_module


def test_discover_screeners_loads_stochastic_swing():
    # The Stochastic screener replaced the old connection-test screener.
    screeners = discover_screeners()

    assert "stochastic_swing" in screeners
    assert screeners["stochastic_swing"].universe == "nifty_500"
    # Every discovered screener exposes a chart builder.
    assert screeners["stochastic_swing"].build_chart is not None
    # The connection-test screener was removed.
    assert "connection_test" not in screeners


def test_validate_screener_module_rejects_missing_metadata():
    # A Python module without SCREENER metadata should not appear in the UI.
    module = ModuleType("bad_screener")

    with pytest.raises(ScreenerRegistryError):
        validate_screener_module(module)


def test_validate_screener_module_accepts_valid_contract():
    # Build a tiny in-memory module so this test focuses only on contract
    # validation, not filesystem discovery.
    module = ModuleType("good_screener")
    module.SCREENER = {
        "key": "good",
        "name": "Good",
        "description": "Good test screener",
        "universe": "nifty_500",
        "timeframe": "daily",
        "lookback_days": 100,
    }

    def run(universe_df, data_loader, params) -> pd.DataFrame:
        # The body is unimportant here; the registry only validates signature
        # and metadata.
        return pd.DataFrame()

    module.run = run

    definition = validate_screener_module(module)

    assert definition.key == "good"
    # `build_chart` is optional; an absent attribute should resolve to None.
    assert definition.build_chart is None


def test_validate_screener_module_registers_build_chart_when_present():
    module = ModuleType("charty_screener")
    module.SCREENER = {
        "key": "charty",
        "name": "Charty",
        "description": "Has a chart",
        "universe": "nifty_500",
        "timeframe": "daily",
        "lookback_days": 50,
    }

    def run(universe_df, data_loader, params) -> pd.DataFrame:
        return pd.DataFrame()

    def build_chart(candles, params):  # noqa: ANN001 - test stub only
        return "fig"

    module.run = run
    module.build_chart = build_chart

    definition = validate_screener_module(module)

    assert definition.build_chart is build_chart


def test_validate_screener_module_rejects_non_callable_build_chart():
    module = ModuleType("broken_chart_screener")
    module.SCREENER = {
        "key": "broken_chart",
        "name": "Broken Chart",
        "description": "build_chart should be callable",
        "universe": "nifty_500",
        "timeframe": "daily",
        "lookback_days": 50,
    }

    def run(universe_df, data_loader, params) -> pd.DataFrame:
        return pd.DataFrame()

    module.run = run
    module.build_chart = "not callable"

    with pytest.raises(ScreenerRegistryError):
        validate_screener_module(module)
