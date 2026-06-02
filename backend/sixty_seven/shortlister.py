"""Deterministic 67% drawdown shortlisting for the 67 ka funda strategy."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

import pandas as pd

from backend.indicators import prepare_ohlc


@dataclass(frozen=True)
class DrawdownCandidate:
    """One stock that passed the deterministic 67% drawdown gate."""

    symbol: str
    ath_price: float
    ath_date: str
    latest_close: float
    signal_date: str
    drawdown_pct: float
    upside_to_ath_pct: float

    def to_prompt_dict(self) -> dict[str, object]:
        return asdict(self)


def _date_text(value: object) -> str:
    if hasattr(value, "date") and callable(value.date):
        try:
            return value.date().isoformat()
        except Exception:  # noqa: BLE001 - display fallback only
            return str(value)
    return str(value or "")


def shortlist_candidate(
    symbol: str,
    candles: pd.DataFrame,
    *,
    drawdown_threshold_pct: float = 67.0,
    upside_threshold_pct: float = 100.0,
) -> DrawdownCandidate | None:
    """Return a candidate when latest close is far below available-history ATH."""
    try:
        frame = prepare_ohlc(candles)
    except (TypeError, ValueError):
        return None
    if frame.empty or "high" not in frame.columns or "close" not in frame.columns:
        return None

    highs = pd.to_numeric(frame["high"], errors="coerce")
    closes = pd.to_numeric(frame["close"], errors="coerce")
    if highs.dropna().empty or closes.dropna().empty:
        return None

    ath_index = highs.idxmax()
    latest = frame.iloc[-1]
    ath_row = frame.loc[ath_index]
    ath_price = float(ath_row["high"])
    latest_close = float(latest["close"])
    if ath_price <= 0 or latest_close <= 0:
        return None

    drawdown_pct = ((ath_price - latest_close) / ath_price) * 100.0
    upside_to_ath_pct = ((ath_price - latest_close) / latest_close) * 100.0
    if drawdown_pct < float(drawdown_threshold_pct):
        return None
    if upside_to_ath_pct < float(upside_threshold_pct):
        return None

    return DrawdownCandidate(
        symbol=str(symbol).strip().upper(),
        ath_price=ath_price,
        ath_date=_date_text(ath_row.get("timestamp", "")),
        latest_close=latest_close,
        signal_date=_date_text(latest.get("timestamp", "")),
        drawdown_pct=drawdown_pct,
        upside_to_ath_pct=upside_to_ath_pct,
    )


def shortlist_universe_frames(
    frames: Mapping[str, pd.DataFrame],
    *,
    drawdown_threshold_pct: float = 67.0,
    upside_threshold_pct: float = 100.0,
) -> list[DrawdownCandidate]:
    """Shortlist a mapping of symbol -> candles while preserving input order."""
    candidates: list[DrawdownCandidate] = []
    for symbol, candles in frames.items():
        candidate = shortlist_candidate(
            symbol,
            candles,
            drawdown_threshold_pct=drawdown_threshold_pct,
            upside_threshold_pct=upside_threshold_pct,
        )
        if candidate is not None:
            candidates.append(candidate)
    return candidates
