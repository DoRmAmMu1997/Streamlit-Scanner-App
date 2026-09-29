"""Shared usage/billing-limit classification for every Claude Agent SDK runner.

Beginner note:
    The Fundamentals, Technical, 67-ka-Funda and IPO extraction agents all
    need to tell "the plan's usage or billing limit refused this run" apart
    from a genuine bug. Keeping the one marker list in this dependency-free
    leaf means a newly observed refusal message is recognized by every agent
    at once, instead of drifting between forked copies.
"""

from __future__ import annotations

# Substrings that mark a usage/limit failure in *unstructured* CLI error text.
# Structured SDK signals (RateLimitEvent, AssistantMessage.error) are checked
# first by each runner; this list is only the fallback for raw error messages.
USAGE_LIMIT_MARKERS = (
    "rate limit",
    "usage limit",
    "limit reached",
    "out of credit",
    "credit balance",
    "quota",
    "billing",
)


def mentions_usage_limit(*texts: str | None) -> bool:
    """Return whether unstructured CLI text reads like a usage/billing refusal.

    Args:
        *texts: Optional exception/diagnostic strings used only for classification.

    Returns:
        True if a known usage-limit marker occurs, otherwise False.

    Beginner note:
        This is classification, never presentation: the text may contain
        credentials or command paths, so callers raise a fixed typed error
        instead of showing the matched provider message.
    """
    haystack = " ".join(text for text in texts if text).lower()
    return any(marker in haystack for marker in USAGE_LIMIT_MARKERS)
