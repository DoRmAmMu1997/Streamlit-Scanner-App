"""Shared display helpers used by multiple UI pages (REF-001).

These helpers existed in app.py first; they moved here because both the main
scanner page and the scan-history page need them, and pages must not import
each other (or app.py) without creating cycles.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

import numpy as np
import pandas as pd
import streamlit as st

from backend.auth.session import auth_secret_values
from backend.scanner_base import PROVENANCE_COLUMN
from backend.scoring import sort_by_final_score
from backend.security import redact_text


def _drop_provenance(results: pd.DataFrame) -> pd.DataFrame:
    """Return a copy without legacy or canonical internal provenance columns.

    PROV-002 attaches a per-row provenance dict to every screener frame for
    persistence. It is machine-readable evidence, not something to render in the
    results table or dump into the download CSV (a raw dict cell would show as a
    repr and bloat the file), so display/export paths drop it. ``errors="ignore"``
    keeps this safe for legacy or hand-built frames that never had the column.
    """
    return results.drop(
        columns=[PROVENANCE_COLUMN, "provenance_json", "score_breakdown"],
        errors="ignore",
    )


def _sort_results_by_final_score(results: pd.DataFrame) -> pd.DataFrame:
    """Order display/export rows by ``final_score`` (shared RANK-002 rule).

    The scan service already ranks new results, but persisted history may include
    older or hand-built frames. Delegating to ``backend.scoring.sort_by_final_score``
    guarantees the scanner page, the history page, and the live scorer all order
    rows identically (highest score first, null/non-finite last, ties stable).
    """
    return sort_by_final_score(results)


_SCORE_COMPONENT_COLUMNS = [
    "Symbol",
    "Final score",
    "Technical",
    "Liquidity",
    "Risk",
    "Freshness",
    "Coverage",
    "Missing",
]


def _score_components_frame(results: pd.DataFrame) -> pd.DataFrame:
    """Build the compact RANK-002 component table for Streamlit expanders.

    ``score_breakdown`` is an audit receipt, so the main results table hides the
    raw dictionary. This helper extracts just the human-sized fields: component
    scores, coverage, and missing components.

    The helper accepts both shapes used in the app:
    - a direct ``score_breakdown`` column from a hand-built/test frame; and
    - a nested ``provenance``/``provenance_json.score_breakdown`` receipt from
      real scanner persistence.
    """
    rows: list[dict[str, Any]] = []
    # to_dict("records") types its keys as Hashable; result columns are strings.
    for row in cast(list[dict[str, Any]], results.to_dict("records")):
        breakdown = _extract_score_breakdown(row)
        if breakdown is None:
            continue
        components = breakdown.get("components")
        component_map = components if isinstance(components, Mapping) else {}
        # Convert every numeric field through _optional_float so Streamlit shows
        # a blank cell for missing data instead of the literal strings "nan" or
        # "None". Coverage/missing stay as readable comma-separated text.
        rows.append(
            {
                "Symbol": row.get("symbol", ""),
                "Final score": _optional_float(
                    row.get("final_score", breakdown.get("final_score"))
                ),
                "Technical": _optional_float(component_map.get("technical")),
                "Liquidity": _optional_float(component_map.get("liquidity")),
                "Risk": _optional_float(component_map.get("risk")),
                "Freshness": _optional_float(component_map.get("freshness")),
                "Coverage": _join_component_names(breakdown.get("coverage")),
                "Missing": _join_component_names(breakdown.get("missing")),
            }
        )
    return pd.DataFrame(rows, columns=_SCORE_COMPONENT_COLUMNS)


def _extract_score_breakdown(row: Mapping[str, Any]) -> Mapping[str, Any] | None:
    """Find ``score_breakdown`` on a result row or inside provenance.

    The UI reads receipts but never mutates them. Returning ``None`` for unknown
    shapes keeps legacy scan results renderable even before RANK-002 existed.
    """
    direct = row.get("score_breakdown")
    if isinstance(direct, Mapping):
        return direct

    # Fresh scanner rows usually keep the receipt inside canonical provenance.
    # History rows use ``provenance_json`` after SQLAlchemy has read JSON back
    # from the database.
    for column in (PROVENANCE_COLUMN, "provenance_json"):
        provenance = row.get(column)
        if not isinstance(provenance, Mapping):
            continue
        nested = provenance.get("score_breakdown")
        if isinstance(nested, Mapping):
            return nested
    return None


def _optional_float(value: Any) -> float | None:
    """Coerce a display number while keeping missing/non-finite values blank.

    Streamlit's numeric column formatter works best with real floats or nulls,
    not mixed strings like ``"nan"``.
    """
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if np.isfinite(result) else None


def _join_component_names(value: Any) -> str:
    """Render a list-like component set as compact comma-separated text.

    Receipts store coverage and missing components as JSON lists because that is
    easier for code to inspect. Humans scanning the UI need a short phrase.
    """
    if isinstance(value, str):
        return value
    if isinstance(value, list | tuple):
        return ", ".join(str(item) for item in value)
    return ""


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
        if series.dtype == object or pd.api.types.is_string_dtype(series.dtype):
            safe[column] = series.map(_escape_cell)
    return safe


def _escape_cell(value: Any) -> Any:
    """Prefix a single dangerous string cell with an apostrophe."""
    if isinstance(value, str) and value.startswith(_CSV_INJECTION_PREFIXES):
        return "'" + value
    return value


def _redact_secrets(text: str) -> str:
    """Strip any loaded credentials from an error message before display.

    Delegates to ``backend.security.redaction`` so Streamlit, backend jobs, and
    tests all share one definition of "secret-looking text." Streamlit-specific
    OIDC values still come from ``st.secrets``, so this wrapper passes them as
    extra secrets on top of the process/env-backed DEPLOY-004 settings.

    Beginner note:
    SDKs and frameworks occasionally embed request payloads or config values in
    exception messages. We replace known secret values with a fixed mask before
    passing text to `st.error(...)`, so an error panel can still be useful
    without accidentally leaking credentials.

    The shared helper is intentionally best-effort. If settings parsing itself
    failed (for example `LOG_LEVEL=chatty`), this function must still return a
    readable error string instead of raising a second exception while trying to
    redact the first one.
    """
    return redact_text(text, extra_secrets=auth_secret_values(st))


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
