"""RANK-002 model tests for annotating complete scan result frames."""

from __future__ import annotations

import datetime as dt
import math

import pandas as pd

from backend.scoring import ScoringConfig, ScoringContext, score_candidates
from backend.scoring.components import risk_score_absolute


def _provenance() -> dict:
    return {
        "triggered_rules": ["test_rule"],
        "indicator_values": {"close": 100.0},
        "source": "deterministic",
    }


def _candles(close_values: list[float], volume_values: list[int] | None = None) -> pd.DataFrame:
    volumes = volume_values or [1000 for _ in close_values]
    return pd.DataFrame(
        {
            "timestamp": pd.date_range("2026-05-01", periods=len(close_values), freq="D"),
            "open": close_values,
            "high": [value + 1 for value in close_values],
            "low": [value - 1 for value in close_values],
            "close": close_values,
            "volume": volumes,
        }
    )


class _CachedLoader:
    """Fake loader that exposes only the cache-read method scoring is allowed to use."""

    def __init__(self, frames: dict[str, pd.DataFrame]):
        self.frames = frames
        self.cache_reads: list[tuple[str, str]] = []

    def read_cached_history(self, symbol: str, security_id: str) -> pd.DataFrame:
        self.cache_reads.append((symbol, security_id))
        return self.frames.get(symbol, pd.DataFrame()).copy()

    def get_daily_history(self, *_args, **_kwargs):  # pragma: no cover - should never be called
        raise AssertionError("RANK-002 scoring must not make live Dhan history calls")


def _context(loader: _CachedLoader, *, config: ScoringConfig | None = None) -> ScoringContext:
    return ScoringContext(
        universe_key="test_universe",
        universe_df=pd.DataFrame(
            [
                {"symbol": "AAA", "security_id": "1"},
                {"symbol": "BBB", "security_id": "2"},
            ]
        ),
        data_loader=loader,
        data_snapshot_date=dt.date(2026, 6, 2),
        config=config
        or ScoringConfig(
            weights={"technical": 0.4, "liquidity": 0.2, "risk": 0.3, "freshness": 0.1},
            liquidity_window=3,
            risk_window=3,
            risk_vol_cap=0.05,
            freshness_halflife_days=5,
        ),
    )


def test_score_candidates_populates_final_score_and_breakdown_with_all_components():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "rating": "BUY",
                "signal_date": "2026-06-02",
                "close": 104.0,
                "confidence": 7,
                "reason": "keep me",
                "provenance": _provenance(),
            }
        ]
    )
    candles = _candles([100, 102, 104], [100, 100, 100])
    context = _context(_CachedLoader({"AAA": candles}))

    scored = score_candidates(frame, context=context)

    risk = risk_score_absolute(candles, window=3, vol_cap=0.05)
    expected = round((0.4 * 50.0) + (0.2 * 50.0) + (0.3 * risk) + (0.1 * 100.0), 2)
    assert scored.loc[0, "final_score"] == expected
    breakdown = scored.loc[0, "provenance"]["score_breakdown"]
    assert breakdown["model_version"] == "rank-1.0"
    assert breakdown["components"]["technical"] == 50.0
    assert breakdown["components"]["liquidity"] == 50.0
    assert breakdown["components"]["risk"] == round(risk, 2)
    assert breakdown["components"]["freshness"] == 100.0
    assert breakdown["coverage"] == ["technical", "liquidity", "risk", "freshness"]
    assert breakdown["missing"] == []


def test_score_candidates_renormalizes_when_components_are_missing():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "rating": "BUY",
                "signal_date": "2026-06-02",
                "confidence": 5,
                "reason": "partial score",
                "provenance": _provenance(),
            }
        ]
    )
    context = _context(_CachedLoader({}))

    scored = score_candidates(frame, context=context)

    assert scored.loc[0, "final_score"] == 60.0
    breakdown = scored.loc[0, "provenance"]["score_breakdown"]
    assert breakdown["coverage"] == ["technical", "freshness"]
    assert breakdown["missing"] == ["liquidity", "risk"]
    assert breakdown["weights_effective"] == {"technical": 0.8, "freshness": 0.2}


def test_score_candidates_keeps_unscorable_rows_with_null_score():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "rating": "BUY",
                "signal_date": None,
                "reason": "no score inputs",
                "provenance": _provenance(),
            }
        ]
    )
    context = _context(_CachedLoader({}))

    scored = score_candidates(frame, context=context)

    assert len(scored) == 1
    assert math.isnan(scored.loc[0, "final_score"])
    breakdown = scored.loc[0, "provenance"]["score_breakdown"]
    assert breakdown["components"] == {}
    assert breakdown["coverage"] == []
    assert breakdown["missing"] == ["technical", "liquidity", "risk", "freshness"]


def test_score_candidates_never_mutates_the_input_frame_or_reason_column():
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "signal_date": "2026-06-02",
                "confidence": 5,
                "reason": "raw reason remains visible",
                "provenance": _provenance(),
            }
        ]
    )
    original_provenance = dict(frame.loc[0, "provenance"])
    context = _context(_CachedLoader({}))

    scored = score_candidates(frame, context=context)

    assert "final_score" not in frame.columns
    assert frame.loc[0, "reason"] == "raw reason remains visible"
    assert frame.loc[0, "provenance"] == original_provenance
    assert scored.loc[0, "reason"] == "raw reason remains visible"


def test_score_candidates_is_deterministic_and_sorts_scores_descending_nulls_last():
    frame = pd.DataFrame(
        [
            {"symbol": "AAA", "signal_date": None, "reason": "unscorable", "provenance": _provenance()},
            {
                "symbol": "BBB",
                "signal_date": "2026-06-02",
                "confidence": 5,
                "reason": "ranked",
                "provenance": _provenance(),
            },
        ]
    )
    context = _context(_CachedLoader({}))

    first = score_candidates(frame, context=context)
    second = score_candidates(frame, context=context)

    assert first["symbol"].tolist() == ["BBB", "AAA"]
    assert second["symbol"].tolist() == first["symbol"].tolist()
    pd.testing.assert_series_equal(
        second["final_score"],
        first["final_score"],
        check_names=False,
    )


def test_score_candidates_reads_cached_candles_without_live_fetches():
    loader = _CachedLoader({"AAA": _candles([100, 101, 102])})
    frame = pd.DataFrame(
        [
            {
                "symbol": "AAA",
                "signal_date": "2026-06-02",
                "confidence": 5,
                "reason": "cached only",
                "provenance": _provenance(),
            }
        ]
    )

    score_candidates(frame, context=_context(loader))

    assert loader.cache_reads == [("AAA", "1")]
