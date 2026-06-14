from __future__ import annotations

import pandas as pd

from ui.common import _csv_safe, _drop_provenance


def test_drop_provenance_hides_legacy_and_canonical_receipt_columns():
    original = pd.DataFrame(
        [
            {
                "symbol": "DEMO",
                "rating": "BUY",
                "provenance": {"triggered_rules": ["rule"]},
                "provenance_json": {"ai": {"model_name": "test-model"}},
            }
        ]
    )

    display = _drop_provenance(original)

    assert list(display.columns) == ["symbol", "rating"]
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
