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
from typing import Any, cast

import pandas as pd

from backend.scoring.components import (
    cross_sectional,
    freshness_score_absolute,
    liquidity_raw,
    risk_score_absolute,
    technical_raw,
)
from backend.scoring.config import ScoringConfig
from backend.scoring.ordering import sort_by_final_score

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

    Think of this object as the "read-only surroundings" for scoring. The
    result rows contain symbol-level signals, while the context supplies the
    already-loaded universe map, cache reader, data snapshot date, and model
    knobs needed to turn those signals into comparable scores.
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

    # Reset to a simple 0..N index because the component Series below are
    # position-based. Keeping an old screener index could make row 17's
    # component accidentally line up with a different DataFrame row.
    ranked = results.copy(deep=True).reset_index(drop=True)
    if ranked.empty:
        if "final_score" not in ranked.columns:
            ranked["final_score"] = pd.Series(dtype="float64")
        return ranked

    # Resolve cached candles once per row, then reuse them for both liquidity
    # and risk. This keeps scoring deterministic and avoids reading the same
    # parquet cache twice for a single symbol.
    # Materialize the row dicts once and reuse them for every component below; a
    # large shortlist would otherwise be converted to records three times.
    # ``to_dict("records")`` types its keys as Hashable; these frames come from
    # screeners whose columns are always strings, so narrow once for every
    # Mapping[str, ...] consumer below (QUAL-006).
    records = cast(list[dict[str, Any]], ranked.to_dict("records"))
    symbol_to_security_id = _security_id_lookup(context.universe_df)
    cached_candles = [
        _read_cached_candles(row, symbol_to_security_id, context.data_loader)
        for row in records
    ]

    # Technical and liquidity are relative to the current shortlist, so they use
    # cross-sectional normalization. A strong symbol gets a high score compared
    # with the other rows returned by this same scan.
    technical_scores = cross_sectional(
        pd.Series(
            [technical_raw(row) for row in records],
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
    # Risk and freshness use fixed absolute curves. A volatile symbol is risky
    # regardless of what else appeared in the shortlist, and a seven-day-old
    # signal is equally stale in every run.
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
            for row in records
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
        # Build a receipt row-by-row so the persisted provenance can explain not
        # only the final number, but also which components were available.
        final_score, breakdown = _score_one_row(
            index,
            component_frames,
            config=context.config,
        )
        final_scores.append(final_score)
        breakdowns.append(breakdown)

    ranked["final_score"] = final_scores
    _attach_score_breakdowns(ranked, breakdowns)
    # Shared with the UI display/export paths so a freshly scored frame and the
    # same run re-read from history order rows identically (deterministic rank).
    return sort_by_final_score(ranked)


def _score_one_row(
    index: int,
    component_frames: Mapping[str, pd.Series],
    *,
    config: ScoringConfig,
) -> tuple[float, dict[str, Any]]:
    """Combine one row's available component scores into a final score.

    Missing data is handled gently. If liquidity is unavailable for one row but
    technical/risk/freshness are present, only the present weights are
    renormalized. The row is kept with a null score only when *no* component has
    a usable value.
    """
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
        # ``raw_components`` keeps full precision for arithmetic. ``components``
        # is the rounded, JSON-friendly version shown to users and auditors.
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
        # The additive formula is intentionally simple and auditable. A reader
        # can recompute it from the receipt:
        # final_score = sum(component_score * renormalized_present_weight).
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
    """Copy row provenance and add ``score_breakdown`` without mutating input.

    The scorer writes the receipt into whichever provenance column the row
    already uses. It does not create a brand-new public column because display
    and CSV paths should stay compact; UI helpers can still read the nested
    receipt when they need the component table.
    """
    for index, breakdown in enumerate(breakdowns):
        # Attach to EVERY provenance column the row carries, not just the first.
        # ``normalize_screener_row`` prefers ``provenance_json`` over the legacy
        # ``provenance`` when both exist, so stopping at the first match could
        # leave the receipt on the column normalization discards.
        for column in ("provenance", "provenance_json"):
            if column not in ranked.columns:
                continue
            raw = ranked.at[index, column]
            if isinstance(raw, Mapping):
                # Deep-copy the existing provenance first. Some screeners reuse
                # nested dicts across tests, and mutating them here would leak a
                # score receipt back into the caller's original DataFrame.
                provenance = copy.deepcopy(dict(raw))
                provenance["score_breakdown"] = breakdown
                ranked.at[index, column] = provenance


def _read_cached_candles(
    row: Mapping[str, Any],
    symbol_to_security_id: Mapping[str, str],
    data_loader: Any,
) -> pd.DataFrame:
    """Read cached candles for one row, never falling back to a live fetch.

    RANK-002 is allowed to use data the scan already prepared, but it must not
    surprise the user with extra Dhan calls. That is why this helper only looks
    for ``read_cached_history`` and deliberately ignores live loader methods.
    """
    symbol = _clean_text(row.get("symbol"))
    if not symbol:
        return pd.DataFrame()

    # Result rows may already carry security_id. If not, use the universe that
    # was loaded for the current scan. We do not reload the universe here because
    # a changed CSV on disk would make scoring differ from the actual screener
    # inputs.
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
        # A corrupt cache file or a test fake should drop only liquidity/risk for
        # this row. The final score can still be computed from other components.
        return pd.DataFrame()
    return candles if isinstance(candles, pd.DataFrame) else pd.DataFrame()


def _security_id_lookup(universe_df: pd.DataFrame | None) -> dict[str, str]:
    """Build a symbol -> security_id map from the already-loaded universe.

    Different universe builders and Dhan exports have used slightly different
    security-id column names over time. The ordered column list keeps this
    helper backward-compatible without requiring every caller to rename columns.
    """
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
    """Parse a stored or pandas-friendly value into a plain ``date``.

    Freshness should be based on the scan's data snapshot, not the current wall
    clock. This helper accepts the common date-like shapes that can appear in
    params/result rows and returns ``None`` when the value is not trustworthy.
    """
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
    """Return a finite component value or ``None`` for missing/unsafe input."""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _finite_weight(value: Any) -> float | None:
    """Return a positive finite weight or ``None`` when it should be ignored."""
    weight = _finite_component(value)
    if weight is None or weight <= 0:
        return None
    return weight


def _clean_text(value: Any) -> str | None:
    """Trim a symbol/security-id-like value and treat blanks as missing."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None
