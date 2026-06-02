from __future__ import annotations

import pandas as pd
import pytest

from backend.sixty_seven.shortlister import (
    DrawdownCandidate,
    shortlist_candidate,
    shortlist_universe_frames,
)


def _candles(highs: list[float], closes: list[float]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-01-01", periods=len(highs), freq="D"),
            "open": closes,
            "high": highs,
            "low": [min(high, close) - 1.0 for high, close in zip(highs, closes)],
            "close": closes,
            "volume": [1000.0] * len(highs),
        }
    )


def test_shortlist_candidate_qualifies_exactly_at_67_percent_drawdown():
    candidate = shortlist_candidate(
        "demo",
        _candles([300.0, 250.0, 120.0], [250.0, 150.0, 99.0]),
        drawdown_threshold_pct=67.0,
        upside_threshold_pct=100.0,
    )

    assert isinstance(candidate, DrawdownCandidate)
    assert candidate.symbol == "DEMO"
    assert candidate.ath_price == pytest.approx(300.0)
    assert candidate.ath_date == "2026-01-01"
    assert candidate.latest_close == pytest.approx(99.0)
    assert candidate.drawdown_pct == pytest.approx(67.0)
    assert candidate.upside_to_ath_pct == pytest.approx(203.0303, rel=1e-4)


def test_shortlist_candidate_rejects_below_drawdown_threshold():
    assert (
        shortlist_candidate(
            "DEMO",
            _candles([300.0, 250.0, 120.0], [250.0, 150.0, 100.0]),
            drawdown_threshold_pct=67.0,
            upside_threshold_pct=100.0,
        )
        is None
    )


def test_shortlist_candidate_handles_empty_and_bad_frames():
    assert shortlist_candidate("EMPTY", pd.DataFrame()) is None
    assert shortlist_candidate("BAD", pd.DataFrame({"close": [10.0]})) is None


def test_shortlist_universe_frames_preserves_input_order():
    frames = {
        "FIRST": _candles([300.0, 200.0, 120.0], [250.0, 150.0, 90.0]),
        "SKIP": _candles([300.0, 250.0, 120.0], [250.0, 150.0, 120.0]),
        "SECOND": _candles([500.0, 300.0, 150.0], [450.0, 220.0, 100.0]),
    }

    rows = shortlist_universe_frames(
        frames,
        drawdown_threshold_pct=67.0,
        upside_threshold_pct=100.0,
    )

    assert [row.symbol for row in rows] == ["FIRST", "SECOND"]
