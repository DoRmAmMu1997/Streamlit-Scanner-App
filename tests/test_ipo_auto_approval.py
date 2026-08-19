"""IPO-011 auto-approval policy tests.

Beginner note:
Auto-approval is the one place where AI output becomes scoring evidence with
no human in the loop, so every test here is about a boundary rather than a
happy path: the switch is off by default, only fully verified proposals ever
qualify, the approving identity is obviously not a person, and a failure
leaves the proposal pending instead of losing it.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from backend.ipo.agents.auto_approval import (
    AUTOMATION_ACTOR_EMAIL,
    auto_approve_ready_proposals,
)
from backend.ipo.models import (
    Confidence,
    IpoExtractionProposalStatus,
    IpoValidationError,
)
from backend.observability import EVENT_IPO_PROPOSAL_AUTO_APPROVED


def _proposal(proposal_id: int, confidence: Confidence) -> Any:
    """Build a pending-proposal stand-in with the fields the policy reads."""
    return SimpleNamespace(
        id=proposal_id,
        issue_id=100 + proposal_id,
        confidence=confidence,
        status=IpoExtractionProposalStatus.PENDING,
    )


def _install(monkeypatch, proposals: list[Any]) -> None:
    """Point the policy at a canned pending queue."""
    from backend.ipo.agents import auto_approval

    monkeypatch.setattr(
        auto_approval, "list_extraction_proposals", lambda **_kwargs: proposals
    )


def test_disabled_by_default_touches_nothing(monkeypatch) -> None:
    """The shipped default must behave exactly like the reviewed flow."""
    _install(monkeypatch, [_proposal(1, Confidence.HIGH)])

    def _must_not_approve(*_args: Any, **_kwargs: Any) -> Any:
        """Fail loudly if the disabled policy approves anything."""
        raise AssertionError("auto-approval must not run when disabled")

    outcome = auto_approve_ready_proposals(
        enabled=False, approver=_must_not_approve, session_factory=object
    )

    assert outcome.disabled is True
    assert outcome.approved == ()


def test_only_high_confidence_proposals_are_approved(monkeypatch) -> None:
    """MEDIUM and weaker proposals keep waiting for a person.

    Beginner note:
        HIGH means the host re-resolved every cited value from the real PDF
        bytes. MEDIUM carries values it could not independently confirm, which
        is exactly the case a human must adjudicate.
    """
    _install(
        monkeypatch,
        [
            _proposal(1, Confidence.HIGH),
            _proposal(2, Confidence.MEDIUM),
            _proposal(3, Confidence.LOW),
        ],
    )
    approved_ids: list[int] = []

    def _approver(proposal_id: int, **_kwargs: Any) -> Any:
        """Record the approval and return a revision-like object."""
        approved_ids.append(proposal_id)
        return SimpleNamespace(id=900 + proposal_id)

    outcome = auto_approve_ready_proposals(
        enabled=True,
        approver=_approver,
        audit_recorder=lambda **_kwargs: True,
        session_factory=object,
    )

    assert approved_ids == [1]
    assert outcome.approved == (1,)
    assert outcome.skipped_low_confidence == 2
    assert outcome.failed == 0


def test_approval_is_attributed_to_the_automation_identity(monkeypatch) -> None:
    """An autonomous approval must never look like a human attestation."""
    _install(monkeypatch, [_proposal(1, Confidence.HIGH)])
    seen: dict[str, Any] = {}
    audits: list[dict[str, Any]] = []

    def _approver(proposal_id: int, **kwargs: Any) -> Any:
        """Capture the actor the policy attributes the revision to."""
        seen.update({"proposal_id": proposal_id, **kwargs})
        return SimpleNamespace(id=42)

    def _record_audit(**kwargs: Any) -> bool:
        """Capture the audit payload like the real best-effort sink."""
        audits.append(kwargs)
        return True

    auto_approve_ready_proposals(
        enabled=True,
        approver=_approver,
        audit_recorder=_record_audit,
        session_factory=object,
    )

    assert seen["reviewed_by_email"] == AUTOMATION_ACTOR_EMAIL
    assert "@" in AUTOMATION_ACTOR_EMAIL
    assert audits[0]["event"] == EVENT_IPO_PROPOSAL_AUTO_APPROVED
    assert audits[0]["user_email"] == AUTOMATION_ACTOR_EMAIL
    assert audits[0]["metadata"]["manual_extraction_id"] == 42
    assert audits[0]["metadata"]["confidence"] == Confidence.HIGH.value


def test_one_failing_proposal_stays_pending_without_blocking_siblings(
    monkeypatch,
) -> None:
    """A proposal that cannot convert is counted, not raised, not lost."""
    _install(monkeypatch, [_proposal(1, Confidence.HIGH), _proposal(2, Confidence.HIGH)])
    approved_ids: list[int] = []

    def _approver(proposal_id: int, **_kwargs: Any) -> Any:
        """Reject the first proposal the way the repository would."""
        if proposal_id == 1:
            raise IpoValidationError("stale citation binding")
        approved_ids.append(proposal_id)
        return SimpleNamespace(id=902)

    outcome = auto_approve_ready_proposals(
        enabled=True,
        approver=_approver,
        audit_recorder=lambda **_kwargs: True,
        session_factory=object,
    )

    assert approved_ids == [2]
    assert outcome.approved == (2,)
    assert outcome.failed == 1


def test_environment_switch_drives_the_default(monkeypatch) -> None:
    """With no explicit override the env var decides, and it defaults off."""
    from backend.ipo.agents import auto_approval

    _install(monkeypatch, [_proposal(1, Confidence.HIGH)])
    monkeypatch.setattr(
        auto_approval, "get_ipo_auto_approve_high_confidence", lambda: False
    )
    assert auto_approve_ready_proposals(session_factory=object).disabled is True

    monkeypatch.setattr(
        auto_approval, "get_ipo_auto_approve_high_confidence", lambda: True
    )
    outcome = auto_approve_ready_proposals(
        approver=lambda proposal_id, **_kwargs: SimpleNamespace(id=proposal_id),
        audit_recorder=lambda **_kwargs: True,
        session_factory=object,
    )
    assert outcome.disabled is False
    assert outcome.approved == (1,)
