"""Reusable data-quality checks for market-data inputs (DATA-001).

This package re-exports the candle checker's public surface so callers can write
``from backend.data_quality import validate_candles`` without knowing which module
it lives in. The implementation (and a full explanation of every check) is in
``candles.py``.
"""

from backend.data_quality.candles import (
    CandleQualityReport,
    DataQualityFinding,
    validate_candles,
)

__all__ = [
    "CandleQualityReport",
    "DataQualityFinding",
    "validate_candles",
]
