"""Per-session chart HTML cache, extracted from app.py (REF-001).

Rendering a Lightweight Charts payload is the most expensive UI step (indicator
math plus JSON serialization plus HTML assembly). This module caches rendered
HTML in ``st.session_state`` keyed by everything that can change the output:
screener, symbol, security id, the candle cache file token, and a digest of the
chart-relevant params. The cache is bounded (LRU-style eviction) and versioned
so a deploy that changes the payload shape rebuilds instead of failing.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import streamlit as st

from backend.charts import render_chart_html
from backend.screener_registry import ScreenerDefinition

logger = logging.getLogger(__name__)

# Version stamp for cached payload dicts. Bump this whenever the stored shape
# changes so payloads cached by an older build rebuild cleanly instead of
# half-deserializing after a deploy.
_CHART_PAYLOAD_SCHEMA = 1

_CHART_HTML_CACHE_STATE_KEY = "chart_html_cache"
_CHART_HTML_CACHE_LIMIT = 16


@dataclass(frozen=True)
class _ChartRenderPayload:
    """Rendered chart HTML plus metadata needed by Streamlit's embed call."""

    html: str
    height: int
    from_cache: bool = False


def _json_cache_default(value: Any) -> str:
    """Serialize non-JSON values in chart parameters for cache-key hashing."""
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return str(value)


def _chart_params_digest(params_for_chart: dict[str, Any]) -> str:
    """Return a stable digest for chart-affecting screener parameters.

    Beginner note: a chart cache key cannot store a raw dict directly because
    dict ordering and date objects can vary across reruns. We convert the dict
    into sorted JSON, then hash that string into a compact key fragment.
    """
    payload = json.dumps(
        params_for_chart,
        sort_keys=True,
        default=_json_cache_default,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _chart_file_token(data_loader, symbol: str, security_id: str) -> tuple[str, int | None]:
    """Return the candle cache path and mtime used to invalidate chart HTML.

    If a fresh prefetch updates the Parquet file, `st_mtime_ns` changes and the
    chart cache key changes with it. That gives us a cheap invalidation signal
    without reading the candle DataFrame on every rerun.
    """
    cache_path = getattr(data_loader, "cache_path", None)
    if not callable(cache_path):
        return ("no-cache-path", None)
    path = Path(cache_path(symbol, security_id))
    try:
        return (str(path), path.stat().st_mtime_ns if path.exists() else None)
    except OSError:
        logger.warning("Could not stat chart cache path %s", path)
        return (str(path), None)


def _chart_html_cache_key(
    selected: ScreenerDefinition,
    chart_symbol: str,
    security_id: str,
    data_loader,
    params_for_chart: dict[str, Any],
) -> str:
    """Build the session-state key for one rendered chart payload."""
    path_text, mtime_ns = _chart_file_token(data_loader, chart_symbol, security_id)
    raw_key = json.dumps(
        {
            "screener": selected.key,
            "symbol": chart_symbol,
            "security_id": security_id,
            "path": path_text,
            "mtime_ns": mtime_ns,
            "params": _chart_params_digest(params_for_chart),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return "chart-html::" + hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _chart_payload_store() -> dict[str, dict[str, Any]]:
    """Return the bounded per-session chart HTML cache.

    This lives in `st.session_state`, not `st.cache_data`, because the payload
    depends on the selected screener's Python callable. Session-state caching is
    simpler and avoids asking Streamlit to hash function objects.
    """
    store = st.session_state.setdefault(_CHART_HTML_CACHE_STATE_KEY, {})
    if not isinstance(store, dict):
        store = {}
        st.session_state[_CHART_HTML_CACHE_STATE_KEY] = store
    return store


def _remember_chart_payload(
    store: dict[str, dict[str, Any]],
    cache_key: str,
    payload: _ChartRenderPayload,
) -> None:
    """Save one chart payload while keeping session memory bounded."""
    if cache_key not in store and len(store) >= _CHART_HTML_CACHE_LIMIT:
        # Dicts preserve insertion order, so popping the first key discards the
        # oldest chart this session cached. That prevents a long browsing
        # session from accumulating unbounded HTML strings.
        oldest_key = next(iter(store))
        store.pop(oldest_key, None)
    store[cache_key] = {
        "schema": _CHART_PAYLOAD_SCHEMA,
        "html": payload.html,
        "height": payload.height,
    }


def _get_or_build_chart_payload(
    selected: ScreenerDefinition,
    chart_symbol: str,
    security_id: str,
    data_loader,
    params_for_chart: dict[str, Any],
) -> _ChartRenderPayload | None:
    """Return rendered chart HTML, reusing a session cache when possible.

    A table row click or dropdown change causes Streamlit to rerun this file.
    Without this helper, the app re-read candles, rebuilt indicators, serialized
    the chart spec, and regenerated HTML every time the same row stayed
    selected. The key includes the candle cache file mtime and chart params, so
    a real data or parameter change still rebuilds.
    """
    if selected.build_chart is None:
        return None

    cache_key = _chart_html_cache_key(
        selected,
        chart_symbol,
        security_id,
        data_loader,
        params_for_chart,
    )
    store = _chart_payload_store()
    cached = store.get(cache_key)
    if cached is not None and cached.get("schema") != _CHART_PAYLOAD_SCHEMA:
        # Payload cached by an older build of this module; rebuild below.
        store.pop(cache_key, None)
        cached = None
    if cached is not None:
        try:
            return _ChartRenderPayload(
                html=str(cached["html"]),
                height=int(cached["height"]),
                from_cache=True,
            )
        except (KeyError, TypeError, ValueError):
            # A malformed session-state value should not break the chart pane;
            # drop it and rebuild from disk below.
            store.pop(cache_key, None)

    candles = data_loader.read_cached_history(chart_symbol, security_id)
    if candles.empty:
        return None

    spec = selected.build_chart(candles, params_for_chart)
    payload = _ChartRenderPayload(
        html=render_chart_html(spec),
        height=int(spec.get("height", 640)),
    )
    _remember_chart_payload(store, cache_key, payload)
    return payload
