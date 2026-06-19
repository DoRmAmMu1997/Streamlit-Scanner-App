"""Local sector metadata helpers for the validation dashboard.

The current committed universe CSVs mostly contain instrument mapping columns,
not sector classifications. This helper still exists so VALID-004 can use sector
metadata as soon as a local universe file provides it, while gracefully returning
an empty mapping today.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pandas as pd

from backend.universe_loader import load_universe

UniverseLoader = Callable[[str], pd.DataFrame]

_SECTOR_COLUMNS = ("sector", "industry", "macro_sector", "sector_name")


def load_universe_sector_lookup(
    universe_keys: Iterable[str],
    *,
    universe_loader: UniverseLoader = load_universe,
) -> dict[tuple[str, str], str]:
    """Return ``{(universe_key, SYMBOL): sector}`` from local universe metadata.

    Missing sector columns are expected in the current repo and simply produce no
    rows. Missing/corrupt universe files are also ignored here because the
    validation dashboard is read-only; a metadata problem should not take down
    performance metrics that can still render with the ``Unknown`` fallback.
    """
    lookup: dict[tuple[str, str], str] = {}
    for universe_key in sorted({str(key) for key in universe_keys if str(key).strip()}):
        try:
            universe = universe_loader(universe_key)
        except (KeyError, FileNotFoundError, ValueError):
            continue
        sector_column = _first_sector_column(universe)
        if sector_column is None or "symbol" not in universe.columns:
            continue
        for _, row in universe.iterrows():
            symbol = str(row.get("symbol", "")).strip().upper()
            sector = str(row.get(sector_column, "")).strip()
            if symbol and sector:
                lookup[(universe_key, symbol)] = sector
    return lookup


def _first_sector_column(universe: pd.DataFrame) -> str | None:
    """Pick the first known sector-like column, case-insensitively."""
    columns_by_lower = {str(column).lower(): str(column) for column in universe.columns}
    for wanted in _SECTOR_COLUMNS:
        if wanted in columns_by_lower:
            return columns_by_lower[wanted]
    return None
