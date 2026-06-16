"""Focused tests for the admin health page's security and display behavior."""

from __future__ import annotations

import datetime as dt

import app
from backend.auth.session import AuthenticatedUser
from backend.health import (
    AdminHealthSnapshot,
    DataQualityFindingHealth,
    DataQualityRunHealth,
    ScanRunHealth,
    ServiceHealth,
)

# The health renderer lives in ui.health_page (REF-001); app re-exports it.
# Streamlit fakes must be patched onto the module the renderer actually reads.
from ui import health_page


class _FakeColumn:
    """Record Streamlit metric calls without launching a browser."""

    def __init__(self, metrics):
        self.metrics = metrics

    def metric(self, label, value, **_kwargs):
        self.metrics.append((label, value))

    def caption(self, text, **_kwargs):
        self.metrics.append(("caption", str(text)))


class _FakeStreamlit:
    """Minimal Streamlit surface used by the health renderer tests."""

    def __init__(self):
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.captions: list[str] = []
        self.metrics: list[tuple[str, object]] = []
        self.dataframes: list[object] = []

    def subheader(self, *_args, **_kwargs):
        pass

    def caption(self, text, **_kwargs):
        self.captions.append(str(text))

    def columns(self, count):
        return [_FakeColumn(self.metrics) for _ in range(count)]

    def markdown(self, *_args, **_kwargs):
        pass

    def dataframe(self, data, *_args, **_kwargs):
        self.dataframes.append(data)

    def error(self, text, **_kwargs):
        self.errors.append(str(text))

    def warning(self, text, **_kwargs):
        self.warnings.append(str(text))

    def info(self, *_args, **_kwargs):
        pass


def _quality_run() -> DataQualityRunHealth:
    return DataQualityRunHealth(
        run_id=21,
        started_at=dt.datetime(2026, 6, 10, 9, 0, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 6, 10, 9, 2, tzinfo=dt.UTC),
        screener_key="envelope",
        universe_key="nifty_500",
        checked_symbols=2,
        usable_symbols=1,
        warning_symbols=1,
        fatal_symbols=1,
        findings=(
            DataQualityFindingHealth(
                symbol="WIPRO",
                severity="fatal",
                code="HIGH_BELOW_LOW",
                message="token=quality-secret",
                affected_rows=1,
                latest_date=dt.date(2026, 6, 1),
            ),
        ),
    )


def _snapshot(
    *, failure_message: str | None = None, quality_run: DataQualityRunHealth | None = None
) -> AdminHealthSnapshot:
    """Return a compact deterministic snapshot for renderer tests."""
    failed = (
        ScanRunHealth(
            run_id=8,
            started_at=dt.datetime(2026, 6, 11, 9, 0, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 6, 11, 9, 1, tzinfo=dt.UTC),
            screener_key="demo",
            universe_key="nifty_100",
            symbols_scanned=100,
            triggered_by="job:daily",
            error_message=failure_message,
        )
        if failure_message
        else None
    )
    return AdminHealthSnapshot(
        last_successful_scan=None,
        last_failed_scan=failed,
        last_data_refresh=None,
        cached_symbol_count=0,
        latest_candle_date=None,
        unreadable_cache_file_count=0,
        cache_size_bytes=0,
        data_size_bytes=0,
        disk_free_bytes=1024,
        latest_data_quality_run=quality_run,
        services=(
            ServiceHealth("Database", "ready", "Scan-history queries succeeded."),
        ),
    )


def test_health_renderer_rejects_non_admin_without_collecting(monkeypatch):
    """The renderer is a second guard even if orchestration is called directly."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(health_page, "st", fake_st)

    app._render_admin_health_page(
        AuthenticatedUser("person@example.com", "Person", is_admin=False),
        snapshot_loader=lambda: (_ for _ in ()).throw(
            AssertionError("health data must not load for non-admins")
        ),
    )

    assert fake_st.errors == ["Admin access is required to view operational health."]


def test_health_scan_metric_uses_short_run_label():
    """Long screener keys belong in wrapping context, not the metric headline."""
    run = _snapshot(failure_message="failed").last_failed_scan

    assert run is not None
    assert app._format_health_scan(run) == "Run #8"
    assert app._health_scan_context(run) == "demo · nifty_100"


def test_health_renderer_rejects_auth_disabled_session(monkeypatch):
    """A local auth bypass must not accidentally expose the admin page."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(health_page, "st", fake_st)

    app._render_admin_health_page(
        None,
        snapshot_loader=lambda: (_ for _ in ()).throw(
            AssertionError("health data must not load without an admin identity")
        ),
    )

    assert fake_st.errors == ["Admin access is required to view operational health."]


def test_health_renderer_exposes_only_snapshot_exception_type(monkeypatch):
    """Unexpected collector errors must not reveal paths, URLs, or credentials."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(health_page, "st", fake_st)

    app._render_admin_health_page(
        AuthenticatedUser("admin@example.com", "Admin", is_admin=True),
        snapshot_loader=lambda: (_ for _ in ()).throw(
            RuntimeError("postgresql://admin:secret@db.example/scanner")
        ),
    )

    assert fake_st.errors == ["Could not collect health snapshot (RuntimeError)."]
    assert "secret" not in " ".join(fake_st.errors)
    assert "db.example" not in " ".join(fake_st.errors)


def test_health_renderer_redacts_stored_failure_text(monkeypatch):
    """Persisted scan errors can contain credentials and must be redacted."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(health_page, "st", fake_st)
    monkeypatch.setenv("DHAN_ACCESS_TOKEN", "broker-secret")

    app._render_admin_health_page(
        AuthenticatedUser("admin@example.com", "Admin", is_admin=True),
        snapshot_loader=lambda: _snapshot(
            failure_message="Dhan failed with broker-secret"
        ),
    )

    rendered = " ".join([*fake_st.errors, *fake_st.warnings, *fake_st.captions])
    assert "broker-secret" not in rendered
    assert "***REDACTED***" in rendered


def test_health_renderer_shows_latest_data_quality_summary(monkeypatch):
    """Admins should see the newest persisted data-quality receipt."""
    fake_st = _FakeStreamlit()
    monkeypatch.setattr(health_page, "st", fake_st)

    app._render_admin_health_page(
        AuthenticatedUser("admin@example.com", "Admin", is_admin=True),
        snapshot_loader=lambda: _snapshot(quality_run=_quality_run()),
    )

    metrics = dict(fake_st.metrics)
    assert metrics["Quality checked symbols"] == 2
    assert metrics["Quality usable symbols"] == 1
    assert metrics["Quality bad/stale symbols"] == 2
    rendered = " ".join([*fake_st.errors, *fake_st.warnings, *fake_st.captions])
    table_text = " ".join(
        str(row)
        for dataframe in fake_st.dataframes
        for row in dataframe.to_dict("records")
    )
    rendered += table_text
    assert "HIGH_BELOW_LOW" in table_text
    assert "quality-secret" not in rendered
    assert "***REDACTED***" in rendered
