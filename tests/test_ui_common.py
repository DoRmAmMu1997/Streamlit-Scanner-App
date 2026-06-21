from __future__ import annotations

import pandas as pd

from ui.common import (
    _csv_safe,
    _drop_provenance,
    _score_components_frame,
    _sort_results_by_final_score,
)


def test_drop_provenance_hides_legacy_and_canonical_receipt_columns():
    original = pd.DataFrame(
        [
            {
                "symbol": "DEMO",
                "rating": "BUY",
                "final_score": 87.06,
                "provenance": {"triggered_rules": ["rule"]},
                "provenance_json": {"ai": {"model_name": "test-model"}},
                "score_breakdown": {"components": {"technical": 50.0}},
            }
        ]
    )

    display = _drop_provenance(original)

    assert list(display.columns) == ["symbol", "rating", "final_score"]
    assert {"provenance", "provenance_json"}.issubset(original.columns)


def test_csv_safe_escapes_formula_prefixes_in_pandas_string_dtype():
    original = pd.DataFrame(
        {
            "reason": pd.Series(
                ["=2+2", "+SUM(A1:A2)", "normal text"],
                dtype="string",
            )
        }
    )

    safe = _csv_safe(original)

    assert safe["reason"].tolist() == ["'=2+2", "'+SUM(A1:A2)", "normal text"]
    assert original["reason"].tolist() == ["=2+2", "+SUM(A1:A2)", "normal text"]


def test_sort_results_by_final_score_descending_with_nulls_last():
    original = pd.DataFrame(
        [
            {"symbol": "LOW", "final_score": 10.0},
            {"symbol": "NONE", "final_score": None},
            {"symbol": "HIGH", "final_score": 90.0},
        ]
    )

    sorted_frame = _sort_results_by_final_score(original)

    assert sorted_frame["symbol"].tolist() == ["HIGH", "LOW", "NONE"]
    assert original["symbol"].tolist() == ["LOW", "NONE", "HIGH"]


def test_score_components_frame_extracts_breakdown_from_provenance():
    results = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "final_score": 87.0,
                "provenance": {
                    "score_breakdown": {
                        "components": {
                            "technical": 100.0,
                            "liquidity": 60.0,
                            "risk": 80.0,
                            "freshness": 100.0,
                        },
                        "coverage": ["technical", "liquidity", "risk", "freshness"],
                        "missing": [],
                    }
                },
            }
        ]
    )

    frame = _score_components_frame(results)

    assert list(frame.columns) == [
        "Symbol",
        "Final score",
        "Technical",
        "Liquidity",
        "Risk",
        "Freshness",
        "Coverage",
        "Missing",
    ]
    assert frame.iloc[0].to_dict() == {
        "Symbol": "AAA",
        "Final score": 87.0,
        "Technical": 100.0,
        "Liquidity": 60.0,
        "Risk": 80.0,
        "Freshness": 100.0,
        "Coverage": "technical, liquidity, risk, freshness",
        "Missing": "",
    }
