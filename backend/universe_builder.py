"""Build scanner universe CSVs.

A "universe" is the stock list a screener is allowed to scan. For example, one
screener might scan NIFTY 100 while another scans F&O stocks. The final CSVs
must contain Dhan `security_id` values, because Dhan history calls need those
IDs rather than just human-readable stock symbols.
"""

from __future__ import annotations

import io
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import requests

from backend.config import (
    DEPENDENCIES_DIR,
    DHAN_SCRIP_MASTER_URL,
    NIFTY_100_URL,
    NIFTY_500_URL,
    REQUEST_HEADERS,
    UNIVERSE_DIR,
)

# The Hemant lists are intentionally local CSVs. Unlike NIFTY 100/500, there is
# no stable public CSV endpoint to download during startup, so we keep the
# pinned source lists next to the generated universe files in `data/universes/`.
HEMANT_SOURCE_FILES: dict[str, Path] = {
    "hemant_super_45": UNIVERSE_DIR / "hemant_super_45.csv",
    "hemant_good_45": UNIVERSE_DIR / "hemant_good_45.csv",
    "hemant_good_200": UNIVERSE_DIR / "hemant_good_200.csv",
}

# Some symbols in the Hemant source use the Google Doc's naming, while Dhan's
# cash-equity master uses NSE trading symbols. Apply these known one-off
# translations before joining to Dhan, otherwise those rows would look unmapped
# even though the stock exists in the instrument master.
MANUAL_SYMBOL_ALIASES = {
    "NAM_INDIA": "NAM-INDIA",
    "BAJAJ_AUTO": "BAJAJ-AUTO",
    "UTLTRACEMCO": "ULTRACEMCO",
    "MCDOWELL_N": "UNITDSPR",
}


# One place to define every supported universe. Screeners refer to these keys
# in their metadata, so keep keys stable once a screener uses them.
UNIVERSE_CONFIG = {
    "nifty_100": {
        "file_name": "nifty_100.csv",
        "display_name": "NIFTY 100",
        "source_url": NIFTY_100_URL,
    },
    "nifty_500": {
        "file_name": "nifty_500.csv",
        "display_name": "NIFTY 500",
        "source_url": NIFTY_500_URL,
    },
    "fno": {
        "file_name": "fno_stocks.csv",
        "display_name": "NSE F&O Stocks",
        "source_url": DHAN_SCRIP_MASTER_URL,
    },
    # Hemant lists are local `source_file` universes, not remote `source_url`
    # universes. The refresh loop sees `source_file` and routes through
    # `load_symbol_list_csv()` + `build_symbol_list_universe()` below.
    "hemant_super_45": {
        "file_name": "hemant_super_45.csv",
        "display_name": "Hemant Super 45",
        "source_file": str(HEMANT_SOURCE_FILES["hemant_super_45"]),
    },
    "hemant_good_45": {
        "file_name": "hemant_good_45.csv",
        "display_name": "Hemant Good 45",
        "source_file": str(HEMANT_SOURCE_FILES["hemant_good_45"]),
    },
    "hemant_good_200": {
        "file_name": "hemant_good_200.csv",
        "display_name": "Hemant Good 200",
        "source_file": str(HEMANT_SOURCE_FILES["hemant_good_200"]),
    },
    # A composite universe: the union (deduped by symbol) of two other Hemant
    # lists. `union_of` is handled by a dedicated branch in
    # `refresh_universe_files`; the members are read from their own source
    # lists, concatenated, and mapped against the same Dhan master snapshot.
    "hemant_super_good_union": {
        "file_name": "hemant_super_good_union.csv",
        "display_name": "Hemant Super + Good 45",
        "union_of": ["hemant_super_45", "hemant_good_45"],
    },
    "hemant_super_good_200_union": {
        "file_name": "hemant_super_good_200_union.csv",
        "display_name": "Hemant Super + Good 45 + Good 200",
        "union_of": ["hemant_super_45", "hemant_good_45", "hemant_good_200"],
    },
}

FNO_SYMBOL_PATTERN = re.compile(
    r"-(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{4}"
    r"(?:-[0-9.]+-(?:CE|PE)|-FUT)$",
    re.IGNORECASE,
)

# The current Dhan detailed instrument master uses these plain column names.
# Older files used SEM_* names, so the normalizer below converts those older
# names once and the rest of the module only works with this canonical schema.
CANONICAL_INSTRUMENT_COLUMNS = [
    "EXCH_ID",
    "SEGMENT",
    "SECURITY_ID",
    "INSTRUMENT",
    "UNDERLYING_SYMBOL",
    "SYMBOL_NAME",
    "DISPLAY_NAME",
    "SERIES",
]

LEGACY_COLUMN_CANDIDATES = {
    "EXCH_ID": ["SEM_EXM_EXCH_ID"],
    "SEGMENT": ["SEM_SEGMENT"],
    "SECURITY_ID": ["SEM_SMST_SECURITY_ID"],
    "INSTRUMENT": ["SEM_INSTRUMENT_NAME"],
    "SYMBOL_NAME": ["SEM_TRADING_SYMBOL"],
    "DISPLAY_NAME": ["SEM_CUSTOM_SYMBOL", "SM_SYMBOL_NAME"],
    "SERIES": ["SEM_SERIES"],
}


# Hard cap on every CSV download. The official Dhan instrument master is roughly
# a few MB and the NIFTY constituent files are tiny. A 50 MB ceiling leaves
# generous headroom while preventing a misbehaving or hostile endpoint from
# streaming gigabytes of data into the scanner's memory.
MAX_DOWNLOAD_BYTES = 50 * 1024 * 1024
INSTRUMENT_MASTER_SNAPSHOT_PATTERN = "all_instrument *.csv"


def download_csv(
    url: str,
    timeout_seconds: float = 60.0,
    max_bytes: int = MAX_DOWNLOAD_BYTES,
) -> pd.DataFrame:
    """Download a public CSV (size-capped) and return it as a string-filled DataFrame.

    Beginner note:
    - `requests.get(..., stream=True)` opens the connection but does not read the
      whole body up front. That lets us count bytes as they arrive and abort
      early if the response is unreasonably large.
    - `verify=True` is the default for `requests`. We pass it explicitly to make
      it obvious that HTTPS certificate validation is on; turning it off would
      enable man-in-the-middle attacks on the public CSV endpoints.
    """
    # dtype=str prevents pandas from turning security IDs into numbers/floats.
    # fillna("") keeps later string cleanup simple and predictable.
    with requests.get(
        url,
        headers=REQUEST_HEADERS,
        timeout=(15.0, float(timeout_seconds)),
        stream=True,
        verify=True,
    ) as response:
        response.raise_for_status()

        # If the server advertises a Content-Length we can refuse oversized
        # downloads before reading a single byte of the body.
        advertised_length = response.headers.get("Content-Length")
        if advertised_length is not None:
            try:
                advertised_size = int(advertised_length)
            except (TypeError, ValueError):
                # An unparsable Content-Length is treated as "unknown"; the byte
                # counter below still enforces the cap.
                advertised_size = None
            if advertised_size is not None and advertised_size > max_bytes:
                raise ValueError(
                    f"Refusing to download {url}: advertised size "
                    f"{advertised_length} bytes exceeds cap of {max_bytes} bytes."
                )

        # Read the body in chunks while counting bytes. Aborting mid-stream is
        # safe because the `with` block closes the connection on the way out.
        buffer = bytearray()
        for chunk in response.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            buffer.extend(chunk)
            if len(buffer) > max_bytes:
                raise ValueError(
                    f"Refusing to download {url}: response exceeded "
                    f"{max_bytes} bytes after streaming."
                )

    text = bytes(buffer).decode("utf-8-sig", errors="ignore")
    return pd.read_csv(io.StringIO(text), dtype=str, low_memory=False).fillna("")


def instrument_master_snapshot_path(
    run_date: date | datetime | None = None,
    snapshot_dir: Path | str = DEPENDENCIES_DIR,
) -> Path:
    """Return the dated local CSV path for the Dhan instrument master snapshot."""
    selected_date = run_date or date.today()
    if isinstance(selected_date, datetime):
        selected_date = selected_date.date()
    return Path(snapshot_dir) / f"all_instrument {selected_date:%Y-%m-%d}.csv"


def prune_old_instrument_master_snapshots(snapshot_path: Path | str) -> list[Path]:
    """Delete older Dhan instrument-master snapshots after a fresh snapshot exists.

    The important safety rule is order: callers should write and close the new
    `snapshot_path` first, then call this helper. That way a failed download or
    failed CSV write never removes the last usable local Dhan master.
    """
    keep_path = Path(snapshot_path).resolve()
    snapshot_dir = keep_path.parent
    deleted_paths: list[Path] = []

    if not snapshot_dir.exists():
        return deleted_paths

    for candidate in snapshot_dir.glob(INSTRUMENT_MASTER_SNAPSHOT_PATTERN):
        # `glob()` can match a directory if it has a .csv-looking name. Only
        # normal files are old snapshots, so folders and other entries stay put.
        if not candidate.is_file():
            continue

        candidate_path = candidate.resolve()
        if candidate_path == keep_path:
            continue

        # The search starts in snapshot_dir, and this extra parent check keeps
        # deletion scoped there even if a future path change gets clever.
        if candidate.parent.resolve() != snapshot_dir:
            continue

        candidate.unlink()
        deleted_paths.append(candidate)

    return deleted_paths


def strip_fno_suffix(trading_symbol: str) -> str:
    """Convert derivative symbols like RELIANCE-MAY2026-FUT to RELIANCE."""
    base = FNO_SYMBOL_PATTERN.sub("", str(trading_symbol or "").strip())
    return base.upper().strip()


def _fill_from_first_available(frame: pd.DataFrame, target: str, candidates: list[str]) -> None:
    """Create/fill one canonical column from the first useful legacy column."""
    if target not in frame.columns:
        frame[target] = ""

    target_values = frame[target].fillna("").astype(str)
    for candidate in candidates:
        if candidate not in frame.columns:
            continue
        candidate_values = frame[candidate].fillna("").astype(str)
        target_values = target_values.where(target_values.str.strip() != "", candidate_values)
    frame[target] = target_values


def normalize_instrument_master_columns(instrument_master: pd.DataFrame) -> pd.DataFrame:
    """Return Dhan instrument-master rows with current non-SEM column names."""
    normalized = instrument_master.copy()
    normalized.columns = [str(column).strip() for column in normalized.columns]

    for target, candidates in LEGACY_COLUMN_CANDIDATES.items():
        _fill_from_first_available(normalized, target, candidates)

    if "UNDERLYING_SYMBOL" not in normalized.columns:
        # Current Dhan files provide UNDERLYING_SYMBOL directly. Legacy files did
        # not, so derive it from the trading/contract symbol as a compatibility
        # fallback.
        normalized["UNDERLYING_SYMBOL"] = ""
    normalized["UNDERLYING_SYMBOL"] = normalized["UNDERLYING_SYMBOL"].fillna("").astype(str)
    fallback_symbol = (
        normalized["SYMBOL_NAME"].fillna("").astype(str).map(strip_fno_suffix)
        if "SYMBOL_NAME" in normalized.columns
        else ""
    )
    normalized["UNDERLYING_SYMBOL"] = normalized["UNDERLYING_SYMBOL"].where(
        normalized["UNDERLYING_SYMBOL"].str.strip() != "",
        fallback_symbol,
    )

    for column in CANONICAL_INSTRUMENT_COLUMNS:
        if column not in normalized.columns:
            normalized[column] = ""
        normalized[column] = normalized[column].fillna("").astype(str).str.strip()

    # The app should no longer expose or reason about SEM_* names. Dropping them
    # also makes the saved snapshot easier to inspect manually.
    legacy_sem_columns = [column for column in normalized.columns if column.upper().startswith("SEM_")]
    return normalized.drop(columns=legacy_sem_columns, errors="ignore")


def _require_instrument_columns(frame: pd.DataFrame, required_columns: Iterable[str]) -> None:
    """Raise a clear error if Dhan's instrument master is missing needed fields."""
    for column in required_columns:
        if column not in frame.columns:
            raise ValueError(f"Instrument master is missing required column: {column}")


def load_instrument_master(
    url: str = DHAN_SCRIP_MASTER_URL,
    save_snapshot: bool = True,
    snapshot_dir: Path | str = DEPENDENCIES_DIR,
    run_date: date | datetime | None = None,
) -> pd.DataFrame:
    """Load Dhan's detailed instrument master, normalize it, and save a snapshot."""
    df = download_csv(url)
    if df.empty:
        raise ValueError(f"Dhan instrument master is empty: {url}")
    normalized = normalize_instrument_master_columns(df)
    if save_snapshot:
        snapshot_path = instrument_master_snapshot_path(run_date=run_date, snapshot_dir=snapshot_dir)
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        normalized.to_csv(snapshot_path, index=False)
        prune_old_instrument_master_snapshots(snapshot_path)
    return normalized


def build_equity_lookup(instrument_master: pd.DataFrame) -> pd.DataFrame:
    """Build a Dhan cash-equity lookup keyed by NSE stock symbol."""
    # Normalize first so every downstream line can use the current Dhan column
    # names even if a test or old local file still contains SEM_* columns.
    eq = normalize_instrument_master_columns(instrument_master)
    required_columns = [
        "EXCH_ID",
        "SEGMENT",
        "INSTRUMENT",
        "SECURITY_ID",
        "UNDERLYING_SYMBOL",
        "SYMBOL_NAME",
        "DISPLAY_NAME",
        "SERIES",
    ]
    _require_instrument_columns(eq, required_columns)
    for column in required_columns:
        eq[column] = eq[column].fillna("").astype(str).str.strip()

    # We want cash-market stock IDs, not derivatives. Daily screeners should
    # fetch the listed equity candle even if the universe is derived from F&O.
    eq = eq.loc[
        (eq["EXCH_ID"].str.upper() == "NSE")
        & (eq["SEGMENT"].str.upper() == "E")
        & (eq["INSTRUMENT"].str.upper() == "EQUITY")
        & (eq["UNDERLYING_SYMBOL"] != "")
    ].copy()
    eq["symbol"] = eq["UNDERLYING_SYMBOL"].str.upper()
    # If a symbol appears in multiple NSE series, prefer the standard EQ series.
    eq["series_priority"] = (eq["SERIES"].str.upper() == "EQ").astype(int)
    eq = eq.sort_values(["symbol", "series_priority"], ascending=[True, False])
    eq = eq.drop_duplicates(subset=["symbol"], keep="first")

    return pd.DataFrame(
        {
            "symbol": eq["symbol"],
            "security_id": eq["SECURITY_ID"],
            "exchange_segment": "NSE_EQ",
            "instrument_type": "EQUITY",
            "company_name": eq["DISPLAY_NAME"].where(eq["DISPLAY_NAME"] != "", eq["SYMBOL_NAME"]),
            "series": eq["SERIES"],
        }
    ).reset_index(drop=True)


def build_fno_universe(instrument_master: pd.DataFrame, equity_lookup: pd.DataFrame) -> pd.DataFrame:
    """Build the NSE stock F&O universe and map it back to cash-market IDs."""
    work = normalize_instrument_master_columns(instrument_master)
    required_columns = ("EXCH_ID", "SEGMENT", "INSTRUMENT", "UNDERLYING_SYMBOL", "SYMBOL_NAME")
    _require_instrument_columns(work, required_columns)
    for column in required_columns:
        work[column] = work[column].fillna("").astype(str).str.strip()

    # Segment D contains derivatives. OPTSTK and FUTSTK are stock derivatives;
    # this excludes index derivatives such as NIFTY options.
    work = work.loc[
        (work["EXCH_ID"].str.upper() == "NSE")
        & (work["SEGMENT"].str.upper() == "D")
        & (work["INSTRUMENT"].str.upper().isin(["OPTSTK", "FUTSTK"]))
        & ((work["UNDERLYING_SYMBOL"] != "") | (work["SYMBOL_NAME"] != ""))
    ].copy()
    # Current Dhan files give the base stock directly in UNDERLYING_SYMBOL. The
    # suffix-stripping fallback keeps older or hand-built test files usable.
    work["symbol"] = work["UNDERLYING_SYMBOL"].where(
        work["UNDERLYING_SYMBOL"] != "",
        work["SYMBOL_NAME"].map(strip_fno_suffix),
    )
    work["symbol"] = work["symbol"].fillna("").astype(str).str.upper().str.strip()
    work = work.loc[(work["symbol"] != "") & (~work["symbol"].str.contains("TEST", case=False, na=False))]

    universe = pd.DataFrame({"symbol": sorted(work["symbol"].dropna().unique())})
    # Merge back to the cash-equity lookup so daily candle requests use
    # NSE_EQ/EQUITY security IDs instead of derivative contract IDs.
    universe = universe.merge(equity_lookup, on="symbol", how="left")
    universe["universe"] = "fno"
    universe["universe_name"] = UNIVERSE_CONFIG["fno"]["display_name"]
    universe["source"] = DHAN_SCRIP_MASTER_URL
    return finalize_universe(universe)


def build_index_universe(
    universe_key: str,
    source_df: pd.DataFrame,
    equity_lookup: pd.DataFrame,
    source_url: str | None = None,
) -> pd.DataFrame:
    """Build a NIFTY constituent universe from an official constituent CSV."""
    if universe_key not in UNIVERSE_CONFIG:
        raise KeyError(f"Unknown universe key: {universe_key}")

    # NIFTY constituent files use friendly column names like "Symbol" and
    # "Company Name". Lower-casing lets us accept small capitalization changes.
    normalized_columns = {str(column).strip().lower(): column for column in source_df.columns}
    symbol_col = normalized_columns.get("symbol")
    company_col = normalized_columns.get("company name")
    series_col = normalized_columns.get("series")
    if symbol_col is None:
        raise ValueError("Constituent CSV is missing a Symbol column")

    # Start with the official index symbols, then attach Dhan security IDs from
    # the instrument master. Keeping missing mappings is useful for debugging.
    universe = pd.DataFrame(
        {
            "symbol": source_df[symbol_col].fillna("").astype(str).str.upper().str.strip(),
            "source_company_name": (
                source_df[company_col].fillna("").astype(str).str.strip() if company_col else ""
            ),
            "source_series": (
                source_df[series_col].fillna("").astype(str).str.strip() if series_col else ""
            ),
        }
    )
    universe = universe.loc[universe["symbol"] != ""].drop_duplicates(subset=["symbol"])
    universe = universe.merge(equity_lookup, on="symbol", how="left")
    # Prefer Dhan's company/series details when available, but fall back to the
    # official NIFTY constituent file for symbols that did not map cleanly.
    universe["company_name"] = universe["company_name"].where(
        universe["company_name"].fillna("").astype(str).str.strip() != "",
        universe["source_company_name"],
    )
    universe["series"] = universe["series"].where(
        universe["series"].fillna("").astype(str).str.strip() != "",
        universe["source_series"],
    )
    universe["universe"] = universe_key
    universe["universe_name"] = UNIVERSE_CONFIG[universe_key]["display_name"]
    universe["source"] = source_url or UNIVERSE_CONFIG[universe_key]["source_url"]
    return finalize_universe(universe)


def load_symbol_list_csv(source_path: Path | str) -> list[str]:
    """Read the raw symbols from a local universe-list CSV.

    Hemant CSVs can exist in two useful shapes:
    - before the first refresh: a tiny one-column file with `symbol`
    - after refresh: the full generated universe CSV, which also has
      `source_symbol`

    When `source_symbol` exists, prefer it because it preserves the original
    Google Doc token such as `UTLTRACEMCO`. The generated `symbol` column may
    already contain the Dhan-friendly alias `ULTRACEMCO`; using `source_symbol`
    lets later refreshes keep showing what the pinned source list actually said.
    """
    path = Path(source_path)
    if not path.exists():
        raise FileNotFoundError(f"Universe source CSV not found: {path}")

    source_df = pd.read_csv(path, dtype=str).fillna("")
    normalized_columns = {str(column).strip().lower(): column for column in source_df.columns}
    source_symbol_col = normalized_columns.get("source_symbol")
    symbol_col = normalized_columns.get("symbol")

    if source_symbol_col is not None:
        source_symbols = source_df[source_symbol_col].astype(str)
        if symbol_col is not None:
            # A row-level fallback makes hand-edited generated CSVs forgiving:
            # if someone leaves source_symbol blank but symbol populated, we
            # can still refresh that row instead of failing the entire file.
            fallback_symbols = source_df[symbol_col].astype(str)
            source_symbols = source_symbols.where(source_symbols.str.strip() != "", fallback_symbols)
        return source_symbols.tolist()

    if symbol_col is None:
        raise ValueError(f"{path} is missing required column: symbol or source_symbol")

    return source_df[symbol_col].tolist()


def normalize_source_symbol(raw_symbol: str) -> str:
    """Return a clean NSE token while preserving the source's symbol spelling."""
    symbol = str(raw_symbol or "").upper().strip()
    symbol = re.sub(r"^NSE\s*:\s*", "", symbol, flags=re.IGNORECASE)
    symbol = symbol.strip(" ,\t\r\n")
    return re.sub(r"\s+", "", symbol)


def normalize_manual_symbol(raw_symbol: str) -> str:
    """Return the Dhan lookup symbol for one pinned/manual source token."""
    symbol = normalize_source_symbol(raw_symbol)
    return MANUAL_SYMBOL_ALIASES.get(symbol, symbol)


def build_symbol_list_universe(
    universe_key: str,
    raw_symbols: Iterable[str],
    equity_lookup: pd.DataFrame,
    source: str | None = None,
) -> pd.DataFrame:
    """Build a universe from a CSV-backed list of NSE symbols."""
    if universe_key not in UNIVERSE_CONFIG:
        raise KeyError(f"Unknown universe key: {universe_key}")

    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for raw_symbol in raw_symbols:
        # `source_symbol` is kept for audit/debugging. `symbol` is the value we
        # actually use to join to Dhan. For most rows they match; alias rows are
        # where preserving both values helps explain what happened.
        source_symbol = normalize_source_symbol(raw_symbol)
        symbol = normalize_manual_symbol(raw_symbol)
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        rows.append({"symbol": symbol, "source_symbol": source_symbol})

    universe = pd.DataFrame(rows, columns=["symbol", "source_symbol"])
    # The manual CSV only knows stock symbols. This merge attaches Dhan's
    # security_id/exchange_segment/instrument_type so the existing candle loader
    # can fetch data exactly like it does for NIFTY and F&O universes.
    universe = universe.merge(equity_lookup, on="symbol", how="left")
    universe["universe"] = universe_key
    universe["universe_name"] = UNIVERSE_CONFIG[universe_key]["display_name"]
    universe["source"] = source or UNIVERSE_CONFIG[universe_key].get("source_file", "")
    # Do not alphabetize custom source lists. Their order comes from the pinned
    # CSV snapshot and may be meaningful to the user reviewing the list.
    return finalize_universe(universe, sort_symbols=False)


def finalize_universe(universe: pd.DataFrame, sort_symbols: bool = True) -> pd.DataFrame:
    """Return a consistent universe CSV shape for all universe builders."""
    # All universe CSVs should have the same columns. That lets loaders and
    # screeners treat NIFTY 100, NIFTY 500, and F&O files the same way.
    for column in ("security_id", "exchange_segment", "instrument_type", "company_name", "series"):
        if column not in universe.columns:
            universe[column] = ""
        universe[column] = universe[column].fillna("").astype(str)

    # A missing security_id means we know the symbol exists in the source list,
    # but cannot fetch its daily candles from Dhan until the mapping is fixed.
    # Keeping that row in the CSV is friendlier than silently dropping it:
    # the status table can still show that a source symbol needs attention.
    universe["mapping_status"] = universe["security_id"].fillna("").astype(str).str.strip().ne("").map(
        lambda value: "mapped" if value else "missing_security_id"
    )
    columns = [
        "universe",
        "universe_name",
        "symbol",
        "security_id",
        "exchange_segment",
        "instrument_type",
        "company_name",
        "series",
        "source",
        "mapping_status",
    ]
    if "source_symbol" in universe.columns:
        # Custom CSV-backed universes include this extra column so alias changes
        # stay visible after refresh. Standard NIFTY/F&O files do not need it.
        columns.append("source_symbol")

    result = universe[columns]
    if sort_symbols:
        result = result.sort_values("symbol")
    return result.reset_index(drop=True)


def universe_file_path(universe_key: str, universe_dir: Path | str = UNIVERSE_DIR) -> Path:
    """Return where a universe CSV should live on disk."""
    return Path(universe_dir) / UNIVERSE_CONFIG[universe_key]["file_name"]


def refresh_universe_files(
    universe_keys: Iterable[str] | None = None,
    universe_dir: Path | str = UNIVERSE_DIR,
    instrument_master: pd.DataFrame | None = None,
    index_sources: dict[str, pd.DataFrame] | None = None,
) -> dict[str, Path]:
    """
    Refresh requested universe CSV files.

    Tests can pass data frames directly. The Streamlit app uses the default
    behavior, which downloads current constituent files and the Dhan master.
    """
    keys = list(universe_keys or UNIVERSE_CONFIG.keys())
    unknown = sorted(set(keys) - set(UNIVERSE_CONFIG))
    if unknown:
        raise KeyError(f"Unknown universe key(s): {', '.join(unknown)}")

    output_dir = Path(universe_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # The Dhan instrument master is downloaded once and reused for every
    # universe so all files are mapped against the same snapshot.
    master = instrument_master if instrument_master is not None else load_instrument_master()
    equity_lookup = build_equity_lookup(master)
    index_sources = index_sources or {}

    written: dict[str, Path] = {}
    for key in keys:
        if key == "fno":
            # F&O comes entirely from Dhan's instrument master because it is a
            # derivatives universe rather than a NIFTY constituent CSV.
            universe_df = build_fno_universe(master, equity_lookup)
        elif "union_of" in UNIVERSE_CONFIG[key]:
            # Composite universe: concatenate the member lists' source symbols
            # and let `build_symbol_list_universe` dedupe (by normalized symbol)
            # and map them against the same Dhan snapshot as everyone else.
            member_symbols: list[str] = []
            for member_key in UNIVERSE_CONFIG[key]["union_of"]:
                member_source = UNIVERSE_CONFIG[member_key].get("source_file")
                if member_source:
                    member_symbols.extend(load_symbol_list_csv(member_source))
            universe_df = build_symbol_list_universe(
                universe_key=key,
                raw_symbols=member_symbols,
                equity_lookup=equity_lookup,
                source=" + ".join(UNIVERSE_CONFIG[key]["union_of"]),
            )
        elif "source_file" in UNIVERSE_CONFIG[key]:
            # Hemant-style custom universes come from local CSVs instead of a
            # remote download. They are then mapped against the same Dhan master
            # snapshot as every other universe, keeping security IDs consistent.
            source_file = Path(UNIVERSE_CONFIG[key]["source_file"])
            universe_df = build_symbol_list_universe(
                universe_key=key,
                raw_symbols=load_symbol_list_csv(source_file),
                equity_lookup=equity_lookup,
                source=str(source_file),
            )
        else:
            source_df = index_sources.get(key)
            if source_df is None:
                # NIFTY 100/500 membership comes from official constituent CSVs,
                # then we map those symbols to Dhan's IDs.
                source_df = download_csv(UNIVERSE_CONFIG[key]["source_url"])
            universe_df = build_index_universe(
                universe_key=key,
                source_df=source_df,
                equity_lookup=equity_lookup,
                source_url=UNIVERSE_CONFIG[key]["source_url"],
            )

        path = universe_file_path(key, output_dir)
        universe_df.to_csv(path, index=False)
        written[key] = path

    return written
