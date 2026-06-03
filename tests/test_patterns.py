"""Tests for the deterministic price-action detectors in backend.technical.patterns.

Each test builds a tiny synthetic OHLC frame that *obviously* contains (or
obviously lacks) the pattern under test, with the expected values worked out by
hand in the comments. Because the detectors are pure functions of the candles,
these assertions pin their behaviour exactly — no LLM, no randomness.
"""

from __future__ import annotations

import pandas as pd

from backend.technical.patterns import (
    detect_double_patterns,
    detect_fair_value_gaps,
    detect_market_structure,
    detect_order_blocks,
)


def _frame(rows: list[tuple], *, start: str = "2024-01-01") -> pd.DataFrame:
    """Build an OHLC(V) frame from (open, high, low, close) or (...,volume) rows."""
    cols = ["open", "high", "low", "close"]
    has_volume = len(rows[0]) == 5
    if has_volume:
        cols.append("volume")
    frame = pd.DataFrame(rows, columns=cols)
    frame.insert(0, "timestamp", pd.date_range(start, periods=len(rows), freq="D"))
    return frame


def _uptrend_frame() -> pd.DataFrame:
    """A clean rising zigzag: Higher Highs + Higher Lows with a final BOS up.

    Pivot highs (left=right=2) land at the 106/110/114 peaks and pivot lows at the
    100/104/108 troughs, so the structure reads as a textbook uptrend. ``open`` is
    set to the previous close so candles have real up/down colour (needed for the
    order-block test).
    """
    closes = [100, 103, 106, 103, 100, 104, 110, 107, 104, 108, 114, 111, 108, 112, 118, 121, 124]
    opens = [closes[0]] + closes[:-1]
    rows = [
        (float(o), float(c + 1), float(c - 1), float(c), 1000.0)
        for o, c in zip(opens, closes)
    ]
    return _frame(rows)


# ---------------------------------------------------------------------------
# Fair Value Gaps
# ---------------------------------------------------------------------------


def test_bullish_fvg_detected_and_unfilled():
    # Triple A=d0 (high 100), B=d1 (impulse up), C=d2 (low 105). 105 > 100 so the
    # void is [100, 105]; later candles stay above 105 → unfilled.
    frame = _frame(
        [
            (95, 100, 94, 99),
            (102, 108, 101, 107),
            (106, 110, 105, 109),
            (109, 112, 108, 111),
        ]
    )
    gaps = detect_fair_value_gaps(frame)
    bullish = [g for g in gaps if g["direction"] == "bullish"]
    assert len(bullish) == 1
    gap = bullish[0]
    assert gap["bottom"] == 100.0
    assert gap["top"] == 105.0
    assert gap["filled"] is False
    assert gap["gap_pct"] > 0


def test_bullish_fvg_marked_filled_when_price_returns():
    # Same gap, but a later candle dips to low 99 (<= 100) → the void is filled.
    frame = _frame(
        [
            (95, 100, 94, 99),
            (102, 108, 101, 107),
            (106, 110, 105, 109),
            (109, 112, 108, 111),
            (108, 110, 99, 100),
        ]
    )
    gaps = detect_fair_value_gaps(frame)
    bullish = [g for g in gaps if g["direction"] == "bullish"]
    assert len(bullish) == 1
    assert bullish[0]["filled"] is True
    assert bullish[0]["fill_time"]  # a date string was recorded


def test_bearish_fvg_detected():
    # Impulse DOWN: C=d2 high 101 < A=d0 low 108 → bearish void [101, 108].
    frame = _frame(
        [
            (110, 112, 108, 109),
            (104, 106, 99, 100),
            (98, 101, 95, 96),
            (95, 99, 92, 94),
        ]
    )
    gaps = detect_fair_value_gaps(frame)
    bearish = [g for g in gaps if g["direction"] == "bearish"]
    assert len(bearish) == 1
    assert bearish[0]["bottom"] == 101.0
    assert bearish[0]["top"] == 108.0


def test_min_gap_pct_filters_small_gaps():
    # The bullish gap [100, 105] is 5%; a 10% threshold should drop it.
    frame = _frame(
        [
            (95, 100, 94, 99),
            (102, 108, 101, 107),
            (106, 110, 105, 109),
            (109, 112, 108, 111),
        ]
    )
    assert detect_fair_value_gaps(frame, min_gap_pct=10.0) == []


# ---------------------------------------------------------------------------
# Double bottom / double top
# ---------------------------------------------------------------------------


def _double_bottom_frame() -> pd.DataFrame:
    # Two equal lows of 45 at idx 2 and idx 8, neckline (highest high between) =
    # 60 at idx 5, then closes break above 60 at idx 11 → confirmed double bottom.
    high = [57, 54, 48, 55, 58, 60, 57, 53, 48, 55, 59, 63, 65, 67, 69]
    low = [55, 52, 45, 52, 56, 58, 54, 50, 45, 52, 56, 60, 62, 64, 66]
    close = [56, 53, 47, 54, 57, 59, 55, 51, 47, 54, 58, 62, 64, 66, 68]
    rows = [(c - 1, h, lo, c) for h, lo, c in zip(high, low, close)]
    return _frame(rows)


def test_double_bottom_confirmed():
    result = detect_double_patterns(_double_bottom_frame(), left=2, right=2)
    db = result["double_bottom"]
    assert db is not None
    assert db["first_price"] == 45.0
    assert db["second_price"] == 45.0
    assert db["neckline"] == 60.0
    assert db["confirmed"] is True
    # A confirmed pattern records how many bars ago the neckline broke.
    assert db["confirm_bars_ago"] is not None and db["confirm_bars_ago"] >= 0
    # Only one pivot high exists, so there is no double top here.
    assert result["double_top"] is None


def test_double_bottom_unconfirmed_before_breakout():
    # Truncate before the neckline break (keep idx 0..10) → still a double bottom
    # shape, but no close above 60 yet, so confirmed must be False.
    frame = _double_bottom_frame().iloc[:11].reset_index(drop=True)
    db = detect_double_patterns(frame, left=2, right=2)["double_bottom"]
    assert db is not None
    assert db["confirmed"] is False
    assert db["confirm_bars_ago"] is None


# ---------------------------------------------------------------------------
# Market structure (trend + BOS/CHoCH)
# ---------------------------------------------------------------------------


def test_market_structure_reads_uptrend_and_bos():
    structure = detect_market_structure(_uptrend_frame(), left=2, right=2)
    assert structure["trend"] == "uptrend"
    assert structure["swing_high"] is not None
    assert structure["swing_low"] is not None
    event = structure["last_event"]
    assert event is not None
    assert event["direction"] == "up"
    # An up-break that agrees with an uptrend is a continuation = BOS.
    assert event["type"] == "BOS"


def test_market_structure_handles_short_frame():
    # Fewer candles than one pivot window → safe, empty-ish summary, no crash.
    tiny = _frame([(100, 101, 99, 100), (100, 102, 99, 101)])
    structure = detect_market_structure(tiny, left=5, right=5)
    assert structure["trend"] == "sideways"
    assert structure["last_event"] is None


# ---------------------------------------------------------------------------
# Order blocks
# ---------------------------------------------------------------------------


def test_bullish_order_block_before_bos():
    blocks = detect_order_blocks(_uptrend_frame(), left=2, right=2)
    bullish = [b for b in blocks if b["direction"] == "bullish"]
    assert bullish, "expected a bullish order block before the up-break"
    ob = bullish[0]
    # The originating down candle (idx 12) has range [107, 109].
    assert ob["bottom"] == 107.0
    assert ob["top"] == 109.0
    # Price never traded back into the zone afterwards → still unmitigated.
    assert ob["mitigated"] is False


def test_order_blocks_empty_on_flat_series():
    # A perfectly flat series has no structure break, hence no order blocks.
    flat = _frame([(100, 101, 99, 100) for _ in range(20)])
    assert detect_order_blocks(flat, left=2, right=2) == []
