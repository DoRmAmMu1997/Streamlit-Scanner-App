"""Focused tests for Streamlit orchestration code.

These tests patch Streamlit and the data loader with tiny fakes. That keeps the
test fast and lets us verify app-level parameter wiring without launching a
browser, opening a Dhan connection, or rendering real UI widgets.
"""

from __future__ import annotations

from datetime import date as real_date
from datetime import timedelta
from types import SimpleNamespace

import pandas as pd

import app
from backend.screener_registry import ScreenerDefinition


class _FixedDate(real_date):
    """Freeze `date.today()` while keeping normal date arithmetic available."""

    @classmethod
    def today(cls) -> "_FixedDate":
        return cls(2026, 6, 2)


class _FakeProgress:
    """Small Streamlit progress placeholder used by `_execute_screener`."""

    def progress(self, _value):
        pass

    def empty(self):
        pass


class _FakeEmpty:
    """Small Streamlit empty placeholder used for scan status text."""

    def markdown(self, _text):
        pass

    def empty(self):
        pass


class _FakeDataLoader:
    """Data loader fake carrying the status fields `_execute_screener` reads."""

    def __init__(self, _client):
        self.last_failures = []
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0


def test_execute_screener_uses_selected_lookback_days(monkeypatch):
    """The UI promise says each screener controls its own lookback window."""
    captured_params: dict = {}

    def fake_run(universe_df, data_loader, params):
        captured_params.update(params)
        return pd.DataFrame(
            [],
            columns=["symbol", "rating", "signal_date", "close", "reason"],
        )

    selected = ScreenerDefinition(
        key="demo",
        name="Demo",
        description="Test-only screener",
        universe="demo_universe",
        timeframe="daily",
        lookback_days=30,
        default_params={},
        module_name="screeners.demo",
        run=fake_run,
    )

    monkeypatch.setattr(app, "date", _FixedDate)
    monkeypatch.setattr(app, "credential_status", lambda: {"ready": True})
    monkeypatch.setattr(app, "load_universe", lambda _key: pd.DataFrame({"symbol": ["DEMO"]}))
    monkeypatch.setattr(app, "DailyDataLoader", _FakeDataLoader)
    monkeypatch.setattr(
        app,
        "DhanDataClient",
        SimpleNamespace(from_env=lambda: object()),
    )
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            progress=lambda _value: _FakeProgress(),
            empty=lambda: _FakeEmpty(),
            error=lambda message: (_ for _ in ()).throw(AssertionError(message)),
        ),
    )

    cache = app._execute_screener(selected)

    assert cache is not None
    assert captured_params["end_date"] == real_date(2026, 6, 2)
    assert captured_params["start_date"] == real_date(2026, 6, 2) - timedelta(days=30)
