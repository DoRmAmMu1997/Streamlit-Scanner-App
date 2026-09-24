"""Shared finite-Decimal parsing regressions for numeric consumers.

Beginner note:
Ranking, notifications, comparisons, persistence, and validation all consume
numbers from differently shaped inputs. These tests keep one shared rule at
their boundary: usable values become exact finite ``Decimal`` objects, while
booleans, malformed text, and NaN/Infinity behave as missing data.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from backend.numeric import finite_decimal
from backend.validation._pricing import as_money


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (Decimal("12.340"), Decimal("12.340")),
        (7, Decimal("7")),
        (2.5, Decimal("2.5")),
        (" 9.75 ", Decimal("9.75")),
        (None, None),
        (True, None),
        (False, None),
        (float("nan"), None),
        (float("inf"), None),
        (float("-inf"), None),
        (Decimal("NaN"), None),
        ("Infinity", None),
        ("not-a-number", None),
    ],
)
def test_finite_decimal_preserves_finite_values_and_rejects_unsafe_values(value, expected):
    """Changing the common parser must preserve exact finite semantics."""
    assert finite_decimal(value) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12.34567", Decimal("12.3457")), (12, Decimal("12.0000"))],
)
def test_finite_values_keep_exact_and_money_semantics(value, expected):
    """The shared parser keeps exact values; money adds only 4 dp quantization."""
    assert finite_decimal(value) == Decimal(str(value))
    assert as_money(value) == expected


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")])
def test_as_money_rejects_non_finite_prices_before_quantizing(value):
    """A non-finite price must take validation's missing-data path."""
    assert as_money(value) is None
