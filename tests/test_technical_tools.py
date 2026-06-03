"""Tests for the technical agent's MCP tool context and payload builders.

The fake-SDK agent tests stop before the tools are invoked, so the actual JSON
payloads the agent receives are pinned here: each must be a plain, JSON-safe dict
with the expected keys. These are the deterministic facts the agent reasons about,
so they also guarantee cache reproducibility.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd

from backend.indicators import major_levels
from backend.technical.tools import TechnicalToolContext


def _candles(periods: int = 300) -> pd.DataFrame:
    """A sideways oscillation so the troughs/peaks cluster into real major levels.

    Price swings around 120 by +/-10, printing repeated pivot lows near ~110 and
    pivot highs near ~130 — exactly the multi-touch zones `major_levels` keeps.
    """
    close = 120.0 + 10.0 * np.sin(np.arange(periods) / 3.0)
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2023-01-01", periods=periods, freq="D"),
            "open": close - 0.5,
            "high": close + 2.0,
            "low": close - 2.0,
            "close": close,
            "volume": [1000.0] * periods,
        }
    )


def _levels(frame: pd.DataFrame) -> list[dict]:
    return major_levels(frame, left=5, right=5, cluster_pct=2.0, min_touches=3)


def test_tool_payloads_are_json_serializable_dicts_with_expected_keys():
    frame = _candles()
    ctx = TechnicalToolContext.build("DEMO", frame, _levels(frame), params=None)

    level_map = ctx.level_map_payload()
    price_patterns = ctx.price_patterns_payload()
    market_structure = ctx.market_structure_payload()

    # All three must be plain dicts that survive json.dumps (no numpy/Timestamp).
    for payload in (level_map, price_patterns, market_structure):
        assert isinstance(payload, dict)
        json.dumps(payload)  # raises if any value is not JSON-serializable

    assert {"daily", "weekly"} <= set(level_map)
    assert {"fair_value_gaps", "double_bottom", "double_top", "order_blocks"} <= set(
        price_patterns
    )
    assert {"daily", "weekly"} <= set(market_structure)
    # Daily structure always reports a trend label.
    assert market_structure["daily"]["trend"] in {"uptrend", "downtrend", "sideways"}


def test_tool_context_relevance_scores_daily_levels():
    frame = _candles()
    ctx = TechnicalToolContext.build("DEMO", frame, _levels(frame), params=None)
    # rank_levels ran, so each daily level carries a relevance score in [0, 1].
    assert ctx.daily_levels  # the synthetic frame produces at least one level
    for level in ctx.daily_levels:
        assert 0.0 <= level["relevance"] <= 1.0
        assert "components" in level


def test_weekly_can_be_disabled():
    frame = _candles()
    ctx = TechnicalToolContext.build(
        "DEMO", frame, _levels(frame), params={"weekly_enabled": False}
    )
    assert ctx.weekly.empty
    assert ctx.level_map_payload()["weekly"] == []
    assert ctx.market_structure_payload()["weekly"] is None
