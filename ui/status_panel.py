"""System-status panel and universe-file status table (REF-003).

Renders the pre-run health checks — Dhan credentials, universe row counts,
last refresh time, local candle-cache size — plus the lazy "Universe file
status" expander. The small ``@st.cache_data`` helpers here exist because
Streamlit reruns the whole script on ordinary widget interactions; a 30-second
cache keeps row clicks and dropdown changes from repeatedly walking the
filesystem or re-reading universe CSVs.

Beginner note:
This module was extracted from ``app.py`` (REF-003, the third slimming pass
after REF-001/REF-002). ``app.py`` re-exports every helper so existing imports
like ``app.show_status_panel`` keep working, and its
``refresh_universes_and_invalidate()`` still clears the caches defined here —
function identity is preserved through the re-export, so ``.clear()`` on the
``app.<name>`` binding hits the same cached function object.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

from backend.config import DAILY_CACHE_DIR, credential_status
from backend.screener_registry import ScreenerDefinition
from backend.universe_builder import UNIVERSE_CONFIG
from backend.universe_loader import (
    all_universe_statuses,
    universe_file_path,
    universe_status,
)


@st.cache_data(ttl=30, show_spinner=False)
def cache_summary(cache_dir: Path | None = None) -> dict[str, Any]:
    """Count cached candle files so the UI can show whether caching is active.

    The cache directory can contain hundreds of Parquet files. Streamlit reruns
    the script for ordinary widget interactions, so caching this small summary
    for 30 seconds keeps row clicks and dropdown changes from repeatedly
    walking the filesystem.

    Beginner note:
    ``cache_dir`` defaults to ``None`` and resolves to ``DAILY_CACHE_DIR``
    inside the body on purpose. A ``cache_dir: Path = DAILY_CACHE_DIR``
    default would bind the config value once, at function-definition time —
    so a test (or a future config override) that repoints the cache directory
    after this module is imported would silently keep summarizing the old
    path. Resolving in the body reads the current value on every call.
    """
    if cache_dir is None:
        cache_dir = DAILY_CACHE_DIR
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
