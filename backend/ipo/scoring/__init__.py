"""Deterministic IPO scoring package: factors, flags, score model, verdict.

This package groups the pure scoring stages: ``factor_derivation`` turns
typed evidence into seven 0-100 factor assessments, ``caution_flags``
evaluates the hard red-line checks, ``score_model`` produces the 100-point
weighted receipt, and ``recommendation`` maps everything onto the binary,
fail-closed verdict.

Beginner note: callers should import these names from :mod:`backend.ipo` (the
subsystem facade) rather than reaching into this package directly. The facade
is the reviewed public surface; this ``__init__`` only keeps the package's own
internal wiring in one obvious place.
"""

from __future__ import annotations

from backend.ipo.scoring.caution_flags import (
    CAUTION_FLAG_ORDER,
    CAUTION_FLAGS_VERSION,
    evaluate_caution_flags,
)
from backend.ipo.scoring.factor_derivation import (
    FACTOR_MODEL_VERSION,
    GMP_SIGNAL_MAX_AGE_DAYS,
    IpoFactorInputs,
    derive_score_input,
)
from backend.ipo.scoring.recommendation import (
    APPLY_AND_HOLD,
    APPLY_FOR_LISTING_GAINS,
    CRITICAL_FACTORS,
    INSUFFICIENT_VERIFIED_DATA,
    OPTIONAL_FACTORS,
    SKIP,
    build_recommendation,
)
from backend.ipo.scoring.score_model import PDF_WEIGHTS, score_ipo

__all__ = [
    "APPLY_AND_HOLD",
    "APPLY_FOR_LISTING_GAINS",
    "CAUTION_FLAGS_VERSION",
    "CAUTION_FLAG_ORDER",
    "CRITICAL_FACTORS",
    "FACTOR_MODEL_VERSION",
    "GMP_SIGNAL_MAX_AGE_DAYS",
    "INSUFFICIENT_VERIFIED_DATA",
    "OPTIONAL_FACTORS",
    "PDF_WEIGHTS",
    "SKIP",
    "IpoFactorInputs",
    "build_recommendation",
    "derive_score_input",
    "evaluate_caution_flags",
    "score_ipo",
]
