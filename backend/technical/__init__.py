"""Technical analysis agent subsystem.

Mirrors `backend/fundamentals/` but for chart-pattern / support detection. The
`TechnicalAnalysisAgent` runs on the Claude Agent SDK (Claude-subscription auth,
no API key) and is driven by the `technical_analysis` screener.

Public surface:
- `TechnicalAnalysisAgent` — reads a stock's OHLC window plus precomputed major
  support/resistance levels and returns a `TechnicalVerdict`.
- `TechnicalVerdict` — Pydantic schema for the agent's structured output.
"""

from backend.technical.technical_agent import (
    TechnicalAnalysisAgent,
    TechnicalVerdict,
)

__all__ = [
    "TechnicalAnalysisAgent",
    "TechnicalVerdict",
]
