"""Deterministic IPO scoring package: weighted score model and binary verdict.

This package groups the pure scoring stages that IPO-001 introduced and later
tickets extend. ``score_model`` turns seven normalized factor assessments into
the 100-point weighted receipt, and ``recommendation`` maps that receipt onto
the binary, fail-closed verdict.

Beginner note: callers should import these names from :mod:`backend.ipo` (the
subsystem facade) rather than reaching into this package directly. The facade
is the reviewed public surface; this ``__init__`` only keeps the package's own
internal wiring in one obvious place.
"""

from __future__ import annotations

from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    CRITICAL_FACTORS,
    OPTIONAL_FACTORS,
    SKIP,
    build_recommendation,
)
from backend.ipo.scoring.score_model import PDF_WEIGHTS, score_ipo

__all__ = [
    "APPLY_AND_HOLD",
    "APPLY_FOR_LISTING_GAINS",
    "CRITICAL_FACTORS",
    "OPTIONAL_FACTORS",
    "PDF_WEIGHTS",
    "SKIP",
    "build_recommendation",
    "score_ipo",
]
