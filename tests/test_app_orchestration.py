"""Focused tests for Streamlit orchestration code.

These tests patch Streamlit and the data loader with tiny fakes. That keeps the
test fast and lets us verify app-level parameter wiring without launching a
browser, opening a Dhan connection, or rendering real UI widgets.
"""

from __future__ import annotations

import os
import time
from datetime import date as real_date
from types import SimpleNamespace

import pandas as pd
import pytest

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


class _FakeExpander:
    """Context manager fake for Streamlit expanders used in app tests."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc_info):
        return False


class _FakeDataLoader:
    """Data loader fake carrying the status fields `_execute_screener` reads."""

    def __init__(self, _client):
        self.last_failures = []
        self.last_cache_hits = 0
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0


def test_scan_history_start_date_subtracts_calendar_years_and_handles_leap_day(monkeypatch):
    class LeapDay(real_date):
        @classmethod
        def today(cls) -> "LeapDay":
            return cls(2024, 2, 29)

    monkeypatch.setattr(app, "date", LeapDay)

    assert app._scan_history_start_date() == real_date(2014, 2, 28)


def test_execute_screener_uses_ten_year_data_window_independent_of_lookback(monkeypatch):
    """Screener lookback is display/strategy metadata; candle history is always 10y."""
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
    assert captured_params["start_date"] == real_date(2016, 6, 2)


def test_redact_secrets_masks_serpapi_and_agent_keys(monkeypatch):
    monkeypatch.setenv("SERPAPI_API_KEY", "serp-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    monkeypatch.setattr(
        app,
        "get_dhan_credentials",
        lambda required=False: SimpleNamespace(
            client_code="client-secret",
            access_token="token-secret",
        ),
    )

    redacted = app._redact_secrets(
        "serp-secret anthropic-secret client-secret token-secret still-visible"
    )

    assert "serp-secret" not in redacted
    assert "anthropic-secret" not in redacted
    assert "client-secret" not in redacted
    assert "token-secret" not in redacted
    assert "still-visible" in redacted
    assert redacted.count("***REDACTED***") == 4


def test_redact_secrets_masks_streamlit_auth_secrets(monkeypatch):
    """OIDC config values should be treated like broker/API secrets in errors."""
    monkeypatch.setattr(app, "get_dhan_credentials", lambda required=False: None)
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            secrets={
                "auth": {
                    "cookie_secret": "cookie-secret",
                    "google": {
                        "client_id": "google-client",
                        "client_secret": "google-secret",
                    },
                }
            }
        ),
    )

    redacted = app._redact_secrets(
        "cookie-secret google-client google-secret still-visible"
    )

    assert "cookie-secret" not in redacted
    assert "google-client" not in redacted
    assert "google-secret" not in redacted
    assert "still-visible" in redacted
    assert redacted.count("***REDACTED***") == 3


def test_main_requires_auth_before_discovering_screeners(monkeypatch):
    """The main app must not discover or run screeners before auth succeeds."""

    class StopFromAuth(RuntimeError):
        """Test-only signal that the auth gate stopped the Streamlit run."""

        pass

    def stop_at_auth(_st):
        # A real Streamlit stop would end this script run. Raising lets pytest
        # assert that the run stopped before `discover_screeners()` was called.
        raise StopFromAuth()

    def fail_if_discovered():
        raise AssertionError("screener discovery must wait for authentication")

    monkeypatch.setattr(app, "require_authenticated_user", stop_at_auth)
    monkeypatch.setattr(app, "discover_screeners", fail_if_discovered)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
        ),
    )

    with pytest.raises(StopFromAuth):
        app.main()


def test_universe_table_defers_status_loading_until_user_opts_in(monkeypatch):
    """Collapsed universe details should not scan every universe on each rerun."""

    def fail_if_loaded():
        raise AssertionError("universe statuses should load only after opt-in")

    monkeypatch.setattr(app, "all_universe_statuses", fail_if_loaded)
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            expander=lambda *_args, **_kwargs: _FakeExpander(),
            toggle=lambda *_args, **_kwargs: False,
            dataframe=lambda *_args, **_kwargs: None,
        ),
    )

    app.render_universe_table()


def test_chart_payload_cache_reuses_html_until_cache_file_changes(monkeypatch, tmp_path):
    """Chart reruns should reuse HTML while candles, params, and screener stay stable."""
    chart_file = tmp_path / "DEMO_1.parquet"
    chart_file.write_bytes(b"first")

    class FakeLoader:
        def __init__(self):
            self.read_calls = 0

        def cache_path(self, symbol, security_id):
            return chart_file

        def read_cached_history(self, symbol, security_id):
            self.read_calls += 1
            return pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2026-01-01")],
                    "open": [10.0],
                    "high": [11.0],
                    "low": [9.0],
                    "close": [10.5],
                }
            )

    build_calls = 0

    def build_chart(candles, params):
        nonlocal build_calls
        build_calls += 1
        return {"title": f"demo-{params['period']}", "height": 321, "panes": []}

    selected = ScreenerDefinition(
        key="demo",
        name="Demo",
        description="Test-only screener",
        universe="demo_universe",
        timeframe="daily",
        lookback_days=30,
        default_params={"period": 20},
        module_name="screeners.demo",
        run=lambda *_args, **_kwargs: pd.DataFrame(),
        build_chart=build_chart,
    )
    loader = FakeLoader()
    monkeypatch.setattr(app, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(app, "render_chart_html", lambda spec: f"<html>{spec['title']}</html>")

    first = app._get_or_build_chart_payload(selected, "DEMO", "1", loader, {"period": 20})
    second = app._get_or_build_chart_payload(selected, "DEMO", "1", loader, {"period": 20})

    assert first is not None
    assert second is not None
    assert first.html == second.html
    assert second.from_cache is True
    assert loader.read_calls == 1
    assert build_calls == 1

    # A changed parquet mtime means the underlying candles may have changed, so
    # the chart cache must miss and rebuild.
    newer = time.time() + 5
    os.utime(chart_file, (newer, newer))
    third = app._get_or_build_chart_payload(selected, "DEMO", "1", loader, {"period": 20})

    assert third is not None
    assert third.from_cache is False
    assert loader.read_calls == 2
    assert build_calls == 2
