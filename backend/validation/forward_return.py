"""VALID-002 pure forward-return math over one symbol's candle frame."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

import pandas as pd

from backend.indicators import prepare_ohlc
from backend.storage.models import ForwardReturnStatus

FORWARD_RETURN_HORIZONS: tuple[int, ...] = (20, 60, 120)
MISSING_FUTURE_DATA_GRACE_DAYS = 7

_MONEY_QUANT = Decimal("0.0001")
_PCT_QUANT = Decimal("0.0001")


@dataclass(frozen=True)
class ForwardReturnPoint:
    """One horizon measurement, or a retryable/terminal non-computed status."""

    horizon_days: int
    status: ForwardReturnStatus
    entry_date: dt.date | None = None
    exit_date: dt.date | None = None
    entry_price: Decimal | None = None
    exit_price: Decimal | None = None
    forward_return_pct: Decimal | None = None
    max_adverse_excursion_pct: Decimal | None = None
    max_favorable_excursion_pct: Decimal | None = None


def compute_forward_return(
    candles: pd.DataFrame,
    signal_date: dt.date,
    horizon_days: int,
    *,
    as_of: dt.date | None = None,
    missing_data_grace_days: int = MISSING_FUTURE_DATA_GRACE_DAYS,
) -> ForwardReturnPoint:
    """Measure one signal's forward return without database or network access.

    The no-lookahead contract is the important bit: entry is the next bar's
    open, exit is the ``horizon_days`` bar's close, and an exit after ``as_of``
    stays pending instead of being guessed.
    """
    as_of_date = as_of or dt.date.today()
    frame = _prepared_frame(candles)
    if frame.empty:
        return _empty_point(horizon_days, ForwardReturnStatus.INSUFFICIENT_DATA)

    signal_index = _position_for_date(frame, signal_date)
    if signal_index is None:
        return _empty_point(horizon_days, ForwardReturnStatus.INSUFFICIENT_DATA)

    entry_index = signal_index + 1
    exit_index = signal_index + int(horizon_days)
    if entry_index >= len(frame) or exit_index >= len(frame):
        return _empty_point(
            horizon_days,
            _missing_future_status(frame, as_of_date, missing_data_grace_days),
        )

    entry_row = frame.iloc[entry_index]
    exit_row = frame.iloc[exit_index]
    entry_date = entry_row["_date"]
    exit_date = exit_row["_date"]
    if exit_date > as_of_date:
        return _empty_point(horizon_days, ForwardReturnStatus.PENDING)

    entry_price = _as_money(entry_row["open"])
    exit_price = _as_money(exit_row["close"])
    if entry_price is None or exit_price is None or entry_price <= 0:
        return _empty_point(horizon_days, ForwardReturnStatus.INSUFFICIENT_DATA)

    window = frame.iloc[entry_index : exit_index + 1]
    low_price = _as_money(window["low"].min())
    high_price = _as_money(window["high"].max())
    if low_price is None or high_price is None:
        return _empty_point(horizon_days, ForwardReturnStatus.INSUFFICIENT_DATA)

    return ForwardReturnPoint(
        horizon_days=int(horizon_days),
        status=ForwardReturnStatus.COMPUTED,
        entry_date=entry_date,
        exit_date=exit_date,
        entry_price=entry_price,
        exit_price=exit_price,
        forward_return_pct=_pct(exit_price - entry_price, entry_price),
        max_adverse_excursion_pct=_pct(low_price - entry_price, entry_price),
        max_favorable_excursion_pct=_pct(high_price - entry_price, entry_price),
    )


def _prepared_frame(candles: pd.DataFrame) -> pd.DataFrame:
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


def _position_for_date(frame: pd.DataFrame, wanted: dt.date) -> int | None:
    for index, value in enumerate(frame["_date"]):
        if value == wanted:
            return index
    return None


def _missing_future_status(
    frame: pd.DataFrame,
    as_of: dt.date,
    grace_days: int,
) -> ForwardReturnStatus:
    latest_date = max(frame["_date"])
    if latest_date + dt.timedelta(days=max(0, grace_days)) >= as_of:
        return ForwardReturnStatus.PENDING
    return ForwardReturnStatus.INSUFFICIENT_DATA


def _empty_point(
    horizon_days: int,
    status: ForwardReturnStatus,
) -> ForwardReturnPoint:
    return ForwardReturnPoint(horizon_days=int(horizon_days), status=status)


def _as_money(value: object) -> Decimal | None:
    try:
        return Decimal(str(value)).quantize(_MONEY_QUANT)
    except (InvalidOperation, ValueError):
        return None


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal:
    return ((numerator / denominator) * Decimal("100")).quantize(_PCT_QUANT)
