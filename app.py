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
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components
from streamlit.runtime.scriptrunner import get_script_run_ctx

from backend.charts import render_chart_html
from backend.config import (
    DAILY_CACHE_DIR,
    credential_status,
    ensure_project_dirs,
    get_dhan_credentials,
    get_fundamentals_model,
)
from backend.fundamentals import (
    AgentVerdict,
    FundamentalAgent,
    FundamentalsUsageLimitError,
)
from backend.daily_data_loader import DailyDataLoader
from backend.dhan_client import DhanDataClient
from backend.screener_registry import ScreenerDefinition, ScreenerRegistryError, discover_screeners
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


# The CLI prefetch downloads ten years of daily candles for every stock in the
# union of all universes. Keeping this constant here makes it easy to find and
# tweak; the actual fetch loop is in `prefetch_data_assets()`.
_PREFETCH_YEARS_BACK = 10


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
    ensure_project_dirs()

    print("[prefetch] Refreshing Dhan instrument master and universe CSVs...", flush=True)
    try:
        written = refresh_universe_files()
    except Exception as exc:
        # Stale local CSVs may still be usable. We surface the error to the
        # terminal so the user can fix it (often a transient network issue).
        logger.exception("Universe refresh failed during prefetch")
        print(f"[prefetch] WARNING: universe refresh failed: {exc}", flush=True)
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
        print(f"[prefetch] WARNING: cannot fetch candles ({exc}). Skipping.", flush=True)
        print("[prefetch] Done. Launching Streamlit UI...", flush=True)
        return

    total = len(union)
    status_counts: dict[str, int] = {}
    for index, row in enumerate(union.to_dict("records"), start=1):
        symbol = str(row.get("symbol", "?")).strip() or "?"
        try:
            _, status = loader.ensure_daily_history(row, years_back=_PREFETCH_YEARS_BACK)
            status_counts[status] = status_counts.get(status, 0) + 1
            print(f"[prefetch] {index:>4}/{total}  {symbol:<14}  {status}", flush=True)
        except Exception as exc:
            logger.exception("Prefetch failed for %s", symbol)
            status_counts["failed"] = status_counts.get("failed", 0) + 1
            print(f"[prefetch] {index:>4}/{total}  {symbol:<14}  FAILED  {exc}", flush=True)

    summary = ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items()))
    print(f"[prefetch] Candle prefetch complete: {summary}.", flush=True)
    print("[prefetch] Done. Launching Streamlit UI...", flush=True)


def launch_streamlit_from_plain_python() -> None:
    """Relaunch this file through Streamlit when someone runs `python app.py`.

    The data prefetch happens FIRST so the terminal shows what was downloaded
    before the Streamlit browser tab opens. Without this handoff, `python
    app.py` would just print Streamlit warnings and never open the browser.
    """
    # Basic logging setup so logger calls inside the prefetch reach the user.
    # Streamlit configures its own handlers later; this only affects the
    # short CLI prefetch window before `streamlit run` takes over.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )

    prefetch_data_assets()

    from streamlit.web import cli as streamlit_cli

    script_path = str(Path(__file__).resolve())
    sys.argv = ["streamlit", "run", script_path, *sys.argv[1:]]
    raise SystemExit(streamlit_cli.main())


# ---------------------------------------------------------------------------
# Security helpers
# ---------------------------------------------------------------------------

# Excel/Sheets treat a cell whose first character is one of these as a formula.
# That makes plain text like `=cmd|...` execute when the CSV is opened in a
# spreadsheet. Prefixing such cells with a single apostrophe makes them inert.
_CSV_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def _csv_safe(df: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of `df` with CSV-formula-injection-safe string cells.

    Numeric / datetime / boolean cells are left untouched; only string cells
    that start with a dangerous prefix are escaped. The transformation is
    idempotent: running it twice does not double-prefix.
    """
    safe = df.copy()
    for column in safe.columns:
        series = safe[column]
        if series.dtype == object:
            safe[column] = series.map(_escape_cell)
    return safe


def _escape_cell(value: Any) -> Any:
    """Prefix a single dangerous string cell with an apostrophe."""
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


def _redact_secrets(text: str) -> str:
    """Strip any loaded credentials from an error message before display.

    Masks the Dhan access token / client code. The Dhan SDK occasionally
    embeds request payloads (including auth headers) in its exception
    messages, so we replace those values with a fixed mask before passing
    text to `st.error(...)`. The Check Fundamentals agent authenticates via
    your Claude subscription (no API key), so there is no LLM key to redact.
    """
    if not isinstance(text, str) or not text:
        return text
    secrets: list[str] = []

    dhan = get_dhan_credentials(required=False)
    if dhan is not None:
        secrets.extend(filter(None, [dhan.access_token, dhan.client_code]))

    if not secrets:
        return text

    redacted = text
    # Replace the longest values first so substring overlaps cannot leak parts
    # of a longer secret after the shorter one is masked.
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            redacted = redacted.replace(secret, "***REDACTED***")
    return redacted


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


def cache_summary(cache_dir: Path = DAILY_CACHE_DIR) -> dict[str, object]:
    """Count cached candle files so the UI can show whether caching is active."""
    if not cache_dir.exists():
        return {"files": 0, "size_mb": 0.0}

    # Each cached daily-history fetch is stored as one Parquet file. Parquet is
    # compact and preserves pandas dtypes better than plain CSV.
    files = list(cache_dir.glob("*.parquet"))
    size = sum(path.stat().st_size for path in files if path.exists())
    return {"files": len(files), "size_mb": round(size / (1024 * 1024), 2)}


def _universe_mtime(universe_key: str) -> str:
    """Return a human-readable last-modified timestamp for a universe CSV."""
    path = universe_file_path(universe_key)
    if not path.exists():
        return "never"
    modified = datetime.fromtimestamp(path.stat().st_mtime)
    return modified.strftime("%Y-%m-%d %H:%M")


def show_status_panel(selected: ScreenerDefinition) -> None:
    """Render the health checks a user needs before pressing Run."""
    creds = credential_status()
    universe = universe_status(selected.universe)
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
            "step downloads it, or use **Refresh universes** in the sidebar."
        )


def render_universe_table() -> None:
    """Show detailed universe-file status without taking over the main screen."""
    with st.expander("Universe file status", expanded=False):
        statuses = all_universe_statuses()
        st.dataframe(pd.DataFrame(statuses), width="stretch", hide_index=True)


# BUY/SELL gets a colored emoji badge. We render a plain DataFrame (not a
# pandas Styler) because Streamlit's row-selection (`selection_mode`) is only
# reliably supported on plain DataFrames — a Styler can silently disable it.
_RATING_BADGES = {"BUY": "🟢 BUY", "SELL": "🔴 SELL"}


def _emoji_rating(results: pd.DataFrame) -> pd.DataFrame:
    """Return a display copy of `results` with BUY/SELL shown as emoji badges.

    Only the `rating` / `signal` columns are touched. Any other value (e.g. the
    connection-test screener's `status` of "ok"/"no_data") is left unchanged.
    The original `results` is never mutated, so the CSV export keeps raw text.
    """
    display = results.copy()
    for column in ("rating", "signal"):
        if column in display.columns:
            # `.map` turns unmapped values into NaN; `.fillna` restores them.
            display[column] = display[column].map(_RATING_BADGES).fillna(display[column])
    return display


def _decimal_column_config(results: pd.DataFrame) -> dict[str, Any]:
    """Build an `st.dataframe` column_config that shows floats to 2 decimals.

    This is display-only formatting (the underlying DataFrame keeps full
    precision) and, unlike a pandas Styler, it works alongside row-selection.
    """
    return {
        column: st.column_config.NumberColumn(format="%.2f")
        for column in results.columns
        if pd.api.types.is_float_dtype(results[column])
    }


def _has_rating_column(results: pd.DataFrame) -> bool:
    """Return True when the results table carries a BUY/SELL-style column."""
    return any(column in results.columns for column in ("rating", "signal"))


# ---------------------------------------------------------------------------
# Check Fundamentals — eligibility, agent caching, UI rendering
#
# The fundamental-analysis agent runs for ANY shortlisted symbol; eligibility
# only selects criteria vs insights-only mode. Two helpers below build that
# eligibility set, and a third lazily instantiates the Claude Agent SDK agent.
# None of the agent code runs unless the user actually clicks the
# "Check Fundamentals" button.
# ---------------------------------------------------------------------------


_FUNDAMENTALS_UNIVERSES: tuple[str, ...] = ("hemant_super_45", "nifty_100")


@st.cache_data(ttl=600)
def _eligible_symbols_set(universe_keys: tuple[str, ...]) -> frozenset[str]:
    """Return the uppercase symbol set across the given universe keys.

    Cached for 10 minutes because universe CSVs are refreshed at most once
    per CLI prefetch run — re-reading on every Streamlit rerun is wasteful.
    """
    symbols: set[str] = set()
    for key in universe_keys:
        try:
            df = load_universe(key)
        except Exception:
            # A missing universe CSV must not break the rest of the UI.
            logger.warning("Could not load universe %s for fundamentals eligibility", key)
            continue
        if "symbol" not in df.columns:
            continue
        for symbol in df["symbol"].astype(str):
            cleaned = symbol.strip().upper()
            if cleaned:
                symbols.add(cleaned)
    return frozenset(symbols)


def _is_eligible_for_fundamentals(symbol: str | None) -> bool:
    """True when `symbol` belongs to Hemant Super 45 OR Nifty 100."""
    if not symbol:
        return False
    return str(symbol).strip().upper() in _eligible_symbols_set(_FUNDAMENTALS_UNIVERSES)


@st.cache_resource(show_spinner=False)
def _get_fundamental_agent(model: str) -> FundamentalAgent:
    """Memoize one agent per model across reruns.

    The Claude Agent SDK authenticates via your Claude subscription, so there
    is no API key argument. `cache_resource` keys on `model`, so switching the
    model rebuilds the agent (and its on-disk cache handle) automatically.
    """
    return FundamentalAgent(model=model)


def _render_fundamentals_panel(symbol: str | None) -> None:
    """Render the per-stock Check Fundamentals section under the chart.

    The button is now visible for ANY selected symbol — eligibility just
    determines which mode the agent runs in:
      - Hemant Super 45 ∪ Nifty 100 symbols → criteria mode (full 7-criteria
        evaluation + observations + outlook + rating).
      - Anything else → insights_only mode (skip the seven criteria,
        produce observations + outlook + rating from screener.in data).

    Stays hidden only when no symbol is selected.
    """
    if not symbol:
        return

    # Mode is symbol-deterministic: HS45/N100 → criteria, everything else
    # → insights_only. The button label and behavior adapt accordingly.
    mode = "criteria" if _is_eligible_for_fundamentals(symbol) else "insights_only"

    st.divider()
    st.subheader("Fundamentals")
    if mode == "criteria":
        st.caption(
            "AI agent applies the seven user-defined criteria, adds its own "
            "expert observations, and produces a holistic 0–10 rating."
        )
    else:
        st.caption(
            f"**Insights-only mode** — `{symbol}` is outside Hemant Super 45 / "
            "Nifty 100, so the seven user-defined criteria are not applied. "
            "The agent still produces a holistic rating, observations, and "
            "forward outlook from screener.in data."
        )

    model = get_fundamentals_model()

    # Session-state cache key is now mode-qualified so a criteria-mode and an
    # insights-only verdict for the same symbol cannot collide.
    session_key = f"fundamentals_verdict::{symbol}::{model}::{mode}"
    cached_verdict_dict = st.session_state.get(session_key)

    button_col, rerun_col, _spacer = st.columns([2, 1, 2])
    primary_label = (
        f"View cached verdict: {symbol}"
        if cached_verdict_dict is not None
        else f"Check Fundamentals: {symbol}"
    )
    run_now = button_col.button(
        primary_label,
        type="primary",
        key=f"check_fund_btn::{symbol}::{model}::{mode}",
        disabled=cached_verdict_dict is not None,
    )
    rerun_now = False
    if cached_verdict_dict is not None:
        rerun_now = rerun_col.button(
            "Re-run analysis",
            key=f"rerun_fund_btn::{symbol}::{model}::{mode}",
            help="Bypass the cache and re-fetch screener.in + re-query the LLM.",
        )

    if run_now or rerun_now:
        try:
            agent = _get_fundamental_agent(model)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Could not build FundamentalAgent")
            st.error(f"Could not build FundamentalAgent: {_redact_secrets(str(exc))}")
            return

        with st.spinner(f"Senior analyst evaluating **{symbol}** — this can take 20–60s..."):
            try:
                verdict = agent.check(symbol, force_refresh=rerun_now, mode=mode)
            except FundamentalsUsageLimitError as exc:
                # Expected operational state (plan limit hit) — show a gentle
                # notice, not a red error, and keep cached verdicts usable.
                logger.warning("Fundamentals usage limit reached for %s: %s", symbol, exc)
                st.warning(f"⏳ {exc}")
                return
            except Exception as exc:  # noqa: BLE001
                logger.exception("Fundamental agent failed for %s", symbol)
                st.error(f"Fundamental check failed: {_redact_secrets(str(exc))}")
                return
        # Persist verdict as plain dict so it survives reruns even after
        # the Pydantic class changes shape.
        st.session_state[session_key] = verdict.model_dump(mode="json")
        cached_verdict_dict = st.session_state[session_key]

    if cached_verdict_dict is None:
        return

    try:
        verdict = AgentVerdict.model_validate(cached_verdict_dict)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Cached verdict for %s is invalid; clearing", symbol, exc_info=True)
        st.session_state.pop(session_key, None)
        st.error(f"Cached verdict could not be parsed: {exc}")
        return

    _render_verdict_block(verdict)


def _render_verdict_block(verdict: AgentVerdict) -> None:
    """Render the rating metric + criteria table + observations + summary.

    Behavior depends on the verdict's mode:
    - "criteria" (HS45 ∪ N100): full output — rating, "criteria passed
      X/Y", criteria-breakdown table, observations, forward outlook,
      summary.
    - "insights_only" (any other stock): rating + observations + forward
      outlook + summary. The "criteria passed" metric and the breakdown
      table are hidden because the seven criteria were not evaluated.
    """
    is_criteria_mode = getattr(verdict, "mode", "criteria") == "criteria"

    # Headline numbers: the criteria-passed metric only appears in criteria mode.
    if is_criteria_mode:
        metric_cols = st.columns([1, 1, 2])
        metric_cols[0].metric(
            "Fundamental rating",
            f"{verdict.rating}/10",
            help="Holistic expert judgment — NOT a count of passed criteria.",
        )
        metric_cols[1].metric(
            "Criteria passed",
            f"{verdict.passed_criteria_count} / {verdict.total_criteria}",
        )
        metric_cols[2].metric(
            "Model",
            verdict.model_used.split("/")[-1] if "/" in verdict.model_used else verdict.model_used,
        )
    else:
        metric_cols = st.columns([1, 2])
        metric_cols[0].metric(
            "Fundamental rating",
            f"{verdict.rating}/10",
            help="Insights-only mode: standalone analyst judgment, no criteria checklist.",
        )
        metric_cols[1].metric(
            "Model",
            verdict.model_used.split("/")[-1] if "/" in verdict.model_used else verdict.model_used,
        )

    # Criteria breakdown table (criteria mode only; hidden when empty)
    breakdown_rows = [
        {
            "Criterion": criterion.name,
            "Pass": "✅" if criterion.passed else "❌",
            "Measured": criterion.measured_value,
            "Threshold": criterion.threshold,
            "Reasoning": criterion.reasoning,
        }
        for criterion in verdict.criteria_breakdown
    ]
    if is_criteria_mode and breakdown_rows:
        st.markdown("**Criteria breakdown**")
        st.dataframe(
            pd.DataFrame(breakdown_rows),
            width="stretch",
            hide_index=True,
        )

    # Additional agent-chosen observations, grouped by sentiment
    if verdict.additional_observations:
        st.markdown("**Additional observations (beyond the seven criteria)**")
        sentiment_order = {"positive": 0, "negative": 1, "neutral": 2}
        sentiment_icon = {"positive": "🟢", "negative": "🔴", "neutral": "⚪"}
        sorted_observations = sorted(
            verdict.additional_observations,
            key=lambda obs: sentiment_order.get(obs.sentiment, 3),
        )
        for observation in sorted_observations:
            icon = sentiment_icon.get(observation.sentiment, "•")
            st.markdown(
                f"- {icon} **{observation.topic}** — {observation.finding}  \n"
                f"  _Evidence:_ {observation.evidence}"
            )

    # Forward outlook (analyst view). Distinct from the criterion-(e) pass/fail —
    # this is the agent's free-form view on the company's next 1–4 quarters,
    # broken into three labelled subsections by source: announcements first,
    # concall second, overall integrated summary third. Subsections that came
    # back empty (e.g. no concall transcript was read) are hidden so the UI
    # never shows an empty bullet.
    outlook = getattr(verdict, "forward_outlook", None)
    if outlook is not None and any(
        section.strip()
        for section in (
            outlook.announcements_conclusion,
            outlook.concall_conclusion,
            outlook.overall_summary,
        )
    ):
        st.markdown("**Forward outlook (analyst view)**")
        if outlook.announcements_conclusion.strip():
            st.markdown(
                f"- **Conclusion from Announcements:** {outlook.announcements_conclusion}"
            )
        if outlook.concall_conclusion.strip():
            st.markdown(
                f"- **Conclusion from the latest Concall:** {outlook.concall_conclusion}"
            )
        if outlook.overall_summary.strip():
            st.markdown(
                f"- **Overall summary:** {outlook.overall_summary}"
            )

    # Summary callout
    st.markdown("**Summary**")
    st.info(verdict.summary_comments)
    st.caption(
        f"Data fetched: `{verdict.data_freshness}` · Model: `{verdict.model_used}`"
    )


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
            for param_key in defaults.keys():
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
    for param_key in (selected.default_params or {}).keys():
        state_key = _param_state_key(selected.key, param_key)
        if state_key in st.session_state:
            params[param_key] = st.session_state[state_key]
    return params


def _configure_logging() -> None:
    """Set up root logging once per Streamlit session.

    Honors `SCANNER_DEBUG=1` for DEBUG output; otherwise stays at WARNING so
    indicator/screener internals do not flood the terminal. `force=False`
    means we do not stomp on a logger already configured by the CLI prefetch
    path, where `launch_streamlit_from_plain_python` has its own setup.
    """
    if logging.getLogger().handlers:
        # Some Python entry point already configured the root logger
        # (e.g. the CLI prefetch). Honor that rather than reconfiguring.
        return
    level = logging.DEBUG if os.getenv("SCANNER_DEBUG") == "1" else logging.WARNING
    logging.basicConfig(
        level=level,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main Streamlit flow
# ---------------------------------------------------------------------------


def main() -> None:
    # Create safe runtime folders on every startup. This avoids first-run
    # crashes when `data/cache/daily` or `data/universes` does not exist yet.
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

    selected = _render_sidebar(screeners)

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
        cache = _execute_screener(selected)
        if cache is not None:
            st.session_state["scan_cache"] = cache

    cache = st.session_state.get("scan_cache")
    if cache is None or cache["screener_key"] != selected.key:
        st.info("Press **Run screener** in the sidebar to scan for matches.", icon="👈")
        return

    _render_scan_output(selected, cache)


def _render_sidebar(screeners: dict[str, ScreenerDefinition]) -> ScreenerDefinition:
    """Render the sidebar and return the selected screener definition.

    The sidebar is intentionally minimal: data refresh belongs to the CLI
    prefetch step (`python app.py`), and date ranges are derived automatically
    from each screener's `lookback_days`. The Run button writes flags into
    `st.session_state` so the main flow can detect them on the same rerun.
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
        if st.button("Run screener", type="primary", width="stretch"):
            # Invalidate any previously cached scan so the next pass through
            # main() executes the screener fresh.
            st.session_state.pop("scan_cache", None)
            st.session_state["pending_run"] = True

    return selected


def _execute_screener(selected: ScreenerDefinition) -> dict[str, Any] | None:
    """Run the selected screener and return a cache payload.

    Returns `None` when the run aborted before producing results (e.g.,
    missing credentials, universe load failure). The returned dict is stashed
    in `st.session_state["scan_cache"]` so subsequent reruns can re-render
    without re-executing.
    """
    # The UI no longer exposes a date range. We always scan the ten years up to
    # today, mirroring what the CLI prefetch cached locally. The screener can
    # still slice further inside `run(...)` if needed.
    end_date = date.today()
    try:
        start_date = end_date.replace(year=end_date.year - _PREFETCH_YEARS_BACK)
    except ValueError:
        # Feb 29 -> Feb 28 on the historical year that lacks the leap day.
        start_date = end_date.replace(month=2, day=28, year=end_date.year - _PREFETCH_YEARS_BACK)

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

    # `params` carries the progress_callback into the screener. We keep a
    # separate `params_for_chart` without the callback so `build_chart` later
    # never receives a stale function reference.
    params_for_chart: dict[str, Any] = dict(selected.default_params)
    # User overrides (typed into the sidebar's "Tune parameters" expander)
    # take precedence over the screener's declared defaults.
    _apply_param_overrides(selected, params_for_chart)
    params_for_chart.update({"start_date": start_date, "end_date": end_date})
    params: dict[str, Any] = dict(params_for_chart)
    params["progress_callback"] = progress_callback

    try:
        # The data loader handles cache/failure bookkeeping. The screener
        # only receives a clean data access object and returns a DataFrame.
        data_loader = DailyDataLoader(DhanDataClient.from_env())
        results = selected.run(universe_df, data_loader, params)
    except Exception as exc:
        logger.exception("Screener run failed for %s", selected.key)
        st.error(f"Screener run failed: {_redact_secrets(str(exc))}")
        return None
    finally:
        # Always clear the progress widgets so they do not linger above the
        # results table after success OR failure.
        progress_bar.empty()
        progress_status.empty()

    return {
        "screener_key": selected.key,
        "results": results,
        "failures": list(data_loader.last_failures),
        "stats": {
            "cache_hits": data_loader.last_cache_hits,
            "cache_misses": data_loader.last_cache_misses,
            "api_attempts": data_loader.last_api_attempts,
            "rate_limit_retries": data_loader.last_rate_limit_retries,
        },
        "universe_df": universe_df,
        "params_for_chart": params_for_chart,
        "data_loader": data_loader,
    }


def _render_scan_output(selected: ScreenerDefinition, cache: dict[str, Any]) -> None:
    """Render the cached scan: stats + single styled+selectable table + chart."""
    results: pd.DataFrame = cache["results"]
    stats = cache["stats"]
    failures: list[dict[str, Any]] = cache["failures"]

    # A short summary line, with the per-run diagnostics tucked into a
    # collapsed expander so they are available but never clutter the results.
    st.markdown(f"### {len(results)} stock(s) shortlisted")
    with st.expander("Run details", expanded=False):
        detail_col1, detail_col2 = st.columns(2)
        detail_col1.metric("Cache hits", stats["cache_hits"])
        detail_col1.metric("API cache misses", stats["cache_misses"])
        detail_col2.metric("API attempts (incl. retries)", stats["api_attempts"])
        detail_col2.metric("Rate-limit retries", stats["rate_limit_retries"])
        st.caption(f"Fetch failures: {len(failures)}")

    if results.empty:
        st.warning("The screener returned no rows.")
    else:
        chart_symbol = _render_results_with_chart(selected, results, cache)
        # If the selected stock belongs to Hemant Super 45 or Nifty 100, show
        # the Check Fundamentals agent panel after the chart. The helper
        # hides itself for ineligible symbols, so screeners scanning the F&O
        # universe still get this section for any HS45/N100 member that
        # happens to be shortlisted.
        _render_fundamentals_panel(chart_symbol)
        # CSV-safe wrapper neutralizes formula injection before download. The
        # raw DataFrame still has full precision; only the on-screen Styler
        # rounds to 2 decimals, so the CSV mirrors the source data.
        st.download_button(
            "Download results CSV",
            data=_csv_safe(results).to_csv(index=False).encode("utf-8"),
            file_name=f"{selected.key}_results.csv",
            mime="text/csv",
        )

    if failures:
        with st.expander("Fetch failures", expanded=True):
            failures_df = pd.DataFrame(failures)
            if "message" in failures_df.columns:
                failures_df["message"] = failures_df["message"].map(_redact_secrets)
            st.dataframe(failures_df, width="stretch", hide_index=True)


def _render_results_with_chart(
    selected: ScreenerDefinition,
    results: pd.DataFrame,
    cache: dict[str, Any],
) -> str | None:
    """Render the combined results table (row-selectable) and the chart.

    Returns the symbol currently shown on the chart, or None when no chart
    can be rendered (no symbol column, no `build_chart`, etc.).
    """
    table_key = f"results_table_{selected.key}"

    # ONE plain DataFrame does both jobs: emoji BUY/SELL badges for the eye,
    # and `selection_mode` row-selection to drive the chart. We deliberately
    # do NOT pass a pandas Styler here — Streamlit only reliably supports row
    # selection on plain DataFrames. 2-decimal price display is handled by
    # `column_config`, which (unlike a Styler) composes with selection.
    table_state = st.dataframe(
        _emoji_rating(results),
        width="stretch",
        hide_index=True,
        column_config=_decimal_column_config(results),
        selection_mode="single-row",
        on_select="rerun",
        key=table_key,
    )
    if _has_rating_column(results):
        st.caption("🟢 BUY / 🔴 SELL · click a row to chart that symbol.")

    if "symbol" not in results.columns or selected.build_chart is None:
        return None

    symbols = [str(symbol).upper() for symbol in results["symbol"].tolist()]
    if not symbols:
        return None

    st.divider()
    st.subheader("Chart")

    # --- Two-widget sync: the results table AND the dropdown both pick the
    # charted symbol. The control the user *just* used wins.
    #
    # Streamlit gotcha: a keyed widget ignores its `index=`/default on reruns;
    # its value lives in `st.session_state[key]`. So the ONLY way to make a
    # table click move the dropdown is to write the picked symbol into the
    # selectbox's session_state key BEFORE the selectbox is instantiated.
    selected_rows = getattr(getattr(table_state, "selection", None), "rows", []) or []
    current_row = int(selected_rows[0]) if selected_rows else None

    selectbox_key = f"chart_symbol_{selected.key}"
    prev_row_key = f"chart_prev_table_row_{selected.key}"

    # A table click counts only when the selected row CHANGED since the last
    # rerun. Otherwise a stale-but-persistent table selection would override
    # every fresh dropdown change.
    table_changed = current_row is not None and current_row != st.session_state.get(prev_row_key)
    st.session_state[prev_row_key] = current_row

    # Keep the selectbox's stored value valid (a screener re-run can change the
    # `symbols` list out from under a previously stored pick).
    if selectbox_key not in st.session_state or st.session_state[selectbox_key] not in symbols:
        st.session_state[selectbox_key] = symbols[0]
    # A fresh table click wins — push it into the selectbox state pre-widget.
    if table_changed and 0 <= current_row < len(symbols):
        st.session_state[selectbox_key] = symbols[current_row]

    chart_symbol = st.selectbox(
        "Chart symbol",
        symbols,
        key=selectbox_key,
        help="Click a table row OR use this dropdown — whichever you use last wins.",
    )

    universe_df: pd.DataFrame = cache["universe_df"]
    universe_match = universe_df.loc[
        universe_df["symbol"].astype(str).str.upper() == chart_symbol
    ]
    if universe_match.empty:
        st.info(
            f"Could not find `{chart_symbol}` in universe `{selected.universe}`. "
            "Try refreshing universes via `python app.py`."
        )
        return chart_symbol
    security_id = str(universe_match.iloc[0].get("security_id", "")).strip()
    if not security_id:
        st.info(f"`{chart_symbol}` has no mapped security_id; cannot load candles.")
        return chart_symbol

    data_loader: DailyDataLoader = cache["data_loader"]
    candles = data_loader.read_cached_history(chart_symbol, security_id)
    if candles.empty:
        st.info(
            f"No cached candles for `{chart_symbol}`. Run `python app.py` to "
            "backfill the local cache for every stock in the union."
        )
        return chart_symbol

    try:
        # The screener returns a chart "spec" (plain dict); `render_chart_html`
        # turns it into a TradingView Lightweight Charts widget.
        spec = selected.build_chart(candles, cache["params_for_chart"])
        chart_html = render_chart_html(spec)
    except Exception as exc:
        logger.exception("build_chart failed for %s on %s", selected.key, chart_symbol)
        st.error(f"Could not build chart: {_redact_secrets(str(exc))}")
        return chart_symbol

    # The chart is an embedded Lightweight Charts widget. Its price scale is
    # natively drag-to-scale — exactly the TradingView-style Y-axis zoom.
    components.html(chart_html, height=int(spec.get("height", 640)), scrolling=False)
    st.caption(
        "Chart controls: drag the price scale (right edge) to scale the Y-axis · "
        "drag the chart to pan · scroll to zoom · double-click to reset."
    )
    return chart_symbol


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
