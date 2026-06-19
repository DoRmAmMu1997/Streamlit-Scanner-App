"""Shared candle-frame and Decimal helpers for the VALID-002 calculators.

``forward_return`` and ``benchmarks`` both need the same three primitives: a
cleaned OHLC frame carrying a plain ``_date`` column, exact-Decimal price
conversion, and a quantized percentage. Keeping one copy here stops the two
modules from silently drifting apart (for example one quantizing money to four
places and the other not), which would make a stock return and its benchmark
return use different rounding.

This module is package-private (leading underscore) — it is an implementation
detail of ``backend.validation``, not part of its public surface.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation

import pandas as pd

from backend.indicators import prepare_ohlc

# Prices and percentages are quantized to four decimal places to match the
# Numeric(18, 4) / Numeric(9, 4) columns the results land in (design §5.1).
MONEY_QUANT = Decimal("0.0001")
PCT_QUANT = Decimal("0.0001")


def prepared_frame(candles: pd.DataFrame) -> pd.DataFrame:
    """Return a sorted/clean OHLC frame with a ``_date`` column, or empty on failure.

    ``prepare_ohlc`` sorts, dedupes, and coerces the price columns; on top of that
    we attach a ``_date`` column (the bar's calendar date) so callers can locate a
    signal/entry/exit bar by date without re-parsing timestamps. Any structurally
    unusable frame (bad input, no timestamp, all-unparseable dates) collapses to an
    empty frame so callers take their "insufficient data" branch instead of raising.
    """
    try:
        frame = prepare_ohlc(candles)
    except (TypeError, ValueError):
        return pd.DataFrame()
    if frame.empty or "timestamp" not in frame.columns:
        return pd.DataFrame()

    timestamps = pd.to_datetime(frame["timestamp"], errors="coerce")
    valid = timestamps.notna()
    if not valid.any():
        return pd.DataFrame()
    prepared = frame.loc[valid].copy()
    prepared["_date"] = timestamps.loc[valid].dt.date
    return prepared.reset_index(drop=True)


def as_money(value: object) -> Decimal | None:
    """Convert a price to an exact Decimal quantized to 4 dp; ``None`` for NaN/garbage.

    Going through ``Decimal(str(value))`` keeps prices exact (no binary-float drift)
    and naturally rejects NaN/Inf — ``Decimal('NaN').quantize(...)`` raises
    ``InvalidOperation``, which we map to ``None``.
    """
    try:
        return Decimal(str(value)).quantize(MONEY_QUANT)
    except (InvalidOperation, ValueError):
        return None


def pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    """Return ``numerator / denominator * 100`` quantized to 4 dp.

    Callers are responsible for guarding ``denominator > 0`` (a zero/negative
    entry price is treated as bad data upstream, not divided here).
    """
    return ((numerator / denominator) * Decimal("100")).quantize(PCT_QUANT)
