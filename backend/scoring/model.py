"""Frame-level RANK-002 scoring.

Beginner note:
The component functions in ``components.py`` know how to score one kind of
input. This module is the "orchestrator" for a whole shortlist:

1. collect raw component inputs for every row;
2. normalize cross-sectional pieces inside the current run;
3. combine whichever components are available for each row; and
4. attach a small ``score_breakdown`` receipt to the row provenance.

The scorer is deliberately cache-only. It may read candles that already exist
on disk through ``DailyDataLoader.read_cached_history(...)``, but it must never
call live-fetch methods such as ``get_daily_history(...)``.
"""

from __future__ import annotations

import copy
import datetime as dt
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from backend.scoring.components import (
    cross_sectional,
    freshness_score_absolute,
    liquidity_raw,
    risk_score_absolute,
    technical_raw,
)
from backend.scoring.config import ScoringConfig

_COMPONENT_ORDER = ("technical", "liquidity", "risk", "freshness")
_SECURITY_ID_COLUMNS = (
    "security_id",
    "dhan_security_id",
    "SEM_SMST_SECURITY_ID",
    "Security Id",
    "securityId",
)


@dataclass(frozen=True)
class ScoringContext:
    """Everything the scorer needs beyond the result DataFrame itself.

    ``universe_df`` and ``data_loader`` are supplied by the scan that just ran.
    Reusing them is important: scoring should not reload universes or make new
    network calls after the screener has finished.
    """

    universe_key: str
    universe_df: pd.DataFrame
    data_loader: Any
    data_snapshot_date: dt.date | None
    config: ScoringConfig


def score_candidates(
    results: pd.DataFrame,
    *,
    context: ScoringContext,
) -> pd.DataFrame:
    """Return a ranked copy of ``results`` with ``final_score`` receipts.

    The input frame is never mutated. Missing data is handled per component:
    for example, a symbol without cached candles can still receive technical
    and freshness points. Only rows with zero computable components get a null
    ``final_score``.
    """
    if results is None:
        return pd.DataFrame(columns=["final_score"])

    ranked = results.copy(deep=True).reset_index(drop=True)
    if ranked.empty:
        if "final_score" not in ranked.columns:
            ranked["final_score"] = pd.Series(dtype="float64")
        return ranked

    symbol_to_security_id = _security_id_lookup(context.universe_df)
    cached_candles = [
        _read_cached_candles(row, symbol_to_security_id, context.data_loader)
        for row in ranked.to_dict("records")
    ]

    technical_scores = cross_sectional(
        pd.Series(
            [technical_raw(row) for row in ranked.to_dict("records")],
            index=ranked.index,
            dtype="float64",
        )
    )
    liquidity_scores = cross_sectional(
        pd.Series(
            [
                _log_liquidity(
                    liquidity_raw(candles, context.config.liquidity_window)
                )
                for candles in cached_candles
            ],
            index=ranked.index,
            dtype="float64",
        )
    )
    risk_scores = pd.Series(
        [
            risk_score_absolute(
                candles,
                context.config.risk_window,
                context.config.risk_vol_cap,
            )
            for candles in cached_candles
        ],
        index=ranked.index,
        dtype="float64",
    )
    freshness_scores = pd.Series(
        [
            _freshness_for_row(
                row,
                snapshot_date=context.data_snapshot_date,
                halflife_days=context.config.freshness_halflife_days,
            )
            for row in ranked.to_dict("records")
        ],
        index=ranked.index,
        dtype="float64",
    )

    component_frames = {
        "technical": technical_scores,
        "liquidity": liquidity_scores,
        "risk": risk_scores,
        "freshness": freshness_scores,
    }

    final_scores: list[float] = []
    breakdowns: list[dict[str, Any]] = []
    for index in ranked.index:
        final_score, breakdown = _score_one_row(
            index,
            component_frames,
            config=context.config,
        )
        final_scores.append(final_score)
        breakdowns.append(breakdown)

    ranked["final_score"] = final_scores
    _attach_score_breakdowns(ranked, breakdowns)
    return _sort_ranked_frame(ranked)


def _score_one_row(
    index: int,
    component_frames: Mapping[str, pd.Series],
    *,
    config: ScoringConfig,
) -> tuple[float, dict[str, Any]]:
    """Combine one row's available component scores into a final score."""
    components: dict[str, float] = {}
    raw_components: dict[str, float] = {}
    missing: list[str] = []
    weights: dict[str, float] = {}

    for name in _COMPONENT_ORDER:
        score = _finite_component(component_frames[name].loc[index])
        weight = _finite_weight(config.weights.get(name))
        if score is None:
            missing.append(name)
            continue
        raw_components[name] = score
        components[name] = round(score, 2)
        if weight is not None:
            weights[name] = weight

    total_weight = sum(weights.values())
    if not weights or not math.isfinite(total_weight) or total_weight <= 0:
        final_score = math.nan
        effective_weights: dict[str, float] = {}
    else:
        effective_weights = {
            name: round(weight / total_weight, 10)
            for name, weight in weights.items()
        }
        # The additive formula is intentionally simple and auditable:
        # final_score = sum(component_score * renormalized_weight).
        final_score = round(
            sum(
                raw_components[name] * weight
                for name, weight in effective_weights.items()
            ),
            2,
        )

    breakdown = {
        "model_version": config.model_version,
        "scale": "0-100",
        "final_score": None if math.isnan(final_score) else final_score,
        "components": components,
        "weights_effective": effective_weights,
        "coverage": [name for name in _COMPONENT_ORDER if name in components],
        "missing": missing,
    }
    return final_score, breakdown


def _attach_score_breakdowns(
    ranked: pd.DataFrame,
    breakdowns: list[dict[str, Any]],
) -> None:
    """Copy row provenance and add ``score_breakdown`` without mutating input."""
    for index, breakdown in enumerate(breakdowns):
        for column in ("provenance", "provenance_json"):
            if column not in ranked.columns:
                continue
            raw = ranked.at[index, column]
            if isinstance(raw, Mapping):
                provenance = copy.deepcopy(dict(raw))
                provenance["score_breakdown"] = breakdown
                ranked.at[index, column] = provenance
                break


def _sort_ranked_frame(frame: pd.DataFrame) -> pd.DataFrame:
    """Sort score descending, put null scores last, and preserve ties."""
    sortable = frame.copy(deep=True)
    sortable["_rank_original_order"] = range(len(sortable))
    sortable["_rank_final_score"] = pd.to_numeric(
        sortable["final_score"],
        errors="coerce",
    )
    sortable["_rank_final_score"] = sortable["_rank_final_score"].where(
        np.isfinite(sortable["_rank_final_score"]),
        np.nan,
    )
    return (
        sortable.sort_values(
            by=["_rank_final_score", "_rank_original_order"],
            ascending=[False, True],
            na_position="last",
            kind="mergesort",
        )
        .drop(columns=["_rank_original_order", "_rank_final_score"])
        .reset_index(drop=True)
    )


def _read_cached_candles(
    row: Mapping[str, Any],
    symbol_to_security_id: Mapping[str, str],
    data_loader: Any,
) -> pd.DataFrame:
    """Read cached candles for one row, never falling back to a live fetch."""
    symbol = _clean_text(row.get("symbol"))
    if not symbol:
        return pd.DataFrame()

    security_id = _clean_text(row.get("security_id"))
    if security_id is None:
        security_id = symbol_to_security_id.get(symbol.upper())
    if not security_id:
        return pd.DataFrame()

    reader = getattr(data_loader, "read_cached_history", None)
    if not callable(reader):
        return pd.DataFrame()
    try:
        candles = reader(symbol, security_id)
    except Exception:
        # A corrupt cache file or a test fake should drop only liquidity/risk.
        return pd.DataFrame()
    return candles if isinstance(candles, pd.DataFrame) else pd.DataFrame()


def _security_id_lookup(universe_df: pd.DataFrame | None) -> dict[str, str]:
    """Build a symbol -> security_id map from the already-loaded universe."""
    if universe_df is None or universe_df.empty or "symbol" not in universe_df.columns:
        return {}
    security_column = next(
        (column for column in _SECURITY_ID_COLUMNS if column in universe_df.columns),
        None,
    )
    if security_column is None:
        return {}

    lookup: dict[str, str] = {}
    for row in universe_df[["symbol", security_column]].to_dict("records"):
        symbol = _clean_text(row.get("symbol"))
        security_id = _clean_text(row.get(security_column))
        if symbol and security_id:
            lookup[symbol.upper()] = security_id
    return lookup


def _freshness_for_row(
    row: Mapping[str, Any],
    *,
    snapshot_date: dt.date | None,
    halflife_days: float,
) -> float | None:
    """Score freshness from stored dates, never from the current wall clock."""
    signal_date = _as_date(row.get("signal_date"))
    if signal_date is None or snapshot_date is None:
        return None
    return freshness_score_absolute((snapshot_date - signal_date).days, halflife_days)


def _as_date(value: Any) -> dt.date | None:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    if value is None:
        return None
    timestamp = pd.to_datetime(value, errors="coerce")
    if pd.isna(timestamp):
        return None
    return timestamp.date()


def _log_liquidity(value: float | None) -> float | None:
    """Compress traded-value scale before cross-sectional normalization."""
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return math.log1p(value)


def _finite_component(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _finite_weight(value: Any) -> float | None:
    weight = _finite_component(value)
    if weight is None or weight <= 0:
        return None
    return weight


def _clean_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
