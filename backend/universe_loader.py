"""Read and summarize generated universe CSV files.

Universe building downloads/mapping data. Universe loading is the simpler
runtime side: open the already-created CSV and make sure it has the columns a
screener needs before Dhan candle fetching begins.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any

import pandas as pd

from backend.config import UNIVERSE_DIR
from backend.universe_builder import UNIVERSE_CONFIG, universe_file_path

# These columns are the minimum needed to fetch daily candles from Dhan.
REQUIRED_UNIVERSE_COLUMNS = ["symbol", "security_id", "exchange_segment", "instrument_type"]


def list_known_universes() -> dict[str, dict[str, Any]]:
    """Return metadata for every universe the app currently understands."""
    return UNIVERSE_CONFIG


def load_universe(universe_key: str, universe_dir: Path | str = UNIVERSE_DIR) -> pd.DataFrame:
    """Load one universe CSV and normalize key text columns."""
    if universe_key not in UNIVERSE_CONFIG:
        raise KeyError(f"Unknown universe key: {universe_key}")

    path = universe_file_path(universe_key, universe_dir)
    if not path.exists():
        raise FileNotFoundError(
            f"Universe CSV not found: {path}. Run `python app.py` to refresh universes before this screener."
        )

    df = pd.read_csv(path, dtype=str).fillna("")
    for column in REQUIRED_UNIVERSE_COLUMNS:
        if column not in df.columns:
            raise ValueError(f"{path} is missing required column: {column}")

    # Normalize the CSV values once here so screeners don't repeat cleanup.
    df["symbol"] = df["symbol"].astype(str).str.upper().str.strip()
    df["security_id"] = df["security_id"].astype(str).str.strip()
    df["exchange_segment"] = df["exchange_segment"].replace("", "NSE_EQ")
    df["instrument_type"] = df["instrument_type"].replace("", "EQUITY")
    if "mapping_status" not in df.columns:
        # Manual universe CSVs might omit mapping_status. Recreate the same idea
        # from security_id so the rest of the app can still filter mapped rows.
        df["mapping_status"] = df["security_id"].ne("").map(
            lambda value: "mapped" if value else "missing_security_id"
        )
    return df


def mapped_only(universe_df: pd.DataFrame) -> pd.DataFrame:
    """Return only rows that have enough Dhan mapping data to be fetched."""
    if "mapping_status" in universe_df.columns:
        return universe_df.loc[
            universe_df["mapping_status"].astype(str).str.lower().eq("mapped")
        ].copy()
    return universe_df.loc[universe_df["security_id"].astype(str).str.strip().ne("")].copy()


def universe_status(universe_key: str, universe_dir: Path | str = UNIVERSE_DIR) -> dict[str, Any]:
    """Return row counts and freshness details for one universe CSV."""
    path = universe_file_path(universe_key, universe_dir)
    status = {
        "key": universe_key,
        "name": UNIVERSE_CONFIG[universe_key]["display_name"],
        "path": str(path),
        "exists": path.exists(),
        "rows": 0,
        "mapped_rows": 0,
        "modified": "",
    }
    if not path.exists():
        return status

    try:
        # This function is used by the UI status panel, so it catches CSV
        # problems and reports them as text instead of crashing the whole app.
        df = pd.read_csv(path, dtype=str).fillna("")
        status["rows"] = len(df)
        if "mapping_status" in df.columns:
            status["mapped_rows"] = int(df["mapping_status"].str.lower().eq("mapped").sum())
        elif "security_id" in df.columns:
            status["mapped_rows"] = int(df["security_id"].astype(str).str.strip().ne("").sum())
        modified_ts = datetime.fromtimestamp(path.stat().st_mtime)
        status["modified"] = modified_ts.strftime("%Y-%m-%d %H:%M:%S")
    except Exception as exc:
        status["error"] = str(exc)
    return status


def all_universe_statuses(universe_dir: Path | str = UNIVERSE_DIR) -> list[dict[str, object]]:
    """Return status rows for the expandable Streamlit universe table."""
    return [universe_status(key, universe_dir) for key in UNIVERSE_CONFIG]


def union_of_mapped_universes(universe_dir: Path | str = UNIVERSE_DIR) -> pd.DataFrame:
    """Return one row per mapped (symbol, security_id) across all universes.

    The scanner caches 10 years of candles up front. The CLI prefetch iterates
    over THIS union so each stock is downloaded once even when it appears in
    multiple universes (e.g., RELIANCE is in both NIFTY 100 and the F&O list).

    Beginner note:
    `mapping_status == "mapped"` is the existing universe-builder marker for
    rows where we successfully resolved a Dhan `security_id`. Unmapped rows
    are kept in the CSVs for debugging visibility but cannot be fetched.
    """
    frames: list[pd.DataFrame] = []
    for key in UNIVERSE_CONFIG:
        try:
            frames.append(load_universe(key, universe_dir))
        except FileNotFoundError:
            # Missing CSVs are not an error here. If the user has never run
            # the prefetch (or only built some universes), we just skip and
            # work with whatever is on disk.
            continue

    if not frames:
        return pd.DataFrame(columns=[*REQUIRED_UNIVERSE_COLUMNS, "mapping_status"])

    combined = pd.concat(frames, ignore_index=True)
    mapped = combined.loc[combined["mapping_status"].astype(str).str.lower() == "mapped"]
    # Dedupe by security_id (not symbol): the same security ID is always the
    # same Dhan instrument, while symbol strings can technically collide.
    return mapped.drop_duplicates(subset=["security_id"], keep="first").reset_index(drop=True)
