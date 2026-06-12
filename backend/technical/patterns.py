"""Deterministic price-action pattern detectors for the Technical Analysis agent.

Why this module exists (beginner note)
--------------------------------------
The Technical Analysis (AI) screener used to recognize only three setups
(cup-and-handle, inverse head-and-shoulders, at-support), and it asked the Claude
agent to *eyeball* them from a raw candle dump. That is fuzzy and hard to cache.

This module adds **deterministic, pure-pandas detectors** for four modern
price-action concepts. "Deterministic" means: given the same candles and the same
settings, the output is always identical — no randomness, no LLM. The Claude
agent then *queries* these detectors through MCP tools (see
`backend/technical/tools.py`) and reasons about the results, instead of guessing
from pixels. Because the detectors are deterministic, the agent's per-day verdict
cache stays correct.

The four concepts
-----------------
1. **Fair Value Gap (FVG)** — a 3-candle imbalance where price "gapped" so fast it
   left an untraded void. Bullish FVGs below price often act as demand zones.
2. **Double Top / Double Bottom** — two roughly-equal swing extremes that signal a
   reversal once the "neckline" between them breaks.
3. **Order Block (OB)** — the last opposite-colour candle right before an
   impulsive move that breaks market structure. A bullish OB is a demand zone.
4. **Market structure (BOS / CHoCH)** — the sequence of swing highs and lows that
   defines trend, plus Break-of-Structure (continuation) and Change-of-Character
   (first counter-trend break, a reversal warning).

Design choices (kept deliberately simple — Karpathy "simplicity first")
-----------------------------------------------------------------------
- Every detector takes a **prepared OHLC frame** (the output of
  `BaseScanner.prepare_candles` / `backend.indicators.prepare_ohlc`): columns
  ``timestamp, open, high, low, close`` (``volume`` optional), oldest row first.
- Every detector returns **plain Python dicts / lists of dicts** with JSON-safe
  values (floats, ints, ``YYYY-MM-DD`` strings, bools) so the agent tools can
  serialize them with no extra conversion.
- Times are returned as ``YYYY-MM-DD`` strings (the app is daily/weekly), matching
  how `technical_agent._ohlc_csv` already renders dates.
- Each detection carries a ``bars_ago`` field (how many candles back it formed)
  so the cheap screener gate and the agent can prefer *recent, near-price* setups.

No TA-Lib / pandas_ta equivalents exist for these structural concepts, so unlike
the indicators in `backend/indicators.py` there is no optional-library fast path —
just clear, vectorized pandas/numpy.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

# Reuse the existing, already-tested vectorized pivot detectors. A "pivot" is a
# local swing extreme (a low with `left` lower-or-equal candles before and
# `right` after it, or the mirror for a high). These are the building blocks for
# double tops/bottoms, order blocks, and market structure.
from backend.indicators import pivot_highs, pivot_lows

# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _recent(frame: pd.DataFrame, lookback_bars: int) -> pd.DataFrame:
    """Return the last `lookback_bars` rows as a fresh 0..n-1 indexed frame.

    Detectors only care about recent history (the app feeds ~10 years of daily
    candles, but a Fair Value Gap from 2016 is noise today). Re-indexing to a
    clean 0..n-1 range lets us use simple integer positions (`iloc`) throughout.
    `lookback_bars <= 0` means "use everything".
    """
    if frame is None or frame.empty:
        return pd.DataFrame()
    work = frame.reset_index(drop=True)
    if lookback_bars and lookback_bars > 0 and len(work) > lookback_bars:
        work = work.tail(lookback_bars).reset_index(drop=True)
    return work


def _date_str(value: Any) -> str:
    """Render a timestamp-ish value as a terse ``YYYY-MM-DD`` string.

    Tolerant of pandas Timestamps, datetimes, numpy datetimes, and plain
    strings. Returns ``""`` when there is no usable timestamp so callers never
    crash on a frame that happens to lack a ``timestamp`` column.
    """
    if value is None:
        return ""
    return str(value)[:10]


def _times(frame: pd.DataFrame) -> list[str]:
    """Return the frame's timestamps as ``YYYY-MM-DD`` strings (or "" if absent)."""
    if "timestamp" not in frame.columns:
        return ["" for _ in range(len(frame))]
    return [_date_str(value) for value in frame["timestamp"].tolist()]


# ---------------------------------------------------------------------------
# 1. Fair Value Gaps (FVG)
# ---------------------------------------------------------------------------


def detect_fair_value_gaps(
    frame: pd.DataFrame,
    *,
    min_gap_pct: float = 0.0,
    lookback_bars: int = 250,
) -> list[dict[str, Any]]:
    """Find 3-candle Fair Value Gaps and report whether each is still unfilled.

    What a Fair Value Gap is (beginner note)
    ----------------------------------------
    Look at any three consecutive candles ``A, B, C``. Price moved so impulsively
    through the middle candle ``B`` that candles ``A`` and ``C`` do not overlap —
    there is a price band that ``B`` jumped across without trading calmly. That
    untraded band is the "gap" / imbalance.

    - **Bullish FVG** (a strong up-move): ``C.low > A.high``. The void is the band
      ``[A.high, C.low]``. Price often dips back *down* to "fill" it later; an
      *unfilled* bullish FVG below current price is a potential demand/support zone.
    - **Bearish FVG** (a strong down-move): ``C.high < A.low``. The void is
      ``[C.high, A.low]`` and tends to get filled from below; it is a supply zone.

    Filled vs unfilled
    ------------------
    A bullish FVG is considered **filled** once a later candle trades back down to
    or below the bottom of the void (``low <= bottom``). Bearish is the mirror
    (a later ``high >= top``). The gate cares about *unfilled* bullish FVGs.

    Parameters
    ----------
    min_gap_pct:
        Ignore tiny gaps smaller than this percent of price (noise filter). ``0``
        keeps every gap.
    lookback_bars:
        Only scan this many most-recent candles.

    Returns a chronological list (oldest first) of dicts::

        {"direction": "bullish"|"bearish", "top": float, "bottom": float,
         "gap_pct": float, "filled": bool, "origin_time": "YYYY-MM-DD",
         "fill_time": "YYYY-MM-DD"|"", "bars_ago": int}
    """
    work = _recent(frame, lookback_bars)
    n = len(work)
    if n < 3:
        # Need at least three candles to form a single A/B/C triple.
        return []

    highs = work["high"].to_numpy(dtype="float64")
    lows = work["low"].to_numpy(dtype="float64")
    times = _times(work)

    gaps: list[dict[str, Any]] = []
    # `i` is the MIDDLE candle (B). A = i-1, C = i+1. We stop at n-2 so C exists.
    for i in range(1, n - 1):
        a_high, a_low = highs[i - 1], lows[i - 1]
        c_high, c_low = highs[i + 1], lows[i + 1]

        if c_low > a_high:
            # Bullish imbalance: the void sits between A.high (bottom) and C.low (top).
            bottom, top, direction = a_high, c_low, "bullish"
        elif c_high < a_low:
            # Bearish imbalance: the void sits between C.high (bottom) and A.low (top).
            bottom, top, direction = c_high, a_low, "bearish"
        else:
            # Candles A and C overlap → no gap on this triple.
            continue

        # Reject gaps that are too small to matter (percent of the lower edge).
        gap_pct = ((top - bottom) / bottom * 100.0) if bottom > 0 else 0.0
        if gap_pct < min_gap_pct:
            continue

        # "Filled" check uses every candle AFTER C (index i+2 onward). A bullish
        # void is filled when price trades back down to its bottom; bearish when
        # price trades back up to its top.
        fill_time = ""
        filled = False
        if direction == "bullish":
            later_lows = lows[i + 2 :]
            hit = np.where(later_lows <= bottom)[0]
        else:
            later_highs = highs[i + 2 :]
            hit = np.where(later_highs >= top)[0]
        if hit.size:
            filled = True
            fill_time = times[i + 2 + int(hit[0])]

        gaps.append(
            {
                "direction": direction,
                "top": round(float(top), 4),
                "bottom": round(float(bottom), 4),
                "gap_pct": round(float(gap_pct), 3),
                "filled": filled,
                "origin_time": times[i],  # the impulsive middle candle
                "fill_time": fill_time,
                # How many candles back the gap's third candle (C) printed.
                "bars_ago": int(n - 1 - (i + 1)),
            }
        )

    return gaps


# ---------------------------------------------------------------------------
# 2. Double Top / Double Bottom
# ---------------------------------------------------------------------------


def detect_double_patterns(
    frame: pd.DataFrame,
    *,
    left: int = 5,
    right: int = 5,
    tolerance_pct: float = 3.0,
    lookback_bars: int = 250,
) -> dict[str, Any]:
    """Detect the most recent double-bottom and double-top (each may be None).

    What these patterns are (beginner note)
    ---------------------------------------
    - **Double bottom** (bullish "W"): price falls to a low, bounces to an interim
      high, falls again to a *second low at roughly the same price*, then turns up.
      The interim high is the **neckline**. The pattern is **confirmed** once a
      candle *closes above the neckline* — that breakout is the actionable trigger.
    - **Double top** (bearish "M"): the mirror image — two roughly-equal highs
      around a neckline low, confirmed when a candle closes *below* the neckline.

    "Roughly equal" means the two extremes are within ``tolerance_pct`` of each
    other. We use the last two confirmed swing pivots (via the shared
    `pivot_lows` / `pivot_highs`) so the lows/highs are real local extremes, not
    noise.

    Returns::

        {"double_bottom": {...}|None, "double_top": {...}|None}

    where each pattern dict is::

        {"first_price": float, "first_time": str, "second_price": float,
         "second_time": str, "neckline": float, "neckline_time": str,
         "confirmed": bool, "confirm_time": str, "bars_ago": int}
    """
    work = _recent(frame, lookback_bars)
    result: dict[str, Any] = {"double_bottom": None, "double_top": None}
    n = len(work)
    if n < (left + right + 1):
        return result

    times = _times(work)
    highs = work["high"].to_numpy(dtype="float64")
    lows = work["low"].to_numpy(dtype="float64")
    closes = work["close"].to_numpy(dtype="float64")

    low_positions = list(np.where(pivot_lows(work["low"], left, right).to_numpy())[0])
    high_positions = list(np.where(pivot_highs(work["high"], left, right).to_numpy())[0])

    def _within_tolerance(a: float, b: float) -> bool:
        base = min(a, b)
        return base > 0 and abs(a - b) / base * 100.0 <= tolerance_pct

    # ----- Double bottom: last two pivot lows + the highest high between them -----
    if len(low_positions) >= 2:
        i1, i2 = low_positions[-2], low_positions[-1]
        p1, p2 = lows[i1], lows[i2]
        if _within_tolerance(p1, p2):
            # Neckline = the highest high in the valley-to-valley span.
            span_highs = highs[i1 : i2 + 1]
            neck_offset = int(np.argmax(span_highs))
            neckline = float(span_highs[neck_offset])
            neckline_pos = i1 + neck_offset
            # Confirmed once a later candle CLOSES above the neckline.
            after = closes[i2 + 1 :]
            hit = np.where(after > neckline)[0]
            confirmed = bool(hit.size)
            confirm_pos = i2 + 1 + int(hit[0]) if confirmed else None
            result["double_bottom"] = {
                "first_price": round(float(p1), 4),
                "first_time": times[i1],
                "second_price": round(float(p2), 4),
                "second_time": times[i2],
                "neckline": round(neckline, 4),
                "neckline_time": times[neckline_pos],
                "confirmed": confirmed,
                "confirm_time": times[confirm_pos] if confirm_pos is not None else "",
                # Bars since the second low, and since the neckline breakout (the
                # screener uses the latter to keep only FRESH confirmations).
                "bars_ago": int(n - 1 - i2),
                "confirm_bars_ago": int(n - 1 - confirm_pos) if confirm_pos is not None else None,
            }

    # ----- Double top: last two pivot highs + the lowest low between them -----
    if len(high_positions) >= 2:
        j1, j2 = high_positions[-2], high_positions[-1]
        q1, q2 = highs[j1], highs[j2]
        if _within_tolerance(q1, q2):
            span_lows = lows[j1 : j2 + 1]
            neck_offset = int(np.argmin(span_lows))
            neckline = float(span_lows[neck_offset])
            neckline_pos = j1 + neck_offset
            after = closes[j2 + 1 :]
            hit = np.where(after < neckline)[0]
            confirmed = bool(hit.size)
            confirm_pos = j2 + 1 + int(hit[0]) if confirmed else None
            result["double_top"] = {
                "first_price": round(float(q1), 4),
                "first_time": times[j1],
                "second_price": round(float(q2), 4),
                "second_time": times[j2],
                "neckline": round(neckline, 4),
                "neckline_time": times[neckline_pos],
                "confirmed": confirmed,
                "confirm_time": times[confirm_pos] if confirm_pos is not None else "",
                "bars_ago": int(n - 1 - j2),
                "confirm_bars_ago": int(n - 1 - confirm_pos) if confirm_pos is not None else None,
            }

    return result


# ---------------------------------------------------------------------------
# 3. Market structure (swings, trend, BOS / CHoCH)
# ---------------------------------------------------------------------------


def detect_market_structure(
    frame: pd.DataFrame,
    *,
    left: int = 5,
    right: int = 5,
    lookback_bars: int = 400,
) -> dict[str, Any]:
    """Summarize trend and the latest Break-of-Structure / Change-of-Character.

    What "market structure" means (beginner note)
    ----------------------------------------------
    Markets move in swings: alternating swing highs (SH) and swing lows (SL).
    Reading the sequence tells you the trend:

    - **Uptrend** = Higher Highs *and* Higher Lows (HH + HL).
    - **Downtrend** = Lower Highs *and* Lower Lows (LH + LL).
    - **Sideways** = anything else (mixed).

    Two structural events matter:

    - **BOS (Break of Structure)** — price closes beyond the most recent swing in
      the *direction of the trend*. It confirms the trend continues.
    - **CHoCH (Change of Character)** — the *first* close beyond a swing *against*
      the trend. It is an early warning that the trend may be reversing.

    Because a pivot at bar ``i`` is only *confirmed* ``right`` bars later, we only
    let a swing act as a breakable reference from bar ``i + right`` onward — this
    avoids "seeing the future" (look-ahead bias).

    Returns::

        {"trend": "uptrend"|"downtrend"|"sideways",
         "swing_high": {"price": float, "time": str}|None,
         "swing_low":  {"price": float, "time": str}|None,
         "last_event": {"type": "BOS"|"CHoCH", "direction": "up"|"down",
                        "price": float, "time": str}|None}
    """
    work = _recent(frame, lookback_bars)
    result: dict[str, Any] = {
        "trend": "sideways",
        "swing_high": None,
        "swing_low": None,
        "last_event": None,
    }
    n = len(work)
    if n < (left + right + 1):
        return result

    times = _times(work)
    highs = work["high"].to_numpy(dtype="float64")
    lows = work["low"].to_numpy(dtype="float64")
    closes = work["close"].to_numpy(dtype="float64")

    high_positions = list(np.where(pivot_highs(work["high"], left, right).to_numpy())[0])
    low_positions = list(np.where(pivot_lows(work["low"], left, right).to_numpy())[0])

    # Latest confirmed swing high / low (used as the breakable reference levels).
    if high_positions:
        ph = high_positions[-1]
        result["swing_high"] = {"price": round(float(highs[ph]), 4), "time": times[ph]}
    if low_positions:
        pl = low_positions[-1]
        result["swing_low"] = {"price": round(float(lows[pl]), 4), "time": times[pl]}

    # ----- Trend from the last two highs and the last two lows -----
    trend = "sideways"
    if len(high_positions) >= 2 and len(low_positions) >= 2:
        higher_high = highs[high_positions[-1]] > highs[high_positions[-2]]
        higher_low = lows[low_positions[-1]] > lows[low_positions[-2]]
        lower_high = highs[high_positions[-1]] < highs[high_positions[-2]]
        lower_low = lows[low_positions[-1]] < lows[low_positions[-2]]
        if higher_high and higher_low:
            trend = "uptrend"
        elif lower_high and lower_low:
            trend = "downtrend"
    result["trend"] = trend

    # ----- Latest structural break -----
    # For every swing, find the FIRST later close that breaks beyond it (up for
    # swing highs, down for swing lows), only counting bars after confirmation.
    # The most recent such break across all swings is the "last event".
    events: list[tuple[int, str, float]] = []  # (break_position, direction, level)
    for ph in high_positions:
        start = ph + right + 1
        if start < n:
            seg = closes[start:]
            hit = np.where(seg > highs[ph])[0]
            if hit.size:
                events.append((start + int(hit[0]), "up", float(highs[ph])))
    for pl in low_positions:
        start = pl + right + 1
        if start < n:
            seg = closes[start:]
            hit = np.where(seg < lows[pl])[0]
            if hit.size:
                events.append((start + int(hit[0]), "down", float(lows[pl])))

    if events:
        # Most recent break wins.
        position, direction, level = max(events, key=lambda item: item[0])
        # Heuristic label: a break that agrees with the prevailing trend is a BOS
        # (continuation); a break against it is a CHoCH (possible reversal). When
        # the trend is sideways we treat any break as a CHoCH (character change).
        agrees = (direction == "up" and trend == "uptrend") or (
            direction == "down" and trend == "downtrend"
        )
        event_type = "BOS" if agrees else "CHoCH"
        result["last_event"] = {
            "type": event_type,
            "direction": direction,
            "price": round(level, 4),
            "time": times[position],
        }

    return result


# ---------------------------------------------------------------------------
# 4. Order Blocks
# ---------------------------------------------------------------------------


def detect_order_blocks(
    frame: pd.DataFrame,
    *,
    left: int = 5,
    right: int = 5,
    lookback_bars: int = 250,
) -> list[dict[str, Any]]:
    """Find demand/supply Order Blocks created by structure-breaking impulses.

    What an Order Block is (beginner note)
    --------------------------------------
    Big players accumulate orders in the *last candle before* an explosive move.
    That candle is the **Order Block** and price often respects it on a retest:

    - **Bullish OB** (demand): the last *down* candle (close < open) immediately
      before an impulsive up-move that **breaks structure** above a prior swing
      high. Its full range ``[low, high]`` is the demand zone.
    - **Bearish OB** (supply): the mirror — the last *up* candle before an
      impulsive down-move that breaks below a prior swing low.

    An OB is **mitigated** once price later trades back into its zone (a bullish
    OB is mitigated when a later candle's ``low <= zone top``). *Unmitigated*
    bullish OBs sitting near price are potential bounce zones for the screener.

    We anchor OBs to real Break-of-Structure events (a close beyond a confirmed
    swing), then walk backwards from the breakout to find the originating candle.
    Returns the most recent bullish and bearish OB (each at most once)::

        [{"direction": "bullish"|"bearish", "top": float, "bottom": float,
          "origin_time": str, "mitigated": bool, "bos_time": str,
          "bars_ago": int}, ...]
    """
    work = _recent(frame, lookback_bars)
    n = len(work)
    if n < (left + right + 2):
        return []

    times = _times(work)
    opens = work["open"].to_numpy(dtype="float64")
    highs = work["high"].to_numpy(dtype="float64")
    lows = work["low"].to_numpy(dtype="float64")
    closes = work["close"].to_numpy(dtype="float64")

    high_positions = list(np.where(pivot_highs(work["high"], left, right).to_numpy())[0])
    low_positions = list(np.where(pivot_lows(work["low"], left, right).to_numpy())[0])

    blocks: list[dict[str, Any]] = []

    def _latest_break(positions: list[int], levels: np.ndarray, direction: str) -> int | None:
        """Return the bar position of the most recent structure break, or None."""
        best: int | None = None
        for p in positions:
            start = p + right + 1
            if start >= n:
                continue
            seg = closes[start:]
            hit = np.where(seg > levels[p])[0] if direction == "up" else np.where(seg < levels[p])[0]
            if hit.size:
                pos = start + int(hit[0])
                if best is None or pos > best:
                    best = pos
        return best

    # ----- Bullish OB: last DOWN candle before the latest bullish BOS -----
    bos_up = _latest_break(high_positions, highs, "up")
    if bos_up is not None:
        # Walk backwards from the candle BEFORE the breakout. The breakout candle
        # proves the structure break; it is not the accumulation candle that
        # created the order block, even if it happens to close red.
        for k in range(bos_up - 1, max(bos_up - (left + right + 5), -1), -1):
            if closes[k] < opens[k]:  # a down (red) candle
                top, bottom = float(highs[k]), float(lows[k])
                # Mitigated if any candle AFTER the breakout dipped into the zone.
                later_lows = lows[bos_up + 1 :]
                mitigated = bool((later_lows <= top).any()) if later_lows.size else False
                blocks.append(
                    {
                        "direction": "bullish",
                        "top": round(top, 4),
                        "bottom": round(bottom, 4),
                        "origin_time": times[k],
                        "mitigated": mitigated,
                        "bos_time": times[bos_up],
                        "bars_ago": int(n - 1 - k),
                    }
                )
                break

    # ----- Bearish OB: last UP candle before the latest bearish BOS -----
    bos_down = _latest_break(low_positions, lows, "down")
    if bos_down is not None:
        # Same rule in reverse: the bearish order block must be the final green
        # candle before price breaks structure downward, not the break candle.
        for k in range(bos_down - 1, max(bos_down - (left + right + 5), -1), -1):
            if closes[k] > opens[k]:  # an up (green) candle
                top, bottom = float(highs[k]), float(lows[k])
                later_highs = highs[bos_down + 1 :]
                mitigated = bool((later_highs >= bottom).any()) if later_highs.size else False
                blocks.append(
                    {
                        "direction": "bearish",
                        "top": round(top, 4),
                        "bottom": round(bottom, 4),
                        "origin_time": times[k],
                        "mitigated": mitigated,
                        "bos_time": times[bos_down],
                        "bars_ago": int(n - 1 - k),
                    }
                )
                break

    return blocks
