"""IPO-011 dispatch regression tests for event-driven screeners.

Beginner note:
``show_status_panel`` renders a candle-data health card -- Dhan credentials,
the universe CSV's symbol count and mtime, the daily cache size -- and its
universe lookup indexes ``UNIVERSE_CONFIG`` directly. A screener whose
``universe`` is only a display label therefore crashed the whole page with
``KeyError`` before the user could press anything. These tests pin both sides
of the fix: the card is skipped for an event-driven screener and still renders
for every ordinary one.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pandas as pd

import app
from backend.auth.roles import Role
from backend.config.settings import get_settings
from backend.screener_registry import ScreenerDefinition


def _definition(**overrides: Any) -> ScreenerDefinition:
    """Build a screener definition; scenarios override what they exercise."""
    values: dict[str, Any] = {
        "key": "demo",
        "name": "Demo",
        "description": "Test-only screener",
        "universe": "demo_universe",
        "timeframe": "daily",
        "lookback_days": 30,
        "default_params": {},
        "module_name": "screeners.demo",
        "run": lambda *_args, **_kwargs: pd.DataFrame(),
    }
    values.update(overrides)
    return ScreenerDefinition(**values)


def _install_main_fakes(monkeypatch, selected: ScreenerDefinition) -> None:
    """Stub everything ``main()`` touches before the status-panel block."""
    settings = get_settings(env={"AUTH_REQUIRED": "false"})
    monkeypatch.setattr(app, "get_settings", lambda: settings)
    monkeypatch.setattr(app, "ensure_project_dirs", lambda: None)
    monkeypatch.setattr(app, "_configure_logging", lambda: None)
    monkeypatch.setattr(app, "ensure_database_schema", lambda: True)
    monkeypatch.setattr(
        app,
        "require_authorized_user",
        lambda _st: SimpleNamespace(email="analyst@example.com", role=Role.ANALYST),
    )
    monkeypatch.setattr(app, "discover_screeners", lambda: {selected.key: selected})
    monkeypatch.setattr(app, "_render_sidebar", lambda _screeners, **_kwargs: selected)
    monkeypatch.setattr(
        app,
        "st",
        SimpleNamespace(
            session_state={},
            set_page_config=lambda **_kwargs: None,
            markdown=lambda *_args, **_kwargs: None,
            title=lambda *_args, **_kwargs: None,
            caption=lambda *_args, **_kwargs: None,
            subheader=lambda *_args, **_kwargs: None,
            write=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            radio=lambda *_args, **_kwargs: "Scanner",
            error=lambda message: (_ for _ in ()).throw(AssertionError(message)),
        ),
    )


def test_event_driven_screener_skips_the_candle_status_panels(monkeypatch) -> None:
    """Selecting the IPO screener must not touch the candle universe at all."""
    selected = _definition(
        key="ipo_screener",
        name="IPO Screener",
        universe="ipo_filings",
        timeframe="event-driven",
        lookback_days=0,
        module_name="screeners.ipo_screener",
        requires_candles=False,
    )

    def _must_not_be_called(*_args: Any, **_kwargs: Any) -> None:
        """Fail loudly if a candle-only status panel renders."""
        raise AssertionError("candle status panels must be skipped")

    _install_main_fakes(monkeypatch, selected)
    monkeypatch.setattr(app, "show_status_panel", _must_not_be_called)
    monkeypatch.setattr(app, "render_universe_table", _must_not_be_called)

    app.main()


def test_ordinary_screener_still_renders_the_candle_status_panels(monkeypatch) -> None:
    """The skip is scoped to event-driven screeners and nothing else."""
    selected = _definition()
    rendered: list[str] = []

    _install_main_fakes(monkeypatch, selected)
    monkeypatch.setattr(
        app, "show_status_panel", lambda _selected: rendered.append("status")
    )
    monkeypatch.setattr(
        app, "render_universe_table", lambda: rendered.append("universe")
    )

    app.main()

    assert rendered == ["status", "universe"]
