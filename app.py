from __future__ import annotations

# Streamlit entrypoint for the scanner app.
#
# This file deliberately stays focused on UI orchestration:
# - show available screeners,
# - call the selected screener,
# - render the returned table, and
# - render an interactive chart for any shortlisted stock.
#
# Strategy logic lives in `screeners/`; broker / data-fetching logic lives in
# `backend/`. Keeping those concerns out of Streamlit makes every screener
# easier to test later.
#
# Beginner note on how this file gets launched:
# - `python app.py` (from a terminal or IDE Run button) hits the `__main__`
#   block at the bottom of this file. We detect that we are NOT inside a
#   Streamlit ScriptRunContext, run the universe and candle download FIRST in
#   plain Python so the data is on disk, and then re-invoke this same file
#   through Streamlit. The terminal is the only place that sees the download
#   progress, which is exactly what we want.
# - `streamlit run app.py` skips that bootstrap path entirely. The app then
#   trusts whatever data is already in `data/cache/daily/` and warns if the
#   universe CSVs are missing.
#
# IMPORTANT: do not place a top-level triple-quoted string above the imports.
# Streamlit's "magic" feature renders any top-level string expression as
# `st.write(...)` content, which would push the page title below it. Keep
# module documentation in `#` comments here.
import logging
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
from streamlit.runtime.scriptrunner import get_script_run_ctx

from backend.admin import apply_config_overrides
from backend.audit import record_audit_event, record_audit_event_once
from backend.auth.roles import (
    EXPORT_RESULTS,
    MANAGE_IPO_DATA,
    MANAGE_ROLES,
    MODIFY_CONFIG,
    RUN_SCAN,
    VIEW_AUDIT_LOG,
    VIEW_HEALTH,
    Role,
    role_has_capability,
)
from backend.auth.session import (
    AuthenticatedUser,
    auth_secret_values,
    require_authorized_user,
    require_capability,
)
from backend.config import (
    DAILY_CACHE_DIR,
    SettingsError,
    credential_status,
    ensure_project_dirs,
    get_settings,
    validate_production_settings,
)
from backend.daily_data_loader import (
    DEFAULT_HISTORY_YEARS_BACK,
    DailyDataLoader,
    history_start_date,
)
from backend.dhan_client import DhanDataClient
from backend.observability import (
    EVENT_ADMIN_PAGE_ACCESSED,
    EVENT_DATA_REFRESH_COMPLETED,
    EVENT_DATA_REFRESH_STARTED,
    EVENT_LOGIN_SUCCESS,
    EVENT_MANUAL_SCAN_STARTED,
    configure_logging,
    log_event,
)
from backend.scanning import ScanStatus, run_scan
from backend.screener_registry import ScreenerDefinition, ScreenerRegistryError, discover_screeners
from backend.security import install_secret_redaction_filter
from backend.storage import (
    ensure_database_schema,
)
from backend.universe_builder import (
    UNIVERSE_CONFIG,
    refresh_universe_files,
)
from backend.universe_loader import (
    all_universe_statuses,
    load_universe,
    union_of_mapped_universes,
    universe_file_path,
    universe_status,
)

# UI page modules (REF-001). app.py re-exports the moved helpers because the
# test suite (and any external caller) accesses them as `app.<name>`, and
# main() calls the page renderers through these module globals so tests can
# monkeypatch `app._render_history_page` and friends.
from ui.audit_page import _render_audit_log_page
from ui.chart_cache import (  # noqa: F401
    _CHART_HTML_CACHE_LIMIT,
    _CHART_HTML_CACHE_STATE_KEY,
    _chart_file_token,
    _chart_html_cache_key,
    _chart_params_digest,
    _chart_payload_store,
    _ChartRenderPayload,
    _get_or_build_chart_payload,
    _json_cache_default,
    _remember_chart_payload,
    _render_cached_symbol_chart,
)
from ui.common import (  # noqa: F401
    _RATING_BADGES,
    _csv_safe,
    _decimal_column_config,
    _drop_provenance,
    _emoji_rating,
    _escape_cell,
    _redact_secrets,
    _score_components_frame,
    _sort_results_by_final_score,
)
from ui.comparison_page import _render_comparison_page
from ui.config_page import _render_config_page
from ui.fundamentals_panel import (  # noqa: F401
    _FUNDAMENTALS_UNIVERSES,
    _eligible_symbols_set,
    _format_data_freshness,
    _get_fundamental_agent,
    _is_eligible_for_fundamentals,
    _render_fundamentals_panel,
    _render_verdict_block,
)
from ui.health_page import (  # noqa: F401
    _cached_admin_health_snapshot,
    _format_bytes,
    _format_health_scan,
    _format_health_time,
    _health_scan_context,
    _render_admin_health_page,
)
from ui.history_page import (  # noqa: F401
    _HISTORY_ERROR_PREVIEW_CHARS,
    _HISTORY_STATUS_BADGES,
    _as_utc,
    _format_run_duration,
    _format_utc_timestamp,
    _history_filter_kwargs,
    _history_filter_signature,
    _history_result_row,
    _history_run_row,
    _history_runs_frame,
    _render_history_page,
    _render_history_run_details,
)
from ui.ipo_manual_page import _render_ipo_manual_page
from ui.roles_page import _render_roles_page
from ui.scan_view import (  # noqa: F401
    _has_rating_column,
    _render_results_with_chart,
    _render_scan_output,
)
from ui.validation_page import _render_validation_page

_LOCAL_OWNER_EMAIL = "local-owner@localhost"

# The CLI prefetch downloads ten years of daily candles for every stock in the
# union of all universes. The window length is shared with the headless daily job
# via DEFAULT_HISTORY_YEARS_BACK; the actual fetch loop is in
# `prefetch_data_assets()`.
_PREFETCH_YEARS_BACK = DEFAULT_HISTORY_YEARS_BACK


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime helpers
# ---------------------------------------------------------------------------


def running_inside_streamlit() -> bool:
    """Return True when this script is being executed by `streamlit run`.

    Beginner note:
    Streamlit apps are not normal command-line scripts. They need Streamlit's
    runner to create a ScriptRunContext, which is the per-session object used
    by widgets, sidebar state, buttons, and dataframes. If that object is
    missing, every `st.*` call runs in "bare mode" and prints repeated
    "missing ScriptRunContext" warnings instead of opening the browser.
    """
    return get_script_run_ctx(suppress_warning=True) is not None


def _scan_history_start_date(today: date | None = None) -> date:
    """Return the first candle date every screener should receive.

    Screeners still declare `lookback_days` for UI context and indicator-specific
    defaults, but the candle frame passed into `compute_signal(...)` should be
    the full 10-year cache. That lets long-memory checks (major levels, old
    Knoxville retests, ATH drawdowns) reason from the same data the prefetch
    step downloads.

    Beginner note:
    `lookback_days` is not wrong or unused; it still describes how much history
    a strategy usually needs. The actual scan loads the larger shared window so
    a short-lookback screener cannot accidentally hide older events that a chart
    or secondary rule may need.
    """
    selected_date = today or date.today()
    return history_start_date(_PREFETCH_YEARS_BACK, selected_date)


def prefetch_data_assets() -> None:
    """Download universe CSVs AND ~10 years of daily candles before Streamlit boots.

    Flow:
      1. Make sure runtime folders exist.
      2. Refresh the Dhan instrument master + universe CSVs (NIFTY 100, NIFTY 500,
         F&O lists).
      3. Compute the union of all mapped universes so each stock is fetched
         exactly once even when it appears in multiple universes.
      4. Delete any leftover legacy cache files (filenames with date suffixes).
      5. For each stock, call `loader.ensure_daily_history(...)` which either
         downloads the full 10-year window (first time) or appends just the
         missing days since the cache was last refreshed.

    This runs in plain Python BEFORE Streamlit boots, when the user starts the
    app via `python app.py`. The Streamlit UI never blocks on downloads.
    """
    # This helper can be called directly from tests or future scripts, bypassing
    # launch_streamlit_from_plain_python(). Install the filter here as a local
    # safety net so prefetch logs are redacted no matter how this function is
    # entered.
    install_secret_redaction_filter(logging.getLogger())
    ensure_project_dirs()

    # OBS-001/OBS-003: mark the start of a data refresh. The matching
    # data_refresh_completed events below report how it ended (ok / skipped / no
    # credentials). Because this is one of OBS-003's seven durable audit events,
    # try the schema bootstrap before the write; the recorder remains
    # best-effort if the database itself is unavailable.
    ensure_database_schema()
    record_audit_event(event=EVENT_DATA_REFRESH_STARTED, user_email=None)

    print("[prefetch] Refreshing Dhan instrument master and universe CSVs...", flush=True)
    try:
        written = refresh_universes_and_invalidate()
    except Exception as exc:
        # Stale local CSVs may still be usable. We surface the error to the
        # terminal so the user can fix it (often a transient network issue).
        logger.exception("Universe refresh failed during prefetch")
        print(f"[prefetch] WARNING: universe refresh failed: {_redact_secrets(str(exc))}", flush=True)
        log_event(
            logger,
            EVENT_DATA_REFRESH_COMPLETED,
            level=logging.ERROR,
            status="failed",
            phase="universe_refresh",
            error_type=type(exc).__name__,
        )
        return
    for key, path in written.items():
        display_name = UNIVERSE_CONFIG.get(key, {}).get("display_name", key)
        print(f"[prefetch]   {display_name:<25} -> {path}", flush=True)

    # Computing the union AFTER the refresh guarantees we see the freshest
    # mapped rows. If no universes loaded, we still let Streamlit boot.
    union = union_of_mapped_universes()
    if union.empty:
        print("[prefetch] No mapped stocks found in any universe; skipping candle prefetch.", flush=True)
        print("[prefetch] Done. Launching Streamlit UI...", flush=True)
        log_event(
            logger,
            EVENT_DATA_REFRESH_COMPLETED,
            status="no_mapped_stocks",
            universe_files=len(written),
        )
        return
    print(f"[prefetch] Union contains {len(union)} unique mapped stocks.", flush=True)

    # Legacy-cache cleanup is a pure filesystem operation; run it before the
    # Dhan client check so old date-suffixed parquet files get removed even
    # when credentials are missing.
    cleanup_loader = DailyDataLoader(client=None)
    removed = cleanup_loader.cleanup_legacy_cache_files()
    if removed:
        print(f"[prefetch] Removed {removed} legacy date-suffixed cache file(s).", flush=True)

    try:
        loader = DailyDataLoader(DhanDataClient.from_env())
    except Exception as exc:
        # No credentials = no candle prefetch, but the app should still start
        # so the user can fix the .env and rerun.
        logger.exception("Could not build Dhan client for candle prefetch")
        print(
            "[prefetch] WARNING: cannot fetch candles "
            f"({_redact_secrets(str(exc))}). Skipping.",
            flush=True,
        )
        print("[prefetch] Done. Launching Streamlit UI...", flush=True)
        log_event(
            logger,
            EVENT_DATA_REFRESH_COMPLETED,
            status="no_credentials_candles_skipped",
            universe_files=len(written),
            mapped_symbols=len(union),
        )
        return

    total = len(union)
    status_counts: dict[str, int] = {}
    # The loader streams outcomes in input order; with SCANNER_DHAN_FETCH_WORKERS
    # above 1 it overlaps Dhan latency and parquet I/O behind the scenes
    # (PERF-001) while this loop's terminal output stays identical.
    outcomes = loader.iter_ensure_universe_history(
        union.to_dict("records"), years_back=_PREFETCH_YEARS_BACK
    )
    for index, outcome in enumerate(outcomes, start=1):
        if outcome.status == "failed":
            status_counts["failed"] = status_counts.get("failed", 0) + 1
            print(
                f"[prefetch] {index:>4}/{total}  {outcome.symbol:<14}  FAILED  "
                f"{outcome.message or ''}",
                flush=True,
            )
        else:
            status_counts[outcome.status] = status_counts.get(outcome.status, 0) + 1
            print(
                f"[prefetch] {index:>4}/{total}  {outcome.symbol:<14}  {outcome.status}",
                flush=True,
            )

    summary = ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
    print(f"[prefetch] Candle prefetch complete: {summary}.", flush=True)
    print("[prefetch] Done. Launching Streamlit UI...", flush=True)
    # OBS-001: report the candle outcome so a boot-time/scheduled refresh can be
    # monitored. status_counts are counts (downloaded/appended/failed), not secrets.
    log_event(
        logger,
        EVENT_DATA_REFRESH_COMPLETED,
        status="ok",
        mapped_symbols=total,
        candle_status_counts=dict(status_counts),
    )


def launch_streamlit_from_plain_python() -> None:
    """Relaunch this file through Streamlit when someone runs `python app.py`.

    The data prefetch happens FIRST so the terminal shows what was downloaded
    before the Streamlit browser tab opens. Without this handoff, `python
    app.py` would just print Streamlit warnings and never open the browser.
    """
    # OBS-001: configure structured, secret-safe logging for the CLI prefetch
    # window (before `streamlit run` takes over). The same shared setup the
    # Streamlit UI and the headless daily job use, so production gets JSON here too.
    configure_logging()

    prefetch_data_assets()

    from streamlit.web import cli as streamlit_cli

    script_path = str(Path(__file__).resolve())
    sys.argv = ["streamlit", "run", script_path, *sys.argv[1:]]
    raise SystemExit(streamlit_cli.main())


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

# A small, targeted stylesheet. It tweaks documented Streamlit hooks only —
# `.block-container` (the main content wrapper) and the `stMetric*` test-ids —
# so a future Streamlit upgrade would, at worst, need these four rules
# re-checked. Kept deliberately tiny to keep that risk low.
_CUSTOM_CSS = """
<style>
  /* Trim Streamlit's large default top gap so content starts higher. */
  .block-container { padding-top: 2.5rem; padding-bottom: 3rem; }
  /* The status metrics are a secondary health strip — quieten the big numbers. */
  [data-testid="stMetricValue"] { font-size: 1.4rem; }
  [data-testid="stMetricLabel"] { opacity: 0.85; }
  /* Even breathing room around horizontal dividers. */
  hr { margin: 1.1rem 0; }
</style>
"""


def _inject_css() -> None:
    """Apply the app's custom CSS once per page render.

    Called right after `st.set_page_config`. `unsafe_allow_html=True` is
    required for a raw <style> block; the CSS here is a fixed literal string
    (no user input), so there is no injection surface.
    """
    st.markdown(_CUSTOM_CSS, unsafe_allow_html=True)


@st.cache_data(ttl=30, show_spinner=False)
def cache_summary(cache_dir: Path = DAILY_CACHE_DIR) -> dict[str, Any]:
    """Count cached candle files so the UI can show whether caching is active.

    The cache directory can contain hundreds of Parquet files. Streamlit reruns
    the script for ordinary widget interactions, so caching this small summary
    for 30 seconds keeps row clicks and dropdown changes from repeatedly
    walking the filesystem.
    """
    if not cache_dir.exists():
        return {"files": 0, "size_mb": 0.0}

    # Each cached daily-history fetch is stored as one Parquet file. Parquet is
    # compact and preserves pandas dtypes better than plain CSV.
    files = list(cache_dir.glob("*.parquet"))
    size = sum(path.stat().st_size for path in files if path.exists())
    return {"files": len(files), "size_mb": round(size / (1024 * 1024), 2)}


@st.cache_data(ttl=30, show_spinner=False)
def _universe_mtime(universe_key: str) -> str:
    """Return a human-readable last-modified timestamp for a universe CSV.

    This is cached briefly for the same reason as `cache_summary`: the value is
    display-only, and a 30-second delay is a good trade-off for a smoother app
    while a user is interacting with scan results.
    """
    path = universe_file_path(universe_key)
    if not path.exists():
        return "never"
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return modified.strftime("%Y-%m-%d %H:%M")


@st.cache_data(ttl=30, show_spinner=False)
def _cached_universe_status(universe_key: str) -> dict[str, Any]:
    """Return one universe status with a short rerun-friendly cache.

    `universe_status(...)` touches the CSV on disk. Caching the result keeps the
    status strip responsive when a table selection or chart dropdown causes a
    Streamlit rerun.
    """
    return universe_status(universe_key)


@st.cache_data(ttl=30, show_spinner=False)
def _cached_all_universe_statuses() -> tuple[dict[str, object], ...]:
    """Return all universe statuses only when the details table is requested."""
    return tuple(all_universe_statuses())


def show_status_panel(selected: ScreenerDefinition) -> None:
    """Render the health checks a user needs before pressing Run."""
    creds = credential_status()
    universe = _cached_universe_status(selected.universe)
    cache = cache_summary()

    universe_display = UNIVERSE_CONFIG.get(selected.universe, {}).get(
        "display_name", selected.universe
    )
    mapped_rows = int(universe.get("mapped_rows") or 0)

    # Four metrics: credentials, universe identity + count, last refresh time,
    # local cache size. They live inside a bordered container so they read as a
    # quiet "system status" card rather than the loudest thing on the page.
    # Each metric uses Streamlit's delta slot as a short contextual line.
    with st.container(border=True):
        st.caption("System status")
        col1, col2, col3, col4 = st.columns(4)
        col1.metric(
            label="Dhan credentials",
            value="Ready" if creds["ready"] else "Missing",
            delta="signed in" if creds["ready"] else "set Dependencies/.env",
            delta_color="normal" if creds["ready"] else "inverse",
        )
        col2.metric(
            label=f"{universe_display} symbols",
            value=mapped_rows,
            delta=f"{int(universe.get('rows') or 0)} total rows",
            delta_color="off",
        )
        col3.metric(
            label="Universe refreshed",
            value=_universe_mtime(selected.universe),
            delta="local CSV mtime",
            delta_color="off",
        )
        col4.metric(
            label="Daily cache",
            value=int(cache["files"]),
            delta=f"{cache['size_mb']} MB on disk",
            delta_color="off",
        )

    if not creds["ready"]:
        st.warning(
            f"Credentials are not ready. Create `{creds['env_path']}` from "
            "`Dependencies/.env.example`, then run `python Dependencies/dhan_token_setup.py`."
        )

    if not universe["exists"]:
        st.info(
            "Universe CSV is missing. Re-run the app via `python app.py` so the prefetch "
            "step downloads it before opening Streamlit."
        )


def render_universe_table() -> None:
    """Show detailed universe-file status without taking over the main screen."""
    with st.expander("Universe file status", expanded=False):
        show_details = st.toggle(
            "Show details",
            value=False,
            key="show_universe_file_status",
        )
        if not show_details:
            # A collapsed expander still executes in Streamlit. This toggle
            # keeps the expensive "read every universe CSV" step lazy until the
            # user asks for the detailed table.
            return
        statuses = _cached_all_universe_statuses()
        st.dataframe(pd.DataFrame(list(statuses)), width="stretch", hide_index=True)


def refresh_universes_and_invalidate() -> dict[str, Path]:
    """Refresh universe CSVs, then clear every cache that reads them.

    Today the only caller runs in the pre-Streamlit CLI phase (a fresh process,
    so the clears are defensive). Routing refreshes through this wrapper means
    any future in-UI "refresh" action inherits correct cache invalidation
    instead of serving stale universe data for up to a TTL window.
    """
    written = refresh_universe_files()
    _universe_mtime.clear()
    _cached_universe_status.clear()
    _cached_all_universe_statuses.clear()
    _eligible_symbols_set.clear()
    return written


# ---------------------------------------------------------------------------
# Parameter override helpers
#
# Every screener declares `default_params` in its SCREENER dict. The sidebar
# renders one editable widget per default so the user can A/B test parameter
# tweaks (e.g. "what if discount_pct were 5% instead of 14%?") without
# editing source code. Overrides live in `st.session_state` keyed by
# screener+param, so switching screeners does not cross-contaminate values.
# ---------------------------------------------------------------------------


def _param_state_key(screener_key: str, param_key: str) -> str:
    """Stable session_state key for one (screener, parameter) override widget.

    Including both pieces ensures `discount_pct` on screener A does not
    overwrite `discount_pct` on screener B if both define one.
    """
    return f"param_override::{screener_key}::{param_key}"


def _render_parameter_overrides(selected: ScreenerDefinition) -> None:
    """Render an expandable sidebar block to tune the selected screener's params.

    Number-input widgets are bound to `st.session_state` directly via `key=`,
    so reading them back later (in `_apply_param_overrides`) does not need
    any extra plumbing.
    """
    defaults = dict(selected.default_params or {})
    if not defaults:
        # A screener without tunable params (rare) skips the expander entirely.
        return

    with st.expander("Tune parameters", expanded=False):
        st.caption(
            "Values override the screener's defaults for the **next** run. "
            "Click 'Reset to defaults' to discard your edits."
        )

        # The reset button removes any user-set keys so the next widget
        # render falls back to the screener's declared defaults. `st.rerun()`
        # gives the widgets a chance to repaint with the default values
        # immediately rather than waiting for the user's next interaction.
        if st.button(
            "Reset to defaults",
            key=f"reset_params_{selected.key}",
            help="Discard any parameter tweaks and use the screener's declared defaults.",
        ):
            for param_key in defaults:
                state_key = _param_state_key(selected.key, param_key)
                st.session_state.pop(state_key, None)
            st.rerun()

        for param_key, default_value in defaults.items():
            state_key = _param_state_key(selected.key, param_key)
            # Seed the session_state on the first render. Without this seed,
            # the number_input would use `value=default_value` only once and
            # then store its own state, which gets messy on screener switch.
            if state_key not in st.session_state:
                st.session_state[state_key] = default_value

            if isinstance(default_value, bool):
                st.checkbox(param_key, key=state_key)
            elif isinstance(default_value, int):
                # Integer parameters: step=1 keeps the widget arrows
                # incrementing cleanly. The default value (already in state)
                # tells Streamlit it is an int widget.
                st.number_input(param_key, step=1, key=state_key)
            else:
                # Float parameters: 4-decimal format covers percentages like
                # 0.0150 cleanly. The user can still type a wider value.
                st.number_input(param_key, key=state_key, format="%.4f")


def _apply_param_overrides(selected: ScreenerDefinition, params: dict[str, Any]) -> dict[str, Any]:
    """Merge any sidebar-edited values from `st.session_state` into `params`.

    `params` is mutated in place (and also returned) so the caller can chain
    if desired. Only keys declared in the screener's `default_params` are
    pulled — that keeps random session_state values from leaking through.
    """
    for param_key in selected.default_params or {}:
        state_key = _param_state_key(selected.key, param_key)
        if state_key in st.session_state:
            params[param_key] = st.session_state[state_key]
    return params


def _configure_logging() -> None:
    """Set up root logging once per Streamlit session.

    Honors `LOG_LEVEL` (or legacy `SCANNER_DEBUG=1`) so deployments can control
    verbosity without editing code. The default stays at WARNING so
    indicator/screener internals do not flood the terminal. `force=False`
    means we do not stomp on a logger already configured by the CLI prefetch
    path, where `launch_streamlit_from_plain_python` has its own setup.
    """
    # Delegate to the shared OBS-001 setup so the Streamlit UI, the CLI prefetch,
    # and the headless daily job all log identically: structured events, JSON in
    # production, secret-redacted. OIDC cookie/client secrets live in st.secrets
    # (not env settings), so pass them as extra_secrets for the redaction filter.
    configure_logging(extra_secrets=auth_secret_values(st))


# ---------------------------------------------------------------------------
# Main Streamlit flow
# ---------------------------------------------------------------------------


def _record_admin_page_access(
    authenticated_user: AuthenticatedUser | None, page: str
) -> None:
    """Record admin_page_accessed once per admin page per session (OBS-003).

    Streamlit reruns the script on every interaction, so without the session
    dedup an admin idling on a page would mint a new audit row each rerun.
    """
    email = authenticated_user.email if authenticated_user is not None else None
    record_audit_event_once(
        session_state=st.session_state,
        dedup_key=f"_audit_admin_page:{page}",
        event=EVENT_ADMIN_PAGE_ACCESSED,
        user_email=email,
        metadata={"page": page},
    )


def main() -> None:
    """Run the Streamlit app after validating runtime settings.

    Beginner note:
    Streamlit reruns this function from top to bottom for each browser session
    and widget interaction. DEPLOY-004 validation happens first so a production
    misconfiguration stops before we create local folders, discover screeners, or
    expose any scanner UI.
    """
    try:
        settings = validate_production_settings(get_settings())
    except SettingsError as exc:
        st.error(f"Runtime configuration error: {_redact_secrets(str(exc))}")
        return

    # Create safe runtime folders only after production settings validate. That
    # way a misconfigured deployment fails clearly instead of quietly creating a
    # local fallback data directory.
    ensure_project_dirs()

    # Root logger setup happens before any screener code runs so per-symbol
    # warnings inside BaseScanner.run() reach the terminal (or DEBUG logs in
    # SCANNER_DEBUG=1 mode).
    _configure_logging()

    st.set_page_config(page_title="Streamlit Scanner App", page_icon="📈", layout="wide")
    _inject_css()
    st.title("Streamlit Scanner App")
    st.caption(
        "Pluggable daily-candle scanner for Indian equities. "
        "Pick a screener and run — ten years of candles are already cached locally."
    )
    # Beginner note:
    # Streamlit reruns this file from top to bottom for every browser session.
    # Keeping the auth gate here means unauthenticated OR unauthorized users stop
    # before screener discovery, Dhan credential checks, cached scan state,
    # charts, or CSV downloads are even reached. Local development may opt out
    # through AUTH_REQUIRED=false; production validation above prevents that
    # unsafe setting in deployed environments.
    # Keep the authenticated identity in a small local variable instead of
    # reaching back into Streamlit later. Streamlit's auth object is UI/session
    # state; the scan service only needs a plain audit string like "ui" or
    # "ui:person@example.com".
    authenticated_user: AuthenticatedUser
    if settings.auth_required:
        authenticated_user = require_authorized_user(st)
    else:
        # Production validation forbids disabling auth. In local development,
        # use one explicit synthetic identity so admin pages and audit entries
        # agree with AUTH-003's documented full-access owner model.
        authenticated_user = AuthenticatedUser(
            email=_LOCAL_OWNER_EMAIL,
            name="Local development owner",
            role=Role.ADMIN,
        )

    # AUTH-003: the effective role drives every capability check below. Auth-off
    # development uses the synthetic local owner above, so admin pages remain
    # reachable and every audit row still has an explicit actor.
    current_role = authenticated_user.role
    current_email = authenticated_user.email

    # Scan history and OBS-003 audit tables need the schema before any durable
    # write. Run this only after the auth gate so an unauthenticated tab still
    # cannot create DB connections or DDL, but before login_success so the first
    # signed-in session is auditable on a fresh database.
    ensure_database_schema()

    if settings.auth_required:
        # OBS-003: record a successful sign-in once per browser session. The
        # once-helper marks the session only after the durable row is written, so
        # a transient DB failure can retry on the next Streamlit rerun.
        record_audit_event_once(
            session_state=st.session_state,
            dedup_key=f"_audit_login:{authenticated_user.email}",
            event=EVENT_LOGIN_SUCCESS,
            user_email=authenticated_user.email,
        )

    # OBS-003: stash the signed-in email so export handlers in the Scanner and
    # Scan history views (which do not receive the user object) can attribute a
    # download. Auth-off development records the synthetic local-owner identity.
    st.session_state["_audit_user_email"] = authenticated_user.email

    # OBS-003: replay admin-set runtime overrides (e.g. LOG_LEVEL) now that the
    # schema exists and we are past the auth gate, then refresh logging so a
    # changed level/format takes effect on this run. get_settings() reads
    # os.environ live, so the override is picked up everywhere after this point.
    apply_config_overrides()
    _configure_logging()

    # SCAN-004: one radio switches between the live scanner and the history
    # audit view. It sits after the auth gate (so history inherits the same
    # protection) and before screener discovery on purpose: a broken screener
    # file must never prevent an operator from inspecting past runs.
    # "Validation / Signal Performance" is a read-only analytical view (like Scan
    # history) available to every authenticated user, not an admin-only page.
    view_options = [
        "Scanner",
        "Scan history",
        "Scan comparison",
        "Validation / Signal Performance",
    ]
    if role_has_capability(current_role, VIEW_HEALTH):
        # AUTH-003: the admin tier sees the operate-the-system pages — health, the
        # runtime settings form, the IPO manual-extraction form, the audit log viewer,
        # and role management. The menu is gated on VIEW_HEALTH (all admin-only), but
        # each handler below re-checks its own specific capability as the real boundary
        # (e.g. the IPO page requires MANAGE_IPO_DATA).
        view_options.extend(
            [
                "Admin health",
                "Admin settings",
                "Admin IPO extraction",
                "Audit log",
                "Admin roles",
            ]
        )

    view = st.radio(
        "View",
        tuple(view_options),
        horizontal=True,
        label_visibility="collapsed",
        key="main_view",
    )
    if view == "Scan history":
        _render_history_page(
            can_export=role_has_capability(current_role, EXPORT_RESULTS)
        )
        return
    if view == "Scan comparison":
        _render_comparison_page(
            can_export=role_has_capability(current_role, EXPORT_RESULTS)
        )
        return
    if view == "Validation / Signal Performance":
        _render_validation_page(
            can_export=role_has_capability(current_role, EXPORT_RESULTS)
        )
        return
    # AUTH-003 defense in depth: the view list already hides these from non-admins,
    # but the handler re-checks the capability before rendering — a stale rerun or a
    # crafted request cannot reach an admin page the UI never offered.
    if view == "Admin health":
        require_capability(st, role=current_role, capability=VIEW_HEALTH, email=current_email)
        _record_admin_page_access(authenticated_user, "Admin health")
        _render_admin_health_page(authenticated_user)
        return
    if view == "Admin settings":
        require_capability(st, role=current_role, capability=MODIFY_CONFIG, email=current_email)
        _record_admin_page_access(authenticated_user, "Admin settings")
        _render_config_page(authenticated_user)
        return
    if view == "Admin IPO extraction":
        require_capability(
            st,
            role=current_role,
            capability=MANAGE_IPO_DATA,
            email=current_email,
        )
        _record_admin_page_access(authenticated_user, "Admin IPO extraction")
        _render_ipo_manual_page(authenticated_user)
        return
    if view == "Audit log":
        require_capability(st, role=current_role, capability=VIEW_AUDIT_LOG, email=current_email)
        _record_admin_page_access(authenticated_user, "Audit log")
        _render_audit_log_page(authenticated_user)
        return
    if view == "Admin roles":
        require_capability(st, role=current_role, capability=MANAGE_ROLES, email=current_email)
        _record_admin_page_access(authenticated_user, "Admin roles")
        _render_roles_page(authenticated_user)
        return

    try:
        # A screener is just a Python module in `screeners/`. Discovery happens
        # on every Streamlit rerun, so adding a new screener file makes it
        # appear in the UI without editing this file.
        screeners = discover_screeners()
    except ScreenerRegistryError as exc:
        st.error(f"Screener registry error: {_redact_secrets(str(exc))}")
        return

    if not screeners:
        st.error("No screeners were discovered in the screeners folder.")
        return

    selected = _render_sidebar(
        screeners, can_run=role_has_capability(current_role, RUN_SCAN)
    )

    show_status_panel(selected)
    render_universe_table()

    st.subheader(selected.name)
    st.write(selected.description)

    # State machine for scan results across Streamlit reruns:
    # - `pending_run` is set by the sidebar Run button. We consume it once,
    #   execute the screener, and stash the payload in `scan_cache`.
    # - On every other rerun (selectbox change, table row click, etc.) we
    #   simply re-render from `scan_cache` so the user does not lose state.
    # - Switching screeners invalidates the cache via the key mismatch check.
    if st.session_state.pop("pending_run", False):
        # AUTH-003 defense in depth: the Run button is hidden from viewers, but the
        # handler re-checks before executing so a stale pending_run flag cannot run
        # a scan a viewer's UI never offered. Denial logs/audits and stops.
        require_capability(st, role=current_role, capability=RUN_SCAN, email=current_email)
        # OBS-003: the user explicitly pressed Run. Record the manual action
        # (edge-triggered by the button, so no dedup is needed). This is distinct
        # from the service-level scan_started lifecycle event, which also fires
        # for the headless daily job.
        record_audit_event(
            event=EVENT_MANUAL_SCAN_STARTED,
            user_email=authenticated_user.email,
            metadata={"screener_key": selected.key, "universe_key": selected.universe},
        )
        cache = _execute_screener(
            selected,
            triggered_by=_scan_trigger(authenticated_user),
        )
        if cache is not None:
            st.session_state["scan_cache"] = cache

    cache = st.session_state.get("scan_cache")
    if cache is None or cache["screener_key"] != selected.key:
        st.info("Press **Run screener** in the sidebar to scan for matches.", icon="👈")
        return

    _render_scan_output(
        selected, cache, can_export=role_has_capability(current_role, EXPORT_RESULTS)
    )



def _render_sidebar(
    screeners: dict[str, ScreenerDefinition], *, can_run: bool
) -> ScreenerDefinition:
    """Render the sidebar and return the selected screener definition.

    The sidebar is intentionally minimal: data refresh belongs to the CLI
    prefetch step (`python app.py`), and every scan uses the 10-year candle
    window maintained there. The Run button writes flags into `st.session_state`
    so the main flow can detect them on the same rerun.

    ``can_run`` is the AUTH-003 ``RUN_SCAN`` capability: viewers see the screener
    selection (so they can read its description) but not the Run button. The main
    flow re-checks the capability before executing, so hiding the button is UX, not
    the security boundary.
    """
    with st.sidebar:
        st.header("Scanner")

        # Streamlit should display human-friendly screener names, but internally
        # we keep stable machine-friendly keys such as `stochastic_swing`.
        options = {definition.name: key for key, definition in screeners.items()}
        selected_name = st.selectbox(
            "Pick a screener",
            list(options),
            help="Each option corresponds to a Python file under `screeners/`.",
        )
        selected_key = options[selected_name]
        selected = screeners[selected_key]

        universe_display = UNIVERSE_CONFIG.get(selected.universe, {}).get(
            "display_name", selected.universe
        )
        # Compact metadata block. The screener's description is intentionally
        # NOT repeated here — it is shown once, in the main area.
        st.markdown(
            f"**Universe** &nbsp; {universe_display}  \n"
            f"**Timeframe** &nbsp; {selected.timeframe}  \n"
            f"**Lookback** &nbsp; {selected.lookback_days} days"
        )

        # Per-screener parameter overrides. Lives inside the sidebar because
        # tweaking values is the SAME conceptual decision as picking a screener.
        _render_parameter_overrides(selected)

        st.divider()
        # AUTH-003: only analysts and admins may run scans. Viewers see a hint
        # instead of the Run button; the main flow still re-checks the capability.
        if can_run:
            if st.button("Run screener", type="primary", width="stretch"):
                # Invalidate any previously cached scan so the next pass through
                # main() executes the screener fresh.
                st.session_state.pop("scan_cache", None)
                st.session_state["pending_run"] = True
        else:
            st.caption("Your role has read-only access; running scans is disabled.")

    return selected


def _scan_trigger(authenticated_user: AuthenticatedUser | None) -> str:
    """Return the small audit label stored in ``scan_runs.triggered_by``.

    Beginner note:
    Local development often runs with auth disabled, so those scans stay as the
    historical value ``"ui"``. When auth is enabled, storing ``ui:<email>`` lets
    a future history page answer "who started this scan?" without exposing any
    extra identity fields.

    We lower-case the email because email identity comparisons elsewhere in the
    app are case-insensitive. That keeps ``Sunny@Example.COM`` and
    ``sunny@example.com`` from becoming two different audit identities.
    """
    if authenticated_user is None:
        return "ui"

    email = str(authenticated_user.email).strip().lower()
    return f"ui:{email}" if email else "ui"


def _execute_screener(
    selected: ScreenerDefinition,
    *,
    triggered_by: str = "ui",
) -> dict[str, Any] | None:
    """Run the selected screener and return a cache payload.

    Returns `None` when the run aborted before producing results (e.g.,
    missing credentials, universe load failure). The returned dict is stashed
    in `st.session_state["scan_cache"]` so subsequent reruns can re-render
    without re-executing.

    ``triggered_by`` is deliberately passed in instead of discovered here. This
    helper already has plenty of UI work to do (credentials, progress widgets,
    chart params, data loading). Keeping auth/audit formatting in ``main()``
    makes this function easy to call from tests and keeps the persistence layer
    independent from Streamlit's auth object.
    """
    # The UI no longer exposes a manual date range. Every screener receives the
    # same 10-year daily history the CLI prefetch maintains; `lookback_days`
    # remains display/strategy metadata, not a data-loading limit.
    end_date = date.today()
    start_date = _scan_history_start_date(end_date)

    creds = credential_status()
    if not creds["ready"]:
        st.error("Dhan credentials are missing. Set up Dependencies/.env before running.")
        return None

    try:
        # The screener decides which universe it owns. The UI does not ask the
        # user to choose NIFTY 100 vs F&O, because that would let users run a
        # strategy against the wrong stock list by accident.
        universe_df = load_universe(selected.universe)
    except Exception as exc:
        logger.exception("Universe load failed for %s", selected.universe)
        st.error(
            f"Could not load universe `{selected.universe}`: {_redact_secrets(str(exc))}"
        )
        return None

    # Live progress widgets. We build them ONCE before the scan and update them
    # from within the per-symbol callback so the user sees motion immediately.
    progress_bar = st.progress(0.0)
    progress_status = st.empty()

    def progress_callback(completed: int, total: int, symbol: str) -> None:
        if total <= 0:
            progress_bar.progress(1.0)
            return
        fraction = max(0.0, min(1.0, completed / total))
        progress_bar.progress(fraction)
        progress_status.markdown(
            f"Scanning **{symbol}** &mdash; {completed} / {total} symbols processed."
        )

    # `params` carries callbacks into the screener. We keep a separate
    # `params_for_chart` without callbacks so `build_chart` later never
    # receives stale function references from a previous Streamlit rerun.
    params_for_chart: dict[str, Any] = dict(selected.default_params)
    # User overrides (typed into the sidebar's "Tune parameters" expander)
    # take precedence over the screener's declared defaults.
    _apply_param_overrides(selected, params_for_chart)
    params_for_chart.update({"start_date": start_date, "end_date": end_date})
    params: dict[str, Any] = dict(params_for_chart)
    params["progress_callback"] = progress_callback

    try:
        # The data loader handles cache/failure bookkeeping. The scan service
        # (SCAN-003) runs the screener AND persists the run + results; it returns
        # a structured result instead of raising on a screener/DB failure. The
        # triggered_by value is the only auth detail the service sees, which keeps
        # the backend reusable for a future scheduled job that will not have a
        # Streamlit user session.
        data_loader = DailyDataLoader(DhanDataClient.from_env())
        result = run_scan(
            screener_key=selected.key,
            universe_key=selected.universe,
            run_callable=selected.run,
            universe_df=universe_df,
            data_loader=data_loader,
            params=params,
            triggered_by=triggered_by,
        )
    except Exception as exc:
        # Reached only for unexpected setup errors (e.g. building the data loader).
        # Screener and persistence failures are captured inside `result`.
        logger.exception("Screener run failed for %s", selected.key)
        st.error(f"Screener run failed: {_redact_secrets(str(exc))}")
        return None
    finally:
        # Always clear the progress widgets so they do not linger above the
        # results table after success OR failure.
        progress_bar.empty()
        progress_status.empty()

    # A screener that raised before producing rows behaves like before: show the
    # error and skip caching. The FAILED run itself is still recorded by the service.
    if result.status is ScanStatus.FAILED:
        st.error(
            f"Screener run failed: {_redact_secrets(result.error_message or 'unknown error')}"
        )
        return None

    return {
        "screener_key": selected.key,
        "results": result.results,
        "failures": list(data_loader.last_failures),
        "compute_failures": result.compute_failures,
        "stats": {
            "cache_hits": data_loader.last_cache_hits,
            "cache_misses": data_loader.last_cache_misses,
            "api_attempts": data_loader.last_api_attempts,
            "rate_limit_retries": data_loader.last_rate_limit_retries,
        },
        "universe_df": universe_df,
        "params_for_chart": params_for_chart,
        "data_loader": data_loader,
        "run_id": result.run_id,
        "status": result.status.value,
    }



# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


if __name__ == "__main__":
    if running_inside_streamlit():
        # We are already inside `streamlit run`. Just render the app.
        main()
    else:
        # Plain `python app.py`: download data first, THEN start Streamlit.
        launch_streamlit_from_plain_python()
