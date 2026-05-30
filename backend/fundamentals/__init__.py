"""Fundamental analysis subsystem.

Public surface:
- `fetch_company_data(symbol, ...)` — scrape and parse one screener.in page.
- `FundamentalsCache` — on-disk JSON cache for fetched data and agent verdicts.
- `FundamentalAgent` — Claude Agent SDK agent that applies the user's seven
  criteria plus its own qualitative analysis and returns an `AgentVerdict`.
- `AgentVerdict` / `CriterionResult` / `Observation` / `ForwardOutlook` —
  Pydantic schemas used by the agent's structured output.
"""

from backend.fundamentals.fundamental_agent import (
    AgentVerdict,
    CriterionResult,
    ForwardOutlook,
    FundamentalAgent,
    FundamentalsAgentError,
    FundamentalsUsageLimitError,
    Observation,
)
from backend.fundamentals.fundamentals_cache import FundamentalsCache
from backend.fundamentals.screener_in_client import (
    ScreenerInFetchError,
    fetch_company_data,
)

__all__ = [
    "AgentVerdict",
    "CriterionResult",
    "ForwardOutlook",
    "FundamentalAgent",
    "FundamentalsAgentError",
    "FundamentalsUsageLimitError",
    "FundamentalsCache",
    "Observation",
    "ScreenerInFetchError",
    "fetch_company_data",
]
