"""Detached IPO verification contracts shared by repository and scoring service."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from backend.ipo.models import IpoEvaluationRecord
from backend.ipo.scoring.factor_derivation import IpoFactorInputs


class IpoScoringConflictError(RuntimeError):
    """Retryable conflict after evidence changed during assembly/publication.

    Beginner note:
        Callers may retry the operation later. They must never count this as a
        successfully current evaluation or expose a half-published receipt.
    """

    code = "ipo_scoring_conflict"
    retryable = True


@dataclass(frozen=True)
class IpoScoringStateRecord:
    """Scalar state detached from SQLAlchemy's identity map.

    Beginner note:
        Missing verification fields intentionally represent unverified history.
    """

    input_revision: int
    current_score_id: int | None
    evaluated_revision: int | None
    last_verified_at: dt.datetime | None


@dataclass(frozen=True)
class IpoScoringSnapshot:
    """One revision-validated evidence bundle and its selected historical pair.

    Beginner note:
        Ratios derive from the detached bundle after the read transaction closes.
        Current selection is read with the evidence, never in another transaction.
    """

    inputs: IpoFactorInputs
    state: IpoScoringStateRecord
    evaluation: IpoEvaluationRecord | None


@dataclass(frozen=True)
class IpoCurrentEvaluation:
    """Current-read receipt retaining stale history for explicitly labeled display.

    Beginner note:
        Only ``fresh`` permits an actionable verdict. The historical calculation
        time remains on evaluation; last_verified_at describes a later check.
    """

    snapshot: IpoScoringSnapshot
    fresh: bool
    reason: str

    @property
    def evaluation(self) -> IpoEvaluationRecord | None:
        """Return selected history without implying freshness."""
        return self.snapshot.evaluation

    @property
    def last_verified_at(self) -> dt.datetime | None:
        """Return actual verification wall time independently of calculation."""
        return self.snapshot.state.last_verified_at


def normalize_scoring_time(value: dt.datetime) -> dt.datetime:
    """Normalize an aware scoring clock to UTC; reject ambiguous naive times.

    Args:
        value: Explicit evaluation/render instant.

    Returns:
        The same instant in UTC, preserving the existing UTC near-close date rule.

    Raises:
        ValueError: If value has no timezone offset.

    Beginner note:
        An injected clock freezes business-time eligibility, not the real wall
        time recorded when publication verifies the evidence.
    """
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("IPO scoring time must be timezone-aware.")
    return value.astimezone(dt.UTC)
