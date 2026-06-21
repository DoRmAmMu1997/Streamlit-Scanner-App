"""Deterministic ordering for scored result frames (RANK-002).

Both the live scan path (``backend.scoring.model.score_candidates``) and the
Streamlit display/export paths must order rows identically — otherwise a freshly
scored table and the same run re-read from history could disagree. Keeping the
rule in one place is what makes "ranking is deterministic" hold everywhere.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def sort_by_final_score(frame: pd.DataFrame) -> pd.DataFrame:
    """Return a copy of ``frame`` ordered by ``final_score`` descending.

    Highest score first; null/non-finite scores last; original row order is the
    explicit tie-breaker so equal scores never reshuffle between runs (stable
    ``mergesort``). A frame without a ``final_score`` column is returned as an
    unchanged copy, so display paths can call this unconditionally.
    """
    if "final_score" not in frame.columns:
        return frame.copy(deep=True)

    ranked = frame.copy(deep=True)
    # Temporary helper columns are dropped before returning, so callers never see
    # them. ``_rank_original_order`` is the deterministic tie-breaker.
    ranked["_rank_original_order"] = range(len(ranked))
    # Coerce quietly: one unparsable/non-finite score becomes "unscored" (sorted
    # last) instead of breaking the whole sort.
    ranked["_rank_final_score"] = pd.to_numeric(ranked["final_score"], errors="coerce")
    ranked["_rank_final_score"] = ranked["_rank_final_score"].where(
        np.isfinite(ranked["_rank_final_score"]),
        np.nan,
    )
    return (
        ranked.sort_values(
            by=["_rank_final_score", "_rank_original_order"],
            ascending=[False, True],
            na_position="last",
            kind="mergesort",
        )
        .drop(columns=["_rank_original_order", "_rank_final_score"])
        .reset_index(drop=True)
    )
