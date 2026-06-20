"""Tests for VALID-002B benchmark index-instrument configuration.

Two layers:
- ``resolve_index_security_ids`` is pure: it reads verified Dhan ``IDX_I`` index
  ids out of a (synthetic) instrument-master DataFrame, so it tests without any
  network or real master file.
- ``benchmark_for_universe`` / ``load_benchmarks`` read the committed
  ``config/benchmarks.yaml`` so the shipped mapping stays valid and graceful-null
  behaviour is preserved when an id is blank or the config is absent.
"""

from __future__ import annotations

import pandas as pd

from backend.validation import benchmarks as bm
from backend.validation.benchmarks import (
    BenchmarkSpec,
    benchmark_for_universe,
    load_benchmarks,
    resolve_index_security_ids,
)


def _master(rows: list[dict[str, str]]) -> pd.DataFrame:
    """Build a minimal instrument-master frame with the columns the resolver reads."""
    columns = ["EXCH_ID", "SEGMENT", "SECURITY_ID", "INSTRUMENT", "SYMBOL_NAME", "DISPLAY_NAME"]
    return pd.DataFrame(rows, columns=columns)


# A representative master: the three wanted NSE indices (note NIFTY 50's trading
# symbol is "NIFTY" while its display name is "Nifty 50"), plus distractors that
# must NOT match — another NSE index, a BSE index, and an NSE *equity* ETF whose
# name contains "NIFTY 50".
_REPRESENTATIVE_ROWS = [
    {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "13", "INSTRUMENT": "INDEX",
     "SYMBOL_NAME": "NIFTY", "DISPLAY_NAME": "Nifty 50"},
    {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "17", "INSTRUMENT": "INDEX",
     "SYMBOL_NAME": "NIFTY 100", "DISPLAY_NAME": "NIFTY 100"},
    {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "19", "INSTRUMENT": "INDEX",
     "SYMBOL_NAME": "NIFTY 500", "DISPLAY_NAME": "NIFTY 500"},
    {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "18", "INSTRUMENT": "INDEX",
     "SYMBOL_NAME": "NIFTY 200", "DISPLAY_NAME": "NIFTY 200"},
    {"EXCH_ID": "BSE", "SEGMENT": "I", "SECURITY_ID": "999", "INSTRUMENT": "INDEX",
     "SYMBOL_NAME": "SENSEX 50", "DISPLAY_NAME": "Nifty 50"},
    {"EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "10176", "INSTRUMENT": "EQUITY",
     "SYMBOL_NAME": "SETFNIF50", "DISPLAY_NAME": "SBI Nifty 50 ETF"},
]


def test_resolve_index_security_ids_reads_verified_ids():
    """The three NSE index ids are resolved by symbol or display name."""
    resolved = resolve_index_security_ids(_master(_REPRESENTATIVE_ROWS))

    assert resolved == {"NIFTY 50": "13", "NIFTY 100": "17", "NIFTY 500": "19"}


def test_resolve_index_security_ids_ignores_non_nse_and_non_index_rows():
    """A BSE index and an NSE equity ETF named like the index must not match."""
    rows = [
        {"EXCH_ID": "BSE", "SEGMENT": "I", "SECURITY_ID": "999", "INSTRUMENT": "INDEX",
         "SYMBOL_NAME": "NIFTY", "DISPLAY_NAME": "Nifty 50"},
        {"EXCH_ID": "NSE", "SEGMENT": "E", "SECURITY_ID": "10176", "INSTRUMENT": "EQUITY",
         "SYMBOL_NAME": "SETFNIF50", "DISPLAY_NAME": "SBI Nifty 50 ETF"},
    ]

    assert resolve_index_security_ids(_master(rows), wanted=("NIFTY 50",)) == {}


def test_resolve_index_security_ids_omits_absent_symbol():
    """A requested index that is not in the master is left out (graceful-null)."""
    resolved = resolve_index_security_ids(
        _master(_REPRESENTATIVE_ROWS), wanted=("NIFTY 50", "NIFTY BANK")
    )

    assert resolved == {"NIFTY 50": "13"}
    assert "NIFTY BANK" not in resolved


def test_resolve_index_security_ids_omits_ambiguous_match():
    """Two NSE index rows with the same name but different ids resolve to neither."""
    rows = [
        {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "13", "INSTRUMENT": "INDEX",
         "SYMBOL_NAME": "NIFTY", "DISPLAY_NAME": "Nifty 50"},
        {"EXCH_ID": "NSE", "SEGMENT": "I", "SECURITY_ID": "1300", "INSTRUMENT": "INDEX",
         "SYMBOL_NAME": "NIFTY", "DISPLAY_NAME": "Nifty 50"},
    ]

    assert resolve_index_security_ids(_master(rows), wanted=("NIFTY 50",)) == {}


def test_committed_config_ships_verified_ids():
    """The committed config maps the universes to the verified IDX_I ids."""
    assert bm.BENCHMARKS["fno"].security_id == "13"
    assert bm.BENCHMARKS["fno"].key == "nifty_50"
    assert bm.BENCHMARKS["nifty_100"].security_id == "17"
    assert bm.BENCHMARKS["nifty_500"].security_id == "19"
    # Every hemant_* universe benchmarks against NIFTY 50.
    assert bm.BENCHMARKS["hemant_super_45"].security_id == "13"


def test_benchmark_for_universe_returns_resolved_spec():
    """A configured universe yields a usable IDX_I BenchmarkSpec."""
    spec = benchmark_for_universe("nifty_500")

    assert spec is not None
    assert spec.key == "nifty_500"
    assert spec.symbol == "NIFTY 500"
    assert spec.security_id == "19"
    assert spec.exchange_segment == "IDX_I"
    assert spec.instrument_type == "INDEX"


def test_benchmark_for_universe_unknown_universe_is_none():
    """An unmapped universe key has no benchmark."""
    assert benchmark_for_universe("totally_unknown_universe") is None


def test_benchmark_for_universe_blank_id_is_none(monkeypatch):
    """A configured-but-unresolved (blank id) entry keeps graceful-null behaviour."""
    monkeypatch.setitem(
        bm.BENCHMARKS,
        "pending_universe",
        BenchmarkSpec(key="nifty_50", symbol="NIFTY 50", security_id=""),
    )

    assert benchmark_for_universe("pending_universe") is None


def test_load_benchmarks_missing_file_is_graceful(tmp_path):
    """A missing config file disables benchmarks rather than raising."""
    assert load_benchmarks(tmp_path / "no_such_benchmarks.yaml") == {}


def test_load_benchmarks_malformed_file_is_graceful(tmp_path):
    """Malformed YAML (no 'benchmarks' mapping) disables benchmarks, no exception."""
    bad = tmp_path / "benchmarks.yaml"
    bad.write_text("benchmarks: not-a-mapping\n", encoding="utf-8")

    assert load_benchmarks(bad) == {}


def test_load_benchmarks_treats_explicit_null_scalars_as_blank(tmp_path):
    """YAML null placeholders must keep graceful-null benchmark behaviour."""
    config = tmp_path / "benchmarks.yaml"
    config.write_text(
        """
        benchmarks:
          null_id:
            key: nifty_50
            symbol: "NIFTY 50"
            security_id:
          null_symbol:
            key: nifty_50
            symbol:
            security_id: "13"
          null_key:
            key:
            symbol: "NIFTY 50"
            security_id: "13"
        """,
        encoding="utf-8",
    )

    specs = load_benchmarks(config)

    assert specs["null_id"].security_id == ""
    assert "null_symbol" not in specs
    assert specs["null_key"].key == "null_key"


def test_load_benchmarks_keeps_blank_id_entries(tmp_path):
    """An entry with a blank id parses (its symbol is kept) but stays unusable."""
    config = tmp_path / "benchmarks.yaml"
    config.write_text(
        """
        benchmarks:
          some_universe:
            key: nifty_50
            symbol: "NIFTY 50"
            security_id: ""
        """,
        encoding="utf-8",
    )

    specs = load_benchmarks(config)

    assert specs["some_universe"].symbol == "NIFTY 50"
    assert specs["some_universe"].security_id == ""
