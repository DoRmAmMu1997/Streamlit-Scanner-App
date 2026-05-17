from __future__ import annotations

"""Tests for building scanner universe CSVs from small fake source data."""

from datetime import date

import pandas as pd

from backend import universe_builder
from backend.universe_builder import (
    build_equity_lookup,
    build_fno_universe,
    build_index_universe,
    load_instrument_master,
    normalize_instrument_master_columns,
    refresh_universe_files,
)


def fake_instrument_master() -> pd.DataFrame:
    """Return the minimum Dhan master rows needed by universe-builder tests."""
    return pd.DataFrame(
        [
            {
                "EXCH_ID": "NSE",
                "SEGMENT": "E",
                "SECURITY_ID": "2885",
                "INSTRUMENT": "EQUITY",
                "UNDERLYING_SYMBOL": "RELIANCE",
                "SYMBOL_NAME": "Reliance Industries",
                "DISPLAY_NAME": "Reliance Industries",
                "SERIES": "EQ",
            },
            {
                "EXCH_ID": "NSE",
                "SEGMENT": "E",
                "SECURITY_ID": "11536",
                "INSTRUMENT": "EQUITY",
                "UNDERLYING_SYMBOL": "TCS",
                "SYMBOL_NAME": "Tata Consultancy Services",
                "DISPLAY_NAME": "Tata Consultancy Services",
                "SERIES": "EQ",
            },
            {
                "EXCH_ID": "NSE",
                "SEGMENT": "D",
                "SECURITY_ID": "999",
                "INSTRUMENT": "FUTSTK",
                "UNDERLYING_SYMBOL": "RELIANCE",
                "SYMBOL_NAME": "RELIANCE-May2026-FUT",
                "DISPLAY_NAME": "RELIANCE MAY FUT",
                "SERIES": "",
            },
        ]
    )


def legacy_prefixed_instrument_master() -> pd.DataFrame:
    """Return old-style Dhan rows so the normalizer proves backward compatibility."""
    return pd.DataFrame(
        [
            {
                "SEM_EXM_EXCH_ID": "NSE",
                "SEM_SEGMENT": "E",
                "SEM_INSTRUMENT_NAME": "EQUITY",
                "SEM_SMST_SECURITY_ID": "2885",
                "SEM_TRADING_SYMBOL": "RELIANCE",
                "SEM_CUSTOM_SYMBOL": "Reliance Industries",
                "SM_SYMBOL_NAME": "Reliance Industries",
                "SEM_SERIES": "EQ",
            },
            {
                "SEM_EXM_EXCH_ID": "NSE",
                "SEM_SEGMENT": "D",
                "SEM_INSTRUMENT_NAME": "FUTSTK",
                "SEM_SMST_SECURITY_ID": "999",
                "SEM_TRADING_SYMBOL": "RELIANCE-MAY2026-FUT",
                "SEM_CUSTOM_SYMBOL": "",
                "SM_SYMBOL_NAME": "",
                "SEM_SERIES": "",
            },
        ]
    )


def test_normalize_instrument_master_columns_removes_sem_prefixes():
    normalized = normalize_instrument_master_columns(legacy_prefixed_instrument_master())

    assert "SEM_EXM_EXCH_ID" not in normalized.columns
    assert {"EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "UNDERLYING_SYMBOL"}.issubset(
        normalized.columns
    )
    assert normalized.iloc[0]["EXCH_ID"] == "NSE"
    assert normalized.iloc[0]["SECURITY_ID"] == "2885"
    assert normalized.iloc[0]["UNDERLYING_SYMBOL"] == "RELIANCE"
    assert normalized.iloc[1]["UNDERLYING_SYMBOL"] == "RELIANCE"


def test_build_index_universe_maps_symbols_to_dhan_security_ids():
    # The NIFTY source file contains symbols; the Dhan master supplies the
    # security_id/exchange_segment needed for API calls.
    equity_lookup = build_equity_lookup(fake_instrument_master())
    source = pd.DataFrame(
        {
            "Symbol": ["RELIANCE", "TCS", "MISSING"],
            "Company Name": ["Reliance", "TCS", "Missing Co"],
            "Series": ["EQ", "EQ", "EQ"],
        }
    )

    universe = build_index_universe("nifty_100", source, equity_lookup, "memory://nifty100")

    mapped = universe.set_index("symbol")
    assert mapped.loc["RELIANCE", "security_id"] == "2885"
    assert mapped.loc["TCS", "exchange_segment"] == "NSE_EQ"
    # Missing symbols stay in the universe CSV so the user can see mapping gaps.
    assert mapped.loc["MISSING", "mapping_status"] == "missing_security_id"


def test_build_fno_universe_maps_derivative_symbols_to_cash_equities():
    # F&O symbols are discovered from derivative rows, then mapped back to the
    # cash-equity security_id because screeners fetch daily stock candles.
    equity_lookup = build_equity_lookup(fake_instrument_master())

    universe = build_fno_universe(fake_instrument_master(), equity_lookup)

    assert universe["symbol"].tolist() == ["RELIANCE"]
    assert universe.iloc[0]["security_id"] == "2885"
    assert universe.iloc[0]["mapping_status"] == "mapped"


def test_build_fno_universe_prefers_underlying_symbol_over_contract_name():
    # Current Dhan rows expose the stock symbol directly in UNDERLYING_SYMBOL.
    # The contract name can contain expiry/strike text, so the builder should
    # prefer UNDERLYING_SYMBOL and only use suffix stripping as a fallback.
    master = fake_instrument_master().copy()
    master.loc[master["SEGMENT"].eq("D"), "SYMBOL_NAME"] = "THIS-NAME-SHOULD-NOT-BE-USED"
    equity_lookup = build_equity_lookup(master)

    universe = build_fno_universe(master, equity_lookup)

    assert universe["symbol"].tolist() == ["RELIANCE"]
    assert universe.iloc[0]["security_id"] == "2885"


def test_refresh_universe_files_writes_requested_csvs(tmp_path):
    # This uses injected fake DataFrames so the test does not download anything.
    source = pd.DataFrame({"Symbol": ["RELIANCE"], "Company Name": ["Reliance"], "Series": ["EQ"]})

    written = refresh_universe_files(
        universe_keys=["nifty_100"],
        universe_dir=tmp_path,
        instrument_master=fake_instrument_master(),
        index_sources={"nifty_100": source},
    )

    assert written["nifty_100"].exists()
    saved = pd.read_csv(written["nifty_100"], dtype=str)
    assert saved.iloc[0]["symbol"] == "RELIANCE"


def test_load_instrument_master_writes_dated_snapshot(tmp_path, monkeypatch):
    # The startup path downloads Dhan's master once and writes a dated local CSV.
    # The test injects fake data so it never touches the network.
    def fake_download_csv(url: str):
        return fake_instrument_master()

    monkeypatch.setattr(universe_builder, "download_csv", fake_download_csv)

    loaded = load_instrument_master(
        url="memory://dhan-master",
        save_snapshot=True,
        snapshot_dir=tmp_path,
        run_date=date(2026, 5, 14),
    )

    snapshot = tmp_path / "all_instrument 2026-05-14.csv"
    assert snapshot.exists()
    saved = pd.read_csv(snapshot, dtype=str)
    assert list(saved.columns) == list(loaded.columns)
    assert saved.iloc[0]["SECURITY_ID"] == "2885"


def test_union_of_mapped_universes_dedupes_by_security_id(tmp_path):
    """The union should yield one row per mapped security_id across all CSVs."""
    from backend.universe_loader import union_of_mapped_universes

    # Two universes share RELIANCE. Each universe also has its own unique
    # entries plus an unmapped row that should be excluded.
    nifty_100 = pd.DataFrame(
        [
            {
                "universe": "nifty_100",
                "universe_name": "NIFTY 100",
                "symbol": "RELIANCE",
                "security_id": "2885",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Reliance",
                "series": "EQ",
                "source": "memory://",
                "mapping_status": "mapped",
            },
            {
                "universe": "nifty_100",
                "universe_name": "NIFTY 100",
                "symbol": "TCS",
                "security_id": "11536",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "TCS",
                "series": "EQ",
                "source": "memory://",
                "mapping_status": "mapped",
            },
        ]
    )
    nifty_500 = pd.DataFrame(
        [
            {
                "universe": "nifty_500",
                "universe_name": "NIFTY 500",
                "symbol": "RELIANCE",
                "security_id": "2885",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Reliance",
                "series": "EQ",
                "source": "memory://",
                "mapping_status": "mapped",
            },
            {
                "universe": "nifty_500",
                "universe_name": "NIFTY 500",
                "symbol": "INFY",
                "security_id": "1594",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Infosys",
                "series": "EQ",
                "source": "memory://",
                "mapping_status": "mapped",
            },
            {
                "universe": "nifty_500",
                "universe_name": "NIFTY 500",
                "symbol": "BADMAP",
                "security_id": "",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Bad Map",
                "series": "",
                "source": "memory://",
                "mapping_status": "missing_security_id",
            },
        ]
    )
    nifty_100.to_csv(tmp_path / "nifty_100.csv", index=False)
    nifty_500.to_csv(tmp_path / "nifty_500.csv", index=False)
    # No F&O file on disk; loader should skip it without raising.

    union = union_of_mapped_universes(universe_dir=tmp_path)

    symbols = sorted(union["symbol"].tolist())
    assert symbols == ["INFY", "RELIANCE", "TCS"]
    # The unmapped BADMAP row must not appear.
    assert "BADMAP" not in symbols
    # And RELIANCE should appear exactly once even though it was in two CSVs.
    assert int((union["symbol"] == "RELIANCE").sum()) == 1
