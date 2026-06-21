"""Public API for the RANK-002 scoring subsystem.

The scanner app imports from ``backend.scoring`` instead of individual files so
future scoring refactors can stay internal. The package has two layers:

- ``ScoringConfig`` describes the model knobs loaded from YAML.
- ``score_candidates`` annotates a screener result frame with ``final_score`` and
  an auditable ``score_breakdown`` receipt.
"""

from backend.scoring.config import ScoringConfig, load_scoring_config
from backend.scoring.model import ScoringContext, score_candidates
from backend.scoring.ordering import sort_by_final_score

__all__ = [
    "ScoringConfig",
    "ScoringContext",
    "load_scoring_config",
    "score_candidates",
    "sort_by_final_score",
]
