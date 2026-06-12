"""Focused tests for Streamlit orchestration code.

These tests patch Streamlit and the data loader with tiny fakes. That keeps the
test fast and lets us verify app-level parameter wiring without launching a
browser, opening a Dhan connection, or rendering real UI widgets.
"""

from __future__ import annotations

import os
import time
from datetime import date as real_date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

import app
from backend.config.settings import get_settings
from backend.observability import EVENT_DATA_REFRESH_COMPLETED
from backend.scanning import ScanRunResult, ScanStatus
from backend.screener_registry import ScreenerDefinition

# Helpers that moved to ui/ (REF-001) read Streamlit and the chart renderer
# from their own modules; fakes must be patched there, not onto app.
from ui import chart_cache, common, health_page, history_page


class _FixedDate(real_date):
    """Freeze `date.today()` while keeping normal date arithmetic available."""

    @classmethod
    def today(cls) -> _FixedDate:
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
        def today(cls) -> LeapDay:
            return cls(2024, 2, 29)

    monkeypatch.setattr(app, "date", LeapDay)

    assert app._scan_history_start_date() == real_date(2014, 2, 28)


def test_execute_screener_uses_ten_year_data_window_independent_of_lookback(monkeypatch):
    """Screener lookback is display/strategy metadata; candle history is always 10y."""
    captured_params: dict = {}

    def fake_run_scan(
        *, screener_key, universe_key, run_callable, universe_df, data_loader,
        params, triggered_by="ui",
    ):
        # `_execute_screener` now delegates running + persistence to the service.
        # We assert the 10-year window via the params it forwards.
        captured_params.update(params)
        return ScanRunResult(status=ScanStatus.SUCCESS, results=pd.DataFrame())

    selected = ScreenerDefinition(
        key="demo",
        name="Demo",
        description="Test-only screener",
        universe="demo_universe",
        timeframe="daily",
        lookback_days=30,
        default_params={},
        module_name="screeners.demo",
        run=lambda *_args, **_kwargs: pd.DataFrame(),
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
    monkeypatch.setattr(app, "run_scan", fake_run_scan)
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
    monkeypatch.setenv("DHAN_CLIENT_ID", "client-secret")
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "token-secret")

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
    monkeypatch.setattr(
        common,
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


def test_redact_secrets_survives_settings_parse_errors(monkeypatch):
    """Displaying a settings error should not make redaction raise again."""
    # Simulate the exact failure mode we care about: settings parsing already
    # failed, and the app is trying to show that settings error to the user. The
    # redactor should degrade gracefully instead of raising a second SettingsError.
    monkeypatch.setenv("LOG_LEVEL", "chatty")

    assert app._redact_secrets("Invalid LOG_LEVEL") == "Invalid LOG_LEVEL"


def test_prefetch_redacts_dhan_setup_errors(monkeypatch, capsys):
    """The terminal prefetch path should not print raw broker tokens."""

    class _CleanupOnlyLoader:
        """Tiny loader fake used before the real Dhan client is constructed."""

        def __init__(self, client=None):
            pass

        def cleanup_legacy_cache_files(self):
            return 0

    def build_broken_dhan_client():
        # This mimics an SDK/setup error echoing a credential in its message.
        # The test asserts the terminal output sees only the redacted version.
        raise RuntimeError("Dhan setup failed with access_token=broker-token-secret")

    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "refresh_universe_files", lambda: {})
    monkeypatch.setattr(
        app,
        "union_of_mapped_universes",
        lambda: pd.DataFrame(
            [
                {
                    "symbol": "DEMO",
                    "security_id": "1",
                    "mapping_status": "mapped",
                }
            ]
        ),
    )
    monkeypatch.setattr(app, "DailyDataLoader", _CleanupOnlyLoader)
    monkeypatch.setattr(
        app,
        "DhanDataClient",
        SimpleNamespace(from_env=build_broken_dhan_client),
    )

    app.prefetch_data_assets()

    output = capsys.readouterr().out
    assert "broker-token-secret" not in output
    assert "***REDACTED***" in output


def test_prefetch_universe_failure_emits_terminal_structured_event(
    monkeypatch, caplog
):
    """Every refresh start should have a terminal event, including exceptions.

    Without this event, production monitoring sees ``data_refresh_started`` and
    cannot distinguish a still-running refresh from one that already failed.
    """
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(
        app,
        "refresh_universe_files",
        lambda: (_ for _ in ()).throw(
            RuntimeError("token=UNIVERSESECRET should stay hidden")
        ),
    )

    with caplog.at_level("INFO"):
        app.prefetch_data_assets()

    completed = [
        getattr(record, "structured_fields", {})
        for record in caplog.records
        if getattr(record, "event", None) == EVENT_DATA_REFRESH_COMPLETED
    ]
    assert len(completed) == 1
    assert completed[0]["status"] == "failed"
    assert completed[0]["phase"] == "universe_refresh"
    assert completed[0]["error_type"] == "RuntimeError"
    assert "UNIVERSESECRET" not in str(completed[0])


def test_main_requires_auth_before_discovering_screeners(monkeypatch):
    """The main app must not touch the DB or screeners before auth succeeds."""

    class StopFromAuth(RuntimeError):
        """Test-only signal that the auth gate stopped the Streamlit run."""

        pass

    def stop_at_auth(_st):
        # A real Streamlit stop would end this script run. Raising lets pytest
        # assert that the run stopped before `discover_screeners()` was called.
        raise StopFromAuth()

    def fail_if_discovered():
        raise AssertionError("screener discovery must wait for authentication")

    def fail_if_schema_bootstrapped():
        raise AssertionError("schema bootstrap must wait for authentication")

    monkeypatch.setattr(app, "require_authorized_user", stop_at_auth)
    monkeypatch.setattr(app, "discover_screeners", fail_if_discovered)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", fail_if_schema_bootstrapped)
    # Local development defaults AUTH_REQUIRED to false. This test is about the
    # guarded path, so opt in explicitly and then assert discovery never runs
    # before the auth gate stops the Streamlit rerun.
    monkeypatch.setenv("AUTH_REQUIRED", "true")
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


def test_main_bootstraps_schema_after_auth_and_before_view_selection(monkeypatch):
    """An authorized run applies migrations before rendering either app view."""

    class StopAtView(RuntimeError):
        """Test-only signal that execution reached the view selector."""

        pass

    calls: list[str] = []

    def record_schema_bootstrap() -> bool:
        calls.append("schema")
        return True

    def authenticate(_st):
        calls.append("auth")
        return SimpleNamespace(email="person@example.com")

    def stop_at_view(*_args, **_kwargs):
        calls.append("view")
        raise StopAtView()

    monkeypatch.setattr(app, "ensure_database_schema", record_schema_bootstrap)
    monkeypatch.setattr(app, "require_authorized_user", authenticate)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setenv("AUTH_REQUIRED", "true")
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            radio=stop_at_view,
        ),
    )

    with pytest.raises(StopAtView):
        app.main()

    assert calls == ["auth", "schema", "view"]


def test_admin_health_view_is_available_and_returns_before_screener_discovery(
    monkeypatch,
):
    """Admins can inspect health even when a screener module is broken."""

    calls: list[str] = []

    def choose_admin_health(_label, options, **_kwargs):
        assert options == ("Scanner", "Scan history", "Admin health")
        calls.append("view")
        return "Admin health"

    monkeypatch.setattr(
        app,
        "get_settings",
        lambda: get_settings(env={"AUTH_REQUIRED": "true"}),
    )
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", lambda: calls.append("schema"))
    monkeypatch.setattr(
        app,
        "require_authorized_user",
        lambda _st: app.AuthenticatedUser(
            email="admin@example.com",
            name="Admin",
            is_admin=True,
        ),
    )
    monkeypatch.setattr(
        app,
        "_render_admin_health_page",
        lambda user: calls.append(f"health:{user.email}"),
    )
    monkeypatch.setattr(
        app,
        "discover_screeners",
        lambda: (_ for _ in ()).throw(
            AssertionError("health view must return before screener discovery")
        ),
    )
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            radio=choose_admin_health,
        ),
    )

    app.main()

    assert calls == ["schema", "view", "health:admin@example.com"]


def test_non_admin_cannot_select_admin_health(monkeypatch):
    """The main selector must not advertise the admin-only operational page."""

    class StopAtDiscovery(RuntimeError):
        """Signal that the normal scanner path continued after view selection."""

    def choose_scanner(_label, options, **_kwargs):
        assert options == ("Scanner", "Scan history")
        return "Scanner"

    monkeypatch.setattr(
        app,
        "get_settings",
        lambda: get_settings(env={"AUTH_REQUIRED": "true"}),
    )
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(
        app,
        "require_authorized_user",
        lambda _st: app.AuthenticatedUser(
            email="person@example.com",
            name="Person",
            is_admin=False,
        ),
    )
    monkeypatch.setattr(
        app,
        "discover_screeners",
        lambda: (_ for _ in ()).throw(StopAtDiscovery()),
    )
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            radio=choose_scanner,
        ),
    )

    with pytest.raises(StopAtDiscovery):
        app.main()


def test_main_passes_authenticated_email_as_scan_trigger(monkeypatch):
    """Authenticated UI scans should persist who triggered the run.

    Beginner note:
    This test does not run a real screener. It replaces ``_execute_screener``
    with a tiny fake whose only job is to capture the keyword argument that
    ``main()`` passes down. That keeps the test focused on auth-to-audit wiring
    instead of data loading, charts, Dhan credentials, or Streamlit rendering.
    """
    settings = get_settings(env={"AUTH_REQUIRED": "true"})
    selected = ScreenerDefinition(
        key="demo",
        name="Demo",
        description="Test-only screener",
        universe="demo_universe",
        timeframe="daily",
        lookback_days=30,
        default_params={},
        module_name="screeners.demo",
        run=lambda *_args, **_kwargs: pd.DataFrame(),
    )
    captured: dict[str, str] = {}

    def fake_execute(_selected, *, triggered_by):
        # The keyword-only signature is intentional. If main() ever calls
        # _execute_screener(selected) without the audit label again, pytest fails
        # with a TypeError before this fake can quietly accept the mistake.
        captured["triggered_by"] = triggered_by
        return {
            "screener_key": selected.key,
            "results": pd.DataFrame(),
            "failures": [],
            "compute_failures": [],
            "stats": {},
            "universe_df": pd.DataFrame(),
            "params_for_chart": {},
            "data_loader": object(),
            "run_id": 123,
            "status": "success",
        }

    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(
        app,
        "require_authorized_user",
        # The mixed-case email proves app.main() normalizes through _scan_trigger
        # before the value reaches run_scan/persistence.
        lambda _st: SimpleNamespace(email="Sunny@Example.COM"),
    )
    monkeypatch.setattr(app, "discover_screeners", lambda: {selected.key: selected})
    monkeypatch.setattr(app, "_render_sidebar", lambda _screeners: selected)
    monkeypatch.setattr(app, "show_status_panel", lambda _selected: None)
    monkeypatch.setattr(app, "render_universe_table", lambda: None)
    monkeypatch.setattr(app, "_execute_screener", fake_execute)
    monkeypatch.setattr(app, "_render_scan_output", lambda _selected, _cache: None)
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            session_state={"pending_run": True},
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            subheader=lambda *_args, **_kwargs: None,
            write=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            # SCAN-004 added a view switcher; staying on "Scanner" keeps this
            # test focused on the scan-trigger wiring.
            radio=lambda *_args, **_kwargs: "Scanner",
            error=lambda message: (_ for _ in ()).throw(AssertionError(message)),
        ),
    )

    app.main()

    assert captured["triggered_by"] == "ui:sunny@example.com"


def test_main_stops_before_runtime_dirs_when_production_settings_are_invalid(monkeypatch):
    """Misconfigured production should fail before local fallback folders appear."""
    errors: list[str] = []
    # APP_ENV=production with no other settings is intentionally invalid. It
    # should produce a clear settings error before any side-effectful startup
    # work, such as creating local data/ folders or discovering screeners.
    settings = get_settings(env={"APP_ENV": "production"})

    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(
        app,
        "ensure_project_dirs",
        lambda: (_ for _ in ()).throw(
            AssertionError("runtime dirs should not be created")
        ),
    )
    monkeypatch.setattr(
        app,
        "discover_screeners",
        lambda: (_ for _ in ()).throw(AssertionError("screeners should not load")),
    )
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(error=lambda message: errors.append(message)),
    )

    app.main()

    assert errors
    assert "Runtime configuration error" in errors[0]
    assert "DATABASE_URL" in errors[0]
    assert "DATA_DIR" in errors[0]


def test_main_skips_auth_gate_when_auth_not_required(monkeypatch):
    """Development default (AUTH_REQUIRED=false) should skip the auth gate.

    DEPLOY-004 changed main() from always gating to ``if settings.auth_required``.
    This locks the dev-skips-auth branch: the gate is not called, yet the run still
    proceeds to screener discovery. Production safety is covered separately by the
    settings validation tests (AUTH_REQUIRED cannot be false in production).
    """

    class _StopAtDiscovery(RuntimeError):
        """Test-only signal that main() reached discovery without the auth gate."""

    auth_calls: list[int] = []
    # get_settings(env={}) yields development defaults: auth_required is False and
    # is_production is False, so production validation passes and the gate is skipped.
    settings = get_settings(env={})

    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(app, "require_authorized_user", lambda _st: auth_calls.append(1))
    monkeypatch.setattr(
        app,
        "discover_screeners",
        lambda: (_ for _ in ()).throw(_StopAtDiscovery()),
    )
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            # SCAN-004 added a view switcher; staying on "Scanner" lets main()
            # continue to the discovery call this test watches for.
            radio=lambda *_args, **_kwargs: "Scanner",
        ),
    )

    with pytest.raises(_StopAtDiscovery):
        app.main()

    assert auth_calls == []


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


def test_app_reexports_helpers_from_extracted_ui_modules():
    """Legacy ``app.<helper>`` imports should still reach the moved functions.

    REF-001 moves implementation into small ``ui`` modules, but existing tests
    and callers still import these helpers from ``app``. Identity checks make
    that compatibility promise explicit instead of merely proving both names
    happen to behave similarly today.
    """
    assert app._csv_safe is common._csv_safe
    assert app._redact_secrets is common._redact_secrets
    assert app._get_or_build_chart_payload is chart_cache._get_or_build_chart_payload
    assert app._render_history_page is history_page._render_history_page
    assert app._render_admin_health_page is health_page._render_admin_health_page


def test_refresh_universes_clears_every_derived_cache_after_success(monkeypatch):
    """A completed universe refresh must invalidate every dependent cache."""
    clear_calls: list[str] = []
    written = {"nifty_100": Path("data") / "nifty_100.csv"}

    monkeypatch.setattr(app, "refresh_universe_files", lambda: written)
    for name in (
        "_universe_mtime",
        "_cached_universe_status",
        "_cached_all_universe_statuses",
        "_eligible_symbols_set",
    ):
        monkeypatch.setattr(
            app,
            name,
            SimpleNamespace(clear=lambda cache_name=name: clear_calls.append(cache_name)),
        )

    assert app.refresh_universes_and_invalidate() == written
    assert clear_calls == [
        "_universe_mtime",
        "_cached_universe_status",
        "_cached_all_universe_statuses",
        "_eligible_symbols_set",
    ]


def test_refresh_universes_keeps_caches_when_refresh_fails(monkeypatch):
    """A failed refresh must not discard still-usable cached universe state."""
    clear_calls: list[str] = []

    def fail_refresh():
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(app, "refresh_universe_files", fail_refresh)
    for name in (
        "_universe_mtime",
        "_cached_universe_status",
        "_cached_all_universe_statuses",
        "_eligible_symbols_set",
    ):
        monkeypatch.setattr(
            app,
            name,
            SimpleNamespace(clear=lambda cache_name=name: clear_calls.append(cache_name)),
        )

    with pytest.raises(RuntimeError, match="refresh failed"):
        app.refresh_universes_and_invalidate()

    assert clear_calls == []


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
    monkeypatch.setattr(chart_cache, "st", SimpleNamespace(session_state={}))
    monkeypatch.setattr(chart_cache, "render_chart_html", lambda spec: f"<html>{spec['title']}</html>")

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

    # A changed chart parameter must also rebuild: the user edited a sidebar
    # value and expects the chart to reflect it, not a stale cached render.
    fourth = app._get_or_build_chart_payload(selected, "DEMO", "1", loader, {"period": 50})

    assert fourth is not None
    assert fourth.from_cache is False
    assert "demo-50" in fourth.html
    assert build_calls == 3


def test_chart_payload_cache_rejects_other_schema_versions(monkeypatch, tmp_path):
    """A payload cached by an older build must rebuild, not half-deserialize."""
    chart_file = tmp_path / "DEMO_1.parquet"
    chart_file.write_bytes(b"candles")

    class FakeLoader:
        def cache_path(self, symbol, security_id):
            return chart_file

        def read_cached_history(self, symbol, security_id):
            return pd.DataFrame(
                {
                    "timestamp": [pd.Timestamp("2026-01-01")],
                    "open": [10.0],
                    "high": [11.0],
                    "low": [9.0],
                    "close": [10.5],
                }
            )

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
        build_chart=lambda candles, params: {"title": "demo", "height": 321, "panes": []},
    )
    session_state: dict = {}
    monkeypatch.setattr(chart_cache, "st", SimpleNamespace(session_state=session_state))
    monkeypatch.setattr(chart_cache, "render_chart_html", lambda spec: "<html>fresh</html>")

    # Seed the session cache with a pre-versioning payload under the real key.
    cache_key = chart_cache._chart_html_cache_key(selected, "DEMO", "1", FakeLoader(), {"period": 20})
    session_state[chart_cache._CHART_HTML_CACHE_STATE_KEY] = {
        cache_key: {"html": "<html>stale-old-shape</html>", "height": 640}
    }

    payload = app._get_or_build_chart_payload(selected, "DEMO", "1", FakeLoader(), {"period": 20})

    assert payload is not None
    assert payload.from_cache is False
    assert payload.html == "<html>fresh</html>"
    # The rebuilt entry is stored with the current schema stamp.
    stored = session_state[chart_cache._CHART_HTML_CACHE_STATE_KEY][cache_key]
    assert stored["schema"] == chart_cache._CHART_PAYLOAD_SCHEMA
