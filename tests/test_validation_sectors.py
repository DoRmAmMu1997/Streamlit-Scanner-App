"""VALID-004 local sector metadata helper tests."""

from __future__ import annotations

import pandas as pd

from backend.validation.sectors import load_universe_sector_lookup


def test_load_universe_sector_lookup_reads_first_available_local_sector_column():
    def universe_loader(universe_key: str) -> pd.DataFrame:
        assert universe_key == "nifty_500"
        return pd.DataFrame(
            [
                {"symbol": "RELIANCE", "industry": "Energy"},
                {"symbol": "TCS", "industry": "Technology"},
                {"symbol": "BLANK", "industry": ""},
            ]
        )

    lookup = load_universe_sector_lookup(["nifty_500"], universe_loader=universe_loader)

    assert lookup == {
        ("nifty_500", "RELIANCE"): "Energy",
        ("nifty_500", "TCS"): "Technology",
    }


def test_load_universe_sector_lookup_returns_empty_mapping_without_metadata():
    lookup = load_universe_sector_lookup(
        ["nifty_500"],
        universe_loader=lambda _key: pd.DataFrame([{"symbol": "RELIANCE"}]),
    )

    assert lookup == {}
