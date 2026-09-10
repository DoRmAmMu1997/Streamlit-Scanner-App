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

from backend.notifications.report import _finite_decimal as report_finite_decimal
from backend.numeric import finite_decimal
from backend.scanning.comparison import _decimal_or_none
from backend.storage.repository import _finite_decimal as repository_finite_decimal
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


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf"), "NaN", "Infinity"])
def test_compatibility_helpers_reject_non_finite_values(value):
    """Existing private import paths keep their finite-only behavior."""
    assert _decimal_or_none(value) is None
    assert repository_finite_decimal(value) is None
    assert report_finite_decimal(value) is None


@pytest.mark.parametrize(
    ("value", "expected"),
    [("12.34567", Decimal("12.3457")), (12, Decimal("12.0000"))],
)
def test_compatibility_helpers_preserve_finite_numeric_semantics(value, expected):
    """Delegating to the leaf parser cannot change valid existing values."""
    assert _decimal_or_none(value) == Decimal(str(value))
    assert repository_finite_decimal(value) == Decimal(str(value))
    assert report_finite_decimal(value) == Decimal(str(value))
    assert as_money(value) == expected


@pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), float("nan"), float("inf")])
def test_as_money_rejects_non_finite_prices_before_quantizing(value):
    """A non-finite price must take validation's missing-data path."""
    assert as_money(value) is None
