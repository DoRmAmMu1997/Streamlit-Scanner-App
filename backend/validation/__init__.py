"""VALID-002 public surface for historical signal validation."""

from backend.validation.benchmarks import (
    BENCHMARKS,
    BenchmarkLeg,
    BenchmarkSpec,
    benchmark_for_universe,
    compute_benchmark_leg,
)
from backend.validation.forward_return import (
    FORWARD_RETURN_HORIZONS,
    ForwardReturnPoint,
    compute_forward_return,
)
from backend.validation.service import (
    ForwardReturnRunSummary,
    compute_pending_forward_returns,
)

__all__ = [
    "BENCHMARKS",
    "FORWARD_RETURN_HORIZONS",
    "BenchmarkLeg",
    "BenchmarkSpec",
    "ForwardReturnPoint",
    "ForwardReturnRunSummary",
    "benchmark_for_universe",
    "compute_benchmark_leg",
    "compute_forward_return",
    "compute_pending_forward_returns",
]
