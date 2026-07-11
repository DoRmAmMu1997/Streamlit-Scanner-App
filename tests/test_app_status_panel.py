"""Render-path tests for ui/status_panel.py (REF-003).

The system-status panel and its small ``@st.cache_data`` helpers moved out of
app.py with no direct tests. House pattern: monkeypatch
``ui.status_panel.st`` with a recording fake and drive the real render paths;
the real cached helpers (`cache_summary`, `_universe_mtime`) run against
tmp_path with their caches cleared around each test so state cannot leak into
other suites.
"""

from __future__ import annotations

import pandas as pd
import pytest

from backend.screener_registry import ScreenerDefinition
from ui import status_panel


class _FakeColumn:
    def __init__(self, owner: _FakeStreamlit):
        self._owner = owner

    def metric(self, *, label, value, delta=None, delta_color="normal"):
        self._owner.metrics.append(
            {"label": label, "value": value, "delta": delta, "delta_color": delta_color}
        )


class _FakeContainer:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeExpander:
    def __init__(self, owner: _FakeStreamlit, label: str):
        owner.expanders.append(label)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class _FakeStreamlit:
    def __init__(self):
        self.captions: list[str] = []
        self.warnings: list[str] = []
        self.infos: list[str] = []
        self.metrics: list[dict] = []
        self.expanders: list[str] = []
        self.dataframes: list[object] = []
        # Programmed return for the universe-table details toggle.
        self.toggle_value = False

    def container(self, **_kwargs):
        return _FakeContainer()

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def columns(self, spec):
        count = spec if isinstance(spec, int) else len(spec)
        return [_FakeColumn(self) for _ in range(count)]

    def warning(self, text, **_kwargs):
        self.warnings.append(str(text))

    def info(self, text, **_kwargs):
        self.infos.append(str(text))

    def expander(self, label, **_kwargs):
        return _FakeExpander(self, str(label))

    def toggle(self, _label, **_kwargs):
        return self.toggle_value

    def dataframe(self, data, **_kwargs):
        self.dataframes.append(data)


def _definition() -> ScreenerDefinition:
    return ScreenerDefinition(
        key="demo",
        name="Demo screener",
        description="test",
        universe="demo_universe",
        timeframe="1d",
        lookback_days=100,
        default_params={},
        module_name="demo",
        run=lambda **_kwargs: pd.DataFrame(),
    )


@pytest.fixture()
def fake_st(monkeypatch):
    fake = _FakeStreamlit()
    monkeypatch.setattr(status_panel, "st", fake)
    return fake


# ---------------------------------------------------------------------------
# show_status_panel
# ---------------------------------------------------------------------------


def _patch_panel_inputs(monkeypatch, *, ready: bool, exists: bool):
    monkeypatch.setattr(
        status_panel,
        "credential_status",
        lambda: {"ready": ready, "env_path": "Dependencies/.env"},
    )
    monkeypatch.setattr(
        status_panel,
        "_cached_universe_status",
        lambda _key: {"exists": exists, "rows": 120, "mapped_rows": 100},
    )
    monkeypatch.setattr(
        status_panel, "cache_summary", lambda: {"files": 42, "size_mb": 3.5}
    )
    monkeypatch.setattr(status_panel, "_universe_mtime", lambda _key: "2026-07-09 18:00")


def test_status_panel_renders_four_metrics_when_everything_is_ready(fake_st, monkeypatch):
    _patch_panel_inputs(monkeypatch, ready=True, exists=True)

    status_panel.show_status_panel(_definition())

    assert fake_st.captions == ["System status"]
    assert [m["label"] for m in fake_st.metrics] == [
        "Dhan credentials",
        # demo_universe has no UNIVERSE_CONFIG entry, so the key itself shows.
        "demo_universe symbols",
        "Universe refreshed",
        "Daily cache",
    ]
    creds, universe, refreshed, cache = fake_st.metrics
    assert creds["value"] == "Ready" and creds["delta"] == "signed in"
    assert universe["value"] == 100 and universe["delta"] == "120 total rows"
    assert refreshed["value"] == "2026-07-09 18:00"
    assert cache["value"] == 42 and cache["delta"] == "3.5 MB on disk"
    assert fake_st.warnings == []
    assert fake_st.infos == []


def test_status_panel_warns_on_missing_credentials_and_universe(fake_st, monkeypatch):
    _patch_panel_inputs(monkeypatch, ready=False, exists=False)

    status_panel.show_status_panel(_definition())

    creds = fake_st.metrics[0]
    assert creds["value"] == "Missing"
    assert creds["delta_color"] == "inverse"
    assert len(fake_st.warnings) == 1
    assert "Dependencies/.env" in fake_st.warnings[0]
    assert len(fake_st.infos) == 1
    assert "python app.py" in fake_st.infos[0]


# ---------------------------------------------------------------------------
# render_universe_table — details stay lazy until the user opts in
# ---------------------------------------------------------------------------


def test_universe_table_stays_lazy_while_toggle_is_off(fake_st, monkeypatch):
    def fail_if_loaded():
        raise AssertionError("universe statuses should load only after opt-in")

    monkeypatch.setattr(status_panel, "_cached_all_universe_statuses", fail_if_loaded)

    status_panel.render_universe_table()

    assert fake_st.expanders == ["Universe file status"]
    assert fake_st.dataframes == []


def test_universe_table_shows_statuses_after_opt_in(fake_st, monkeypatch):
    statuses = ({"universe": "nifty_100", "rows": 100},)
    monkeypatch.setattr(status_panel, "_cached_all_universe_statuses", lambda: statuses)
    fake_st.toggle_value = True

    status_panel.render_universe_table()

    assert len(fake_st.dataframes) == 1
    frame = fake_st.dataframes[0]
    assert list(frame["universe"]) == ["nifty_100"]


# ---------------------------------------------------------------------------
# cache_summary — the REAL cached helper against tmp_path
# ---------------------------------------------------------------------------


def test_cache_summary_counts_only_parquet_files(tmp_path):
    status_panel.cache_summary.clear()
    try:
        (tmp_path / "AAA.parquet").write_bytes(b"\0" * 524288)
        (tmp_path / "BBB.parquet").write_bytes(b"\0" * 524288)
        (tmp_path / "notes.csv").write_bytes(b"\0" * 524288)  # must not count

        summary = status_panel.cache_summary(tmp_path)

        assert summary == {"files": 2, "size_mb": 1.0}
    finally:
        status_panel.cache_summary.clear()


def test_cache_summary_missing_directory_reports_empty(tmp_path):
    status_panel.cache_summary.clear()
    try:
        assert status_panel.cache_summary(tmp_path / "absent") == {
            "files": 0,
            "size_mb": 0.0,
        }
    finally:
        status_panel.cache_summary.clear()


def test_cache_summary_default_resolves_current_daily_cache_dir(tmp_path, monkeypatch):
    """REF-003 rider: the default must be read per call, not bound at import.

    With the old `cache_dir: Path = DAILY_CACHE_DIR` signature this monkeypatch
    would be invisible — the default was frozen when the module was imported.
    """
    status_panel.cache_summary.clear()
    try:
        (tmp_path / "AAA.parquet").write_bytes(b"\0" * 1024)
        monkeypatch.setattr(status_panel, "DAILY_CACHE_DIR", tmp_path)

        assert status_panel.cache_summary()["files"] == 1
    finally:
        status_panel.cache_summary.clear()


# ---------------------------------------------------------------------------
# _universe_mtime — the REAL cached helper against tmp_path
# ---------------------------------------------------------------------------


def test_universe_mtime_reports_never_for_missing_csv(tmp_path, monkeypatch):
    status_panel._universe_mtime.clear()
    try:
        monkeypatch.setattr(
            status_panel, "universe_file_path", lambda _key: tmp_path / "absent.csv"
        )
        assert status_panel._universe_mtime("demo_universe") == "never"
    finally:
        status_panel._universe_mtime.clear()


def test_universe_mtime_formats_existing_csv_timestamp(tmp_path, monkeypatch):
    status_panel._universe_mtime.clear()
    try:
        csv_path = tmp_path / "demo.csv"
        csv_path.write_text("symbol\n", encoding="utf-8")
        monkeypatch.setattr(status_panel, "universe_file_path", lambda _key: csv_path)

        stamp = status_panel._universe_mtime("demo_universe")

        # `YYYY-MM-DD HH:MM` — exact instant depends on the filesystem clock.
        assert len(stamp) == 16
        assert stamp[4] == "-" and stamp[13] == ":"
    finally:
        status_panel._universe_mtime.clear()


# ---------------------------------------------------------------------------
# _cached_universe_status / _cached_all_universe_statuses delegate untouched
# ---------------------------------------------------------------------------


def test_cached_universe_status_delegates_to_loader(monkeypatch):
    status_panel._cached_universe_status.clear()
    try:
        monkeypatch.setattr(
            status_panel, "universe_status", lambda key: {"universe": key, "exists": True}
        )
        assert status_panel._cached_universe_status("demo_universe") == {
            "universe": "demo_universe",
            "exists": True,
        }
    finally:
        status_panel._cached_universe_status.clear()


def test_cached_all_universe_statuses_returns_tuple(monkeypatch):
    status_panel._cached_all_universe_statuses.clear()
    try:
        monkeypatch.setattr(
            status_panel,
            "all_universe_statuses",
            lambda: [{"universe": "nifty_100"}, {"universe": "fno"}],
        )
        statuses = status_panel._cached_all_universe_statuses()
        assert isinstance(statuses, tuple)
        assert [entry["universe"] for entry in statuses] == ["nifty_100", "fno"]
    finally:
        status_panel._cached_all_universe_statuses.clear()
