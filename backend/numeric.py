"""Shared finite-Decimal parsing for backend boundary values.

Beginner note:
Several backend paths receive numbers from JSON, pandas, database columns, or
configuration. A single leaf helper keeps their safety rule consistent without
making those otherwise independent packages import each other.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation


def finite_decimal(value: object) -> Decimal | None:
    """Return ``value`` as an exact finite ``Decimal``, or ``None``.

    Booleans are rejected explicitly even though Python treats them as integers.
    Accepting ``True`` as one would turn a flag into a price or score. Conversion
    through text preserves the existing exact semantics for finite integers,
    floats, numeric strings, and ``Decimal`` values, while the final finite check
    keeps NaN and either infinity out of arithmetic and persistence boundaries.

    Beginner note:
    ``Decimal`` can represent NaN and Infinity without raising an exception.
    Parsing successfully therefore does not mean a value is safe to compare,
    sort, quantize, or store; ``is_finite`` is the required second gate.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    return parsed if parsed.is_finite() else None
