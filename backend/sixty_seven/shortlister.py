"""Deterministic 67% drawdown shortlisting for the 67 ka funda strategy.

Beginner note — "67 ka funda" in one paragraph
----------------------------------------------
The idea: hunt for beaten-down stocks that have fallen at least ~67% from their
all-time high (ATH) yet may have a credible path back. A 67% drawdown means
today's price is ~33% of the ATH, which is a ~203% climb back to it
((100 - 33) / 33). So a 67% drawdown *already implies* well over 100% upside — the
``upside_threshold_pct`` is therefore a secondary, rarely-binding guard; the
drawdown gate dominates at the default thresholds. Keeping both simply makes the
two knobs explicit and independently configurable.

This module is the cheap, deterministic FIRST stage of the screener: pure price
math, no network and no LLM. Only the handful of stocks it shortlists are sent on
to the (expensive) Claude verifier in `backend/sixty_seven/agent.py`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass

import pandas as pd

from backend.indicators import prepare_ohlc


@dataclass(frozen=True)
class DrawdownCandidate:
    """One stock that passed the deterministic 67% drawdown gate.

    Frozen (immutable) so it can be hashed/cached and safely handed to the agent.
    These are the price facts the AI verifier treats as source-of-truth.
    """

    symbol: str
    ath_price: float          # highest high over the AVAILABLE candle history
    ath_date: str             # date of that ATH (YYYY-MM-DD)
    latest_close: float       # most recent close
    signal_date: str          # date of the latest candle
    drawdown_pct: float       # how far below the ATH the latest close is, in %
    upside_to_ath_pct: float  # % gain from latest close back up to the ATH

    def to_prompt_dict(self) -> dict[str, object]:
        """Return the facts as a plain dict (for the prompt and the cache hash)."""
        return asdict(self)


def _date_text(value: object) -> str:
    """Render a timestamp-ish value as a plain ``YYYY-MM-DD`` string.

    Tolerant of pandas Timestamps (which expose ``.date()``), datetimes, and
    plain strings, so a malformed timestamp degrades to ``str(value)`` rather
    than crashing the gate.
    """
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
    """Return a `DrawdownCandidate` when the latest close is far below the ATH.

    "ATH" means the highest high over the *available* candle history (the screener
    feeds ~10 years), not a guaranteed lifetime high. Returns None for any stock
    that is too short, malformed, or simply not down enough — dropped cheaply
    before any AI call.
    """
    # prepare_ohlc sorts/cleans the frame and coerces OHLC to numeric; a bad frame
    # raises, which we treat as "not a candidate".
    try:
        frame = prepare_ohlc(candles)
    except (TypeError, ValueError):
        return None
    if frame.empty or "high" not in frame.columns or "close" not in frame.columns:
        return None

    # Coerce defensively (API/CSV data can arrive as strings). If nothing usable
    # survives, this stock cannot be evaluated.
    highs = pd.to_numeric(frame["high"], errors="coerce")
    closes = pd.to_numeric(frame["close"], errors="coerce")
    if highs.dropna().empty or closes.dropna().empty:
        return None

    # idxmax ignores NaN and returns the FIRST bar that reached the highest high.
    ath_index = highs.idxmax()
    latest = frame.iloc[-1]
    ath_row = frame.loc[ath_index]
    ath_price = float(ath_row["high"])
    latest_close = float(latest["close"])
    # Guard against zero/garbage prices before we divide by them.
    if ath_price <= 0 or latest_close <= 0:
        return None

    # Drawdown is measured against the ATH; upside against today's price. (Per the
    # module note, 67% down ⇒ ~203% upside, so the drawdown gate is the binding
    # one at the default thresholds; the upside check is a belt-and-braces guard.)
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
    """Shortlist a `symbol -> candles` mapping, preserving input (universe) order.

    Order is preserved so the screener's output is deterministic across runs.
    """
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
