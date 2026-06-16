"""Admin health page rendering (OBS-002), extracted from app.py (REF-001).

This page intentionally checks local readiness only. Provider status means
"configured/installed", not "a live request succeeded", so opening it never
spends quota or turns a third-party outage into a slow Streamlit rerun.
The passive collection itself lives in ``backend/health.py``; this module owns
only the Streamlit rendering and its small formatting helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pandas as pd
import streamlit as st

from backend.auth.session import AuthenticatedUser
from backend.health import (
    AdminHealthSnapshot,
    DataQualityRunHealth,
    ScanRunHealth,
    collect_admin_health,
)
from ui.common import _redact_secrets


@st.cache_data(ttl=60, show_spinner=False)
def _cached_admin_health_snapshot() -> AdminHealthSnapshot:
    """Collect one health snapshot and reuse it for 60 seconds.

    Streamlit reruns the script for every widget interaction. Without this small
    cache, an operator clicking between views would repeatedly walk hundreds of
    Parquet files and query scan history even though those values change slowly.
    """
    return collect_admin_health()


def _format_health_scan(run: ScanRunHealth | None) -> str:
    """Build a short run label that cannot overflow a three-column metric."""
    if run is None:
        return "No recorded run"
    return f"Run #{run.run_id}"


def _health_scan_context(run: ScanRunHealth | None) -> str | None:
    """Return wrapping screener/universe context for a recorded health run."""
    if run is None:
        return None
    return f"{run.screener_key} · {run.universe_key}"


def _format_health_time(value: datetime | None) -> str:
    """Render an optional UTC timestamp without exposing host locale details."""
    if value is None:
        return "No data"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")


def _format_bytes(value: int | None) -> str:
    """Render byte counts in familiar binary units for the operator."""
    if value is None:
        return "Unavailable"
    amount = float(max(value, 0))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.0f} {unit}" if unit == "B" else f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def _render_data_quality_summary(run: DataQualityRunHealth | None) -> None:
    """Render the newest persisted candle-quality receipt, if one exists."""
    st.markdown("### Candle data quality")
    if run is None:
        st.info("No scan has recorded candle data-quality findings yet.")
        return

    quality_columns = st.columns(3)
    quality_columns[0].metric("Quality checked symbols", run.checked_symbols)
    quality_columns[1].metric("Quality usable symbols", run.usable_symbols)
    quality_columns[2].metric(
        "Quality bad/stale symbols",
        run.warning_symbols + run.fatal_symbols,
    )
    context = (
        f"Run #{run.run_id} · {run.screener_key} · {run.universe_key} · "
        f"{_format_health_time(run.finished_at)}"
    )
    quality_columns[0].caption(context)
    if not run.findings:
        st.caption("Latest quality receipt has no findings.")
        return

    rows = [
        {
            "Symbol": finding.symbol,
            "Severity": finding.severity.upper(),
            "Code": finding.code,
            "Message": _redact_secrets(finding.message),
            "Affected rows": finding.affected_rows,
            "Latest date": finding.latest_date.isoformat() if finding.latest_date else "",
        }
        for finding in run.findings
    ]
    st.dataframe(pd.DataFrame(rows), width="stretch", hide_index=True)
    if run.findings_truncated:
        st.caption(
            f"Showing {len(run.findings)} of {run.total_findings} findings "
            "(fatal first); see logs for the full set."
        )


def _render_admin_health_page(
    authenticated_user: AuthenticatedUser | None,
    *,
    snapshot_loader: Callable[[], AdminHealthSnapshot] = _cached_admin_health_snapshot,
) -> None:
    """Render passive operational health for an explicitly authorized admin.

    The main view selector hides this page from non-admins, but this function
    repeats the authorization check. That second guard protects future callers,
    direct function use, and development sessions where authentication is
    disabled and ``authenticated_user`` is therefore ``None``.
    """
    if authenticated_user is None or not authenticated_user.is_admin:
        st.error("Admin access is required to view operational health.")
        return

    try:
        snapshot = snapshot_loader()
    except Exception as exc:  # noqa: BLE001 - never render a raw health exception.
        st.error(f"Could not collect health snapshot ({type(exc).__name__}).")
        return
    st.subheader("Admin health")
    st.caption(
        "Passive readiness from local scan history, generated data, cache files, "
        "and provider configuration. External services are not contacted."
    )

    st.markdown("### Recent activity")
    recent_columns = st.columns(3)
    recent_columns[0].metric(
        "Last successful scan",
        _format_health_scan(snapshot.last_successful_scan),
        help=(
            _format_health_time(snapshot.last_successful_scan.finished_at)
            if snapshot.last_successful_scan
            else None
        ),
    )
    recent_columns[1].metric(
        "Last failed scan",
        _format_health_scan(snapshot.last_failed_scan),
        help=(
            _format_health_time(snapshot.last_failed_scan.finished_at)
            if snapshot.last_failed_scan
            else None
        ),
    )
    recent_columns[2].metric(
        "Last data refresh",
        _format_health_time(snapshot.last_data_refresh),
    )
    successful_context = _health_scan_context(snapshot.last_successful_scan)
    failed_context = _health_scan_context(snapshot.last_failed_scan)
    if successful_context:
        recent_columns[0].caption(successful_context)
    if failed_context:
        recent_columns[1].caption(failed_context)

    if snapshot.last_failed_scan and snapshot.last_failed_scan.error_message:
        st.warning(
            "Latest failed scan: "
            f"{_redact_secrets(snapshot.last_failed_scan.error_message)}"
        )

    st.markdown("### Data and storage")
    data_columns = st.columns(3)
    data_columns[0].metric("Cached symbols", snapshot.cached_symbol_count)
    data_columns[1].metric(
        "Latest candle date",
        (
            snapshot.latest_candle_date.isoformat()
            if snapshot.latest_candle_date
            else "No candle data"
        ),
    )
    data_columns[2].metric(
        "Unreadable cache files",
        snapshot.unreadable_cache_file_count,
    )
    storage_columns = st.columns(3)
    storage_columns[0].metric("Daily cache size", _format_bytes(snapshot.cache_size_bytes))
    storage_columns[1].metric("All data size", _format_bytes(snapshot.data_size_bytes))
    storage_columns[2].metric("Disk free", _format_bytes(snapshot.disk_free_bytes))
    if snapshot.unreadable_cache_file_count:
        st.warning(
            f"{snapshot.unreadable_cache_file_count} daily cache file(s) could "
            "not be inspected. Refresh the affected cache before relying on its data."
        )

    _render_data_quality_summary(snapshot.latest_data_quality_run)

    st.markdown("### Service readiness")
    service_rows = [
        {
            "Service": service.name,
            "Status": service.status.upper(),
            "Detail": service.detail,
        }
        for service in snapshot.services
    ]
    st.dataframe(
        pd.DataFrame(service_rows),
        width="stretch",
        hide_index=True,
    )
    for service in snapshot.services:
        if service.status == "unavailable":
            st.error(f"{service.name}: {service.detail}")
