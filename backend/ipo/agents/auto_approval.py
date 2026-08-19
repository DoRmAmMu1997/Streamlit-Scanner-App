"""IPO-011: opt-in auto-approval of fully verified extraction proposals.

The one-button screener can only be end-to-end autonomous if some proposals
convert into evidence without a human. This module is that policy, and it is
deliberately the *only* place the decision lives: the repository's
``approve_extraction_proposal`` stays identity-agnostic and keeps performing
the same strict conversion (payload revalidation plus re-verification of the
hash-verified cached PDF) no matter who asks.

Beginner note — why this is narrow on purpose:
Auto-approval is off unless an operator sets ``IPO_AUTO_APPROVE_HIGH_CONFIDENCE``,
and even then it only touches ``HIGH`` confidence proposals, which by
definition are the ones where the host already re-resolved *every* cited value
from the real PDF bytes. Anything weaker still waits for a person. The
resulting revision is attributed to a reserved automation identity, so an
autonomous approval can never be mistaken for a human attestation in the audit
trail.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from backend.audit import record_audit_event
from backend.config import get_ipo_auto_approve_high_confidence
from backend.ipo.models import (
    Confidence,
    IpoExtractionProposalStatus,
)
from backend.ipo.repository import (
    AuditRecorder,
    SessionFactory,
    approve_extraction_proposal,
    list_extraction_proposals,
)
from backend.observability import EVENT_IPO_PROPOSAL_AUTO_APPROVED, log_event
from backend.storage import session_scope

logger = logging.getLogger(__name__)

# A reserved, obviously non-human identity. It satisfies the repository's actor
# validation (exactly one "@", bounded length) while being trivially greppable
# in ``entered_by_email`` and in the audit trail.
AUTOMATION_ACTOR_EMAIL: Final = "ipo-automation@screener.local"


@dataclass(frozen=True)
class IpoAutoApprovalOutcome:
    """What one auto-approval pass converted, skipped, or failed on."""

    approved: tuple[int, ...] = ()
    skipped_low_confidence: int = 0
    failed: int = 0
    disabled: bool = False


def auto_approve_ready_proposals(
    *,
    issue_id: int | None = None,
    issue_ids: Sequence[int] | None = None,
    data_dir: Path | None = None,
    enabled: bool | None = None,
    audit_recorder: AuditRecorder = record_audit_event,
    approver: Callable[..., object] = approve_extraction_proposal,
    session_factory: SessionFactory = session_scope,
) -> IpoAutoApprovalOutcome:
    """Approve every pending HIGH-confidence proposal, when explicitly enabled.

    Args:
        issue_id: Restrict the pass to one issue; ``None`` scans the queue.
        issue_ids: Restrict the pass to a set of issues. A caller that
            processed a narrowed set of issues must pass the same set, so an
            approval can never reach an issue the run did not touch.
        data_dir: Override of the verified document-cache root (tests).
        enabled: Override of the environment switch, for tests and callers
            that already resolved the setting.
        audit_recorder: Injectable best-effort audit sink.
        approver: Injectable approval function; production uses the reviewed
            repository entry point.
        session_factory: Injectable transaction scope.

    Returns:
        A summary naming the converted proposal ids. ``disabled`` is ``True``
        when the switch is off, which is the shipped default.

    Beginner note:
        Failures are counted, never raised: one malformed proposal must not
        stop the screener run or block its siblings. A proposal that fails
        here simply stays pending for a human, which is the safe direction.
    """
    active = (
        get_ipo_auto_approve_high_confidence() if enabled is None else bool(enabled)
    )
    if not active:
        return IpoAutoApprovalOutcome(disabled=True)

    pending = list_extraction_proposals(
        issue_id=issue_id,
        status=IpoExtractionProposalStatus.PENDING,
        session_factory=session_factory,
    )
    if issue_ids is not None:
        # Approval writes evidence and mutates the issue row, so a run that
        # was narrowed (by active-status or a max-issues cap) must not reach
        # outside its own selection. Without this, a capped run silently
        # converts the entire queue and then rescores only part of it.
        wanted = set(issue_ids)
        pending = [proposal for proposal in pending if proposal.issue_id in wanted]
    approved: list[int] = []
    skipped = 0
    failed = 0
    for proposal in pending:
        if proposal.confidence is not Confidence.HIGH:
            # MEDIUM and weaker carry unverified values by definition.
            skipped += 1
            continue
        try:
            revision = approver(
                proposal.id,
                reviewed_by_email=AUTOMATION_ACTOR_EMAIL,
                data_dir=data_dir,
                session_factory=session_factory,
            )
        except Exception as exc:  # noqa: BLE001 - see the guarantee below
            # A proposal that cannot convert stays pending for a human, and
            # its siblings still process. The catch is deliberately broad
            # because approval reaches the database and the document cache:
            # an IntegrityError, an OSError reading a cached PDF, or a
            # settings failure would otherwise abort the whole screener run
            # after every earlier stage had already succeeded. Only the
            # exception's type is recorded, never its message, since upstream
            # text is untrusted.
            failed += 1
            log_event(
                logger,
                EVENT_IPO_PROPOSAL_AUTO_APPROVED,
                level=logging.WARNING,
                proposal_id=proposal.id,
                issue_id=proposal.issue_id,
                outcome="failed",
                error_type=type(exc).__name__,
            )
            continue
        approved.append(proposal.id)
        log_event(
            logger,
            EVENT_IPO_PROPOSAL_AUTO_APPROVED,
            proposal_id=proposal.id,
            issue_id=proposal.issue_id,
            outcome="approved",
            confidence=proposal.confidence.value,
        )
        audit_recorder(
            event=EVENT_IPO_PROPOSAL_AUTO_APPROVED,
            user_email=AUTOMATION_ACTOR_EMAIL,
            metadata={
                "proposal_id": proposal.id,
                "issue_id": proposal.issue_id,
                "confidence": proposal.confidence.value,
                "manual_extraction_id": getattr(revision, "id", None),
            },
            session_factory=session_factory,
        )
    return IpoAutoApprovalOutcome(
        approved=tuple(approved),
        skipped_low_confidence=skipped,
        failed=failed,
    )
