"""AI-004 — bounded retry around strict AI-output parsing.

Beginner note (why this exists)
-------------------------------
The AI screeners ask Claude for a verdict and expect a single strict-schema JSON
object back. Models occasionally return malformed or incomplete JSON — a stray
sentence before the object, a truncated brace, or a missing required field.
PROV-003 already *rejects* such output (it becomes an ``error`` receipt) so it can
never corrupt scan results.

AI-004 adds the other half of "reject **or** retry": when the model's final answer
fails to parse/validate, give it a bounded second chance (re-run the agentic loop)
before giving up. Transient malformed output then recovers on its own, while a
persistently broken response still ends as a clean, recorded rejection.

This module is one tiny helper so all three AI agents retry the *same* way.
Crucially it retries **only** parse/validation failures — never SDK / CLI /
usage-limit errors, which a retry cannot fix and which would waste the plan's
Agent SDK credit.

When every attempt still fails, the helper raises :class:`AIValidationError`.
That single, distinct type is what makes a *validation* failure recognisable
downstream (AC3): an SDK/CLI/usage-limit error keeps its own class, so the scan
service can count "AI output failed validation" separately from "the AI was
unavailable". It subclasses ``RuntimeError`` (not a fundamentals error) to avoid
an import cycle while staying catchable by the agents' broad error handlers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import TypeVar

logger = logging.getLogger(__name__)

# The validated object the caller's ``parse_once`` returns (e.g. a Pydantic
# verdict). Generic so this helper stays agnostic to each agent's schema.
T = TypeVar("T")


class AIValidationError(RuntimeError):
    """Raised when AI output still fails strict parsing after every retry.

    The original parse/validation error is chained as ``__cause__`` and its text
    is preserved in the message, so callers that show or match on the message
    (and the agents that record ``error_type="AIValidationError"``) keep the
    underlying detail while gaining one clear, dedicated failure type.
    """


def parse_with_retry(
    run_once: Callable[[], str],
    parse_once: Callable[[str], T],
    *,
    attempts: int,
    retry_on: tuple[type[BaseException], ...],
    label: str = "AI output",
) -> T:
    """Run an AI call and strictly parse it, retrying parse failures up to ``attempts``.

    The two callables are kept separate on purpose so the helper can tell a
    *malformed-output* failure (worth retrying) apart from an *infrastructure*
    failure (not worth retrying):

    Args:
        run_once: Produces the model's raw final text for one attempt. Any
            exception it raises — SDK not installed, CLI missing, usage limit, or
            (for the 67 agent) missing/unsafe research evidence — propagates
            immediately and is **not** retried, because a retry cannot fix it.
        parse_once: Turn that text into the validated object, or raise one of
            ``retry_on`` when the output is malformed or missing required fields.
        attempts: Total tries (``>= 1``). ``2`` means one retry. Because each
            attempt re-runs ``run_once``, any per-attempt state that must start
            clean (e.g. the 67 agent's research collector) is reset inside
            ``run_once`` itself, not here.
        retry_on: The parse/validation exception types that justify a retry.
        label: Human label for the debug/warning log line.

    Returns:
        The validated object from the first attempt that parses.

    Raises:
        AIValidationError: after every attempt fails to parse/validate; the last
            ``retry_on`` error is chained as ``__cause__``.
        Exception: immediately and unwrapped, any other error raised by
            ``run_once`` (SDK/CLI/usage-limit, or the 67 agent's research-evidence
            errors) — these are not retried and keep their own type.
    """
    total = max(1, int(attempts))
    for attempt in range(1, total + 1):
        # A failure here (e.g. usage limit) is infrastructure, not malformed
        # output: it is outside the try below, so it propagates without a retry.
        text = run_once()
        try:
            return parse_once(text)
        except retry_on as exc:
            # The final attempt's failure becomes the caller's recorded
            # rejection / error receipt — surface it as the one dedicated
            # AIValidationError type (original chained) so downstream code can
            # tell a validation failure apart from an availability failure.
            if attempt >= total:
                raise AIValidationError(str(exc)) from exc
            logger.warning(
                "%s failed validation on attempt %d/%d (%s); retrying.",
                label,
                attempt,
                total,
                type(exc).__name__,
            )
    # The loop always returns or raises above; this only satisfies type checkers.
    raise RuntimeError("parse_with_retry ran zero attempts")  # pragma: no cover
