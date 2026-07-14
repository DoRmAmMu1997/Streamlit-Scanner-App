"""AI agents that draft IPO evidence proposals for human review (IPO-010).

Beginner note: nothing in this package can write evidence. Agents produce
*proposals* that an administrator must approve before scoring ever sees a
number, so a hallucinated value is a review-queue item, not a verdict input.
"""

from __future__ import annotations

from backend.ipo.agents.financial_extractor import (
    EXTRACTOR_MODEL_VERSION,
    IpoExtractionError,
    IpoExtractionErrorReceipt,
    propose_extraction,
)

__all__ = [
    "EXTRACTOR_MODEL_VERSION",
    "IpoExtractionError",
    "IpoExtractionErrorReceipt",
    "propose_extraction",
]
