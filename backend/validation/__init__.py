"""VALID-002 public surface for historical signal validation."""

from backend.validation.benchmarks import (
    BENCHMARKS,
    BenchmarkLeg,
    BenchmarkSpec,
    benchmark_for_universe,
    compute_benchmark_leg,
    resolve_index_security_ids,
)
from backend.validation.forward_return import (
    FORWARD_RETURN_HORIZONS,
    ForwardReturnPoint,
    compute_forward_return,
)
from backend.validation.metrics import (
    BestWorstSignal,
    ValidationBenchmarkRow,
    ValidationDashboardSummary,
    ValidationMetricFilters,
    ValidationMetricRow,
    ValidationReturnBucket,
    ValidationSectorConcentrationRow,
    ValidationSummary,
    ValidationTimeSeriesPoint,
    summarize_validation_dashboard,
    summarize_validation_metrics,
)
from backend.validation.sectors import load_universe_sector_lookup
from backend.validation.service import (
    ForwardReturnRunSummary,
    compute_pending_forward_returns,
)

__all__ = [
    "BENCHMARKS",
    "FORWARD_RETURN_HORIZONS",
    "BenchmarkLeg",
    "BenchmarkSpec",
    "BestWorstSignal",
    "ForwardReturnPoint",
    "ForwardReturnRunSummary",
    "ValidationBenchmarkRow",
    "ValidationDashboardSummary",
    "ValidationMetricFilters",
    "ValidationMetricRow",
    "ValidationReturnBucket",
    "ValidationSectorConcentrationRow",
    "ValidationSummary",
    "ValidationTimeSeriesPoint",
    "benchmark_for_universe",
    "compute_benchmark_leg",
    "compute_forward_return",
    "compute_pending_forward_returns",
    "load_universe_sector_lookup",
    "resolve_index_security_ids",
    "summarize_validation_dashboard",
    "summarize_validation_metrics",
]
