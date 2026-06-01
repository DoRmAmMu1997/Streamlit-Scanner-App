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
    # The split Bollinger / Envelope / Envelope+Knoxville screeners (formerly the
    # combined "Bollinger Knoxville Buy" + "14% Below 200 EMA") are Hemant Super 45.
    assert screeners["bollinger_lower_band"].universe == "hemant_super_45"
    assert screeners["envelope"].universe == "hemant_super_45"
    assert screeners["envelope_knoxville_buy"].universe == "hemant_super_45"
    assert screeners["week52_low_ceyhun"].universe == "hemant_super_45"
    # The green-candle screener scans the Hemant Super 45 + Good 45 union.
    assert screeners["green_candles_20pct_up"].universe == "hemant_super_good_union"
    # Every discovered screener exposes a chart builder.
    assert screeners["stochastic_swing"].build_chart is not None
    assert screeners["bollinger_lower_band"].build_chart is not None
    assert screeners["envelope"].build_chart is not None
    assert screeners["envelope_knoxville_buy"].build_chart is not None
    assert screeners["week52_low_ceyhun"].build_chart is not None
    assert screeners["green_candles_20pct_up"].build_chart is not None
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


def test_validate_screener_module_handles_basescanner_subclass():
    # Class-based screeners are the new preferred pattern. The registry should
    # find a BaseScanner subclass inside a module, instantiate it, and pull
    # metadata/run/build_chart from the instance.
    from backend.scanner_base import BaseScanner

    module = ModuleType("class_screener")

    class MyClassScanner(BaseScanner):
        SCREENER = {
            "key": "class_demo",
            "name": "Class Demo",
            "description": "A class-based screener.",
            "universe": "nifty_500",
            "timeframe": "daily",
            "lookback_days": 30,
            "default_params": {"period": 14},
        }

        def compute_signal(self, symbol, candles, params):
            return None

        def build_chart(self, candles, params):
            return {"title": "demo"}

    # The class must claim to live in the same module the registry inspects,
    # otherwise the registry will (correctly) skip it as imported-from-elsewhere.
    MyClassScanner.__module__ = module.__name__
    module.MyClassScanner = MyClassScanner

    definition = validate_screener_module(module)

    assert definition.key == "class_demo"
    assert definition.universe == "nifty_500"
    # build_chart was overridden so it should make it into the definition.
    assert definition.build_chart is not None
    # The bound method's signature still validates as (universe_df, data_loader, params).
    assert callable(definition.run)


def test_validate_screener_module_hides_default_basescanner_chart():
    # A BaseScanner subclass that does NOT override build_chart should expose
    # `build_chart = None` to the UI so the chart pane stays hidden.
    from backend.scanner_base import BaseScanner

    module = ModuleType("chartless_class_screener")

    class NoChart(BaseScanner):
        SCREENER = {
            "key": "no_chart",
            "name": "No Chart",
            "description": "Plain class screener with no overridden chart.",
            "universe": "nifty_500",
            "timeframe": "daily",
            "lookback_days": 10,
            "default_params": {},
        }

        def compute_signal(self, symbol, candles, params):
            return None

    NoChart.__module__ = module.__name__
    module.NoChart = NoChart

    definition = validate_screener_module(module)

    assert definition.build_chart is None
