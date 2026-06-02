"""Tests for building scanner universe CSVs from small fake source data."""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from backend import universe_builder
from backend.universe_builder import (
    HEMANT_SOURCE_FILES,
    build_equity_lookup,
    build_fno_universe,
    build_index_universe,
    build_symbol_list_universe,
    load_symbol_list_csv,
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


def test_build_symbol_list_universe_maps_aliases_and_preserves_order():
    equity_lookup = pd.DataFrame(
        [
            {
                "symbol": "NAM-INDIA",
                "security_id": "357",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Nippon Life India AMC",
                "series": "EQ",
            },
            {
                "symbol": "TCS",
                "security_id": "11536",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "Tata Consultancy Services",
                "series": "EQ",
            },
            {
                "symbol": "ULTRACEMCO",
                "security_id": "11532",
                "exchange_segment": "NSE_EQ",
                "instrument_type": "EQUITY",
                "company_name": "UltraTech Cement",
                "series": "EQ",
            },
        ]
    )

    universe = build_symbol_list_universe(
        "hemant_super_45",
        ["NSE:NAM_INDIA", "TCS", "UTLTRACEMCO", "AKZOINDIA"],
        equity_lookup,
        source="memory://hemant",
    )

    assert universe["symbol"].tolist() == ["NAM-INDIA", "TCS", "ULTRACEMCO", "AKZOINDIA"]
    assert universe.loc[0, "source_symbol"] == "NAM_INDIA"
    assert universe.loc[2, "source_symbol"] == "UTLTRACEMCO"
    assert universe.loc[0, "security_id"] == "357"
    assert universe.loc[3, "security_id"] == ""
    assert universe.loc[3, "mapping_status"] == "missing_security_id"


def test_load_symbol_list_csv_reads_symbol_column(tmp_path):
    source_path = tmp_path / "symbols.csv"
    pd.DataFrame({"symbol": ["NSE:NAM_INDIA", "TCS", " UTLTRACEMCO "]}).to_csv(
        source_path, index=False
    )

    symbols = load_symbol_list_csv(source_path)

    assert symbols == ["NSE:NAM_INDIA", "TCS", " UTLTRACEMCO "]


def test_load_symbol_list_csv_prefers_source_symbol_from_generated_csv(tmp_path):
    source_path = tmp_path / "generated_hemant.csv"
    # This shape mirrors a Hemant file after refresh: `symbol` is the Dhan-ready
    # value, while `source_symbol` preserves the original Google Doc spelling.
    # The loader should use source_symbol so a second refresh keeps alias audit
    # history instead of slowly replacing it with Dhan symbols.
    pd.DataFrame(
        {
            "symbol": ["NAM-INDIA", "ULTRACEMCO", "TCS"],
            "source_symbol": ["NAM_INDIA", "UTLTRACEMCO", ""],
        }
    ).to_csv(source_path, index=False)

    symbols = load_symbol_list_csv(source_path)

    assert symbols == ["NAM_INDIA", "UTLTRACEMCO", "TCS"]


def test_hemant_source_csvs_are_pinned_from_google_doc_snapshot():
    assert HEMANT_SOURCE_FILES["hemant_super_45"].parent.name == "universes"
    assert HEMANT_SOURCE_FILES["hemant_super_45"].parent.parent.name == "data"
    for universe_key, source_path in HEMANT_SOURCE_FILES.items():
        config = universe_builder.UNIVERSE_CONFIG[universe_key]
        assert config["source_file"] == str(source_path)
        assert "source_url" not in config

    super_45 = load_symbol_list_csv(HEMANT_SOURCE_FILES["hemant_super_45"])
    good_45 = load_symbol_list_csv(HEMANT_SOURCE_FILES["hemant_good_45"])
    good_200 = load_symbol_list_csv(HEMANT_SOURCE_FILES["hemant_good_200"])

    assert len(super_45) == 43
    assert len(good_45) == 43
    assert len(good_200) == 261
    assert super_45[-3:] == [
        "BAJAJ_AUTO",
        "UTLTRACEMCO",
        "AMBUJACEM",
    ]
    assert good_200[-3:] == ["HDFCBANK", "SBIN", "BLS"]


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


def test_refresh_universe_files_writes_hemant_universe_from_csv_source(tmp_path):
    # The Hemant source file lives in data/universes, but the test writes output
    # to tmp_path so it never overwrites the checked-in source snapshot.
    written = refresh_universe_files(
        universe_keys=["hemant_super_45"],
        universe_dir=tmp_path,
        instrument_master=fake_instrument_master(),
    )

    assert written["hemant_super_45"].exists()
    saved = pd.read_csv(written["hemant_super_45"], dtype=str).fillna("")
    assert saved["universe"].unique().tolist() == ["hemant_super_45"]
    assert saved["universe_name"].unique().tolist() == ["Hemant Super 45"]
    assert saved["symbol"].tolist()[:3] == ["HDFCBANK", "ICICIBANK", "AXISBANK"]
    assert saved.loc[saved["symbol"].eq("TCS"), "security_id"].item() == "11536"
    assert saved.loc[saved["symbol"].eq("HDFCBANK"), "mapping_status"].item() == "missing_security_id"


def test_refresh_universe_files_builds_hemant_super_good_union(tmp_path):
    # The composite universe reads BOTH member source lists, concatenates them,
    # dedupes by symbol, and maps against the same Dhan master. Output goes to
    # tmp_path so the checked-in source snapshots are never overwritten.
    written = refresh_universe_files(
        universe_keys=["hemant_super_good_union"],
        universe_dir=tmp_path,
        instrument_master=fake_instrument_master(),
    )

    assert written["hemant_super_good_union"].exists()
    saved = pd.read_csv(written["hemant_super_good_union"], dtype=str).fillna("")
    assert saved["universe"].unique().tolist() == ["hemant_super_good_union"]
    assert saved["universe_name"].unique().tolist() == ["Hemant Super + Good 45"]
    # The union is deduped: a symbol present in both member lists appears once.
    assert saved["symbol"].is_unique
    # TCS lives in the Hemant Super 45 source list and maps via the fake master.
    assert saved.loc[saved["symbol"].eq("TCS"), "security_id"].item() == "11536"
    # The union is at least as large as one member list (43) and no larger than
    # both stacked (86) — i.e. real overlap was collapsed, nothing was dropped.
    assert 43 <= len(saved) <= 86


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


def test_download_csv_refuses_advertised_content_length_over_cap(monkeypatch):
    """An oversized Content-Length should abort before reading body chunks."""

    class OversizedResponse:
        headers = {"Content-Length": "999"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def raise_for_status(self):
            pass

        def iter_content(self, chunk_size=1):
            pytest.fail("download_csv should reject before streaming the body")

    monkeypatch.setattr(
        universe_builder.requests,
        "get",
        lambda *args, **kwargs: OversizedResponse(),
    )

    with pytest.raises(ValueError, match="advertised size"):
        universe_builder.download_csv("https://example.com/large.csv", max_bytes=10)


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
