"""IPO-011 auto-approval policy tests.

Beginner note:
Auto-approval is the one place where AI output becomes scoring evidence with
no human in the loop, so every test here is about a boundary rather than a
happy path: the switch is off by default, only fully verified proposals ever
qualify, the approving identity is obviously not a person, and a failure
leaves the proposal pending instead of losing it.
"""

from __future__ import annotations

import datetime as dt
from types import SimpleNamespace
from typing import Any, cast

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
from backend.storage import SessionFactory


def _proposal(proposal_id: int, confidence: Confidence) -> Any:
    """Build a pending-proposal stand-in with the fields the policy reads."""
    return SimpleNamespace(
        id=proposal_id,
        issue_id=100 + proposal_id,
        document_id=proposal_id,
        baseline_manual_revision=0,
        needs_review_reasons=(),
        payload={},
        confidence=confidence,
        status=IpoExtractionProposalStatus.PENDING,
    )


def _install(monkeypatch, proposals: list[Any]) -> None:
    """Point the policy at a canned pending queue."""
    from backend.ipo.agents import auto_approval

    monkeypatch.setattr(
        auto_approval, "list_extraction_proposals", lambda **_kwargs: proposals
    )
    monkeypatch.setattr(
        auto_approval,
        "list_documents",
        lambda issue_id, **_kwargs: [
            SimpleNamespace(id=proposal.document_id, document_type="rhp", filing_date=dt.date(2025, 2, 1))
            for proposal in proposals if proposal.issue_id == issue_id
        ],
        raising=False,
    )


def test_disabled_by_default_touches_nothing(monkeypatch) -> None:
    """The shipped default must behave exactly like the reviewed flow."""
    _install(monkeypatch, [_proposal(1, Confidence.HIGH)])

    def _must_not_approve(*_args: Any, **_kwargs: Any) -> Any:
        """Fail loudly if the disabled policy approves anything."""
        raise AssertionError("auto-approval must not run when disabled")

    outcome = auto_approve_ready_proposals(
        enabled=False, approver=_must_not_approve, session_factory=cast(SessionFactory, object)
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
        session_factory=cast(SessionFactory, object),
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
        session_factory=cast(SessionFactory, object),
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
        session_factory=cast(SessionFactory, object),
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
    assert auto_approve_ready_proposals(session_factory=cast(SessionFactory, object)).disabled is True

    monkeypatch.setattr(
        auto_approval, "get_ipo_auto_approve_high_confidence", lambda: True
    )
    outcome = auto_approve_ready_proposals(
        approver=lambda proposal_id, **_kwargs: SimpleNamespace(id=proposal_id),
        audit_recorder=lambda **_kwargs: True,
        session_factory=cast(SessionFactory, object),
    )
    assert outcome.disabled is False
    assert outcome.approved == (1,)



def test_current_document_policy_never_falls_back_to_ready_older_draft():
    """Use full inventory chronology without falling back to an older draft.

    Beginner note:
        Queue order previously let an older DRHP replace a newer RHP. Selection
        must be stable under reordered input, while missing/tied/contradictory
        dates require review. Proposal readiness does not affect this decision.
    """
    from backend.ipo import approval

    assert hasattr(approval, "current_document_id"), "automation needs an inventory-based current-source policy"
    # The pure selector reads only these three fields; full document/cache
    # ownership is exercised by the repository approval tests separately.
    older: Any = SimpleNamespace(id=1, document_type="drhp", filing_date=dt.date(2025, 1, 1))
    latest: Any = SimpleNamespace(id=2, document_type="rhp", filing_date=dt.date(2025, 2, 1))
    assert approval.current_document_id([older, latest]) == 2
    assert approval.current_document_id([latest, older]) == 2
    assert approval.current_document_id([older]) == 1
    assert approval.current_document_id([]) is None
    assert approval.current_document_id([latest, cast(Any, SimpleNamespace(
        id=3, document_type="drhp", filing_date=None))]) is None
    assert approval.current_document_id([older, latest, cast(Any, SimpleNamespace(
        id=3, document_type="rhp", filing_date=latest.filing_date))]) is None
    assert approval.current_document_id([older, cast(Any, SimpleNamespace(
        id=2, document_type="rhp", filing_date=None))]) is None
    assert approval.current_document_id([latest, cast(Any, SimpleNamespace(
        id=3, document_type="drhp", filing_date=dt.date(2025, 3, 1)))]) is None


def test_automatic_queue_approves_only_current_source_for_each_issue(monkeypatch) -> None:
    """A ready older draft must not win merely because the queue returns it first.

    Beginner note:
        Previously both proposals could create evidence, and the last processed
        one became the newest revision. Selection must use the document inventory
        and request a second authority check inside the approval transaction.
    """
    from backend.ipo.agents import auto_approval

    old, current = _proposal(1, Confidence.HIGH), _proposal(2, Confidence.HIGH)
    current.issue_id = old.issue_id
    _install(monkeypatch, [old, current])
    monkeypatch.setattr(auto_approval, "list_documents", lambda *_args, **_kwargs: [
        SimpleNamespace(id=1, document_type="drhp", filing_date=dt.date(2025, 1, 1)),
        SimpleNamespace(id=2, document_type="rhp", filing_date=dt.date(2025, 2, 1)),
    ])
    calls: list[tuple[int, bool]] = []

    def approve(proposal_id: int, **kwargs: Any) -> Any:
        """Record the selection and whether final transactional checking is requested."""
        calls.append((proposal_id, kwargs.get("require_current_candidate", False)))
        return SimpleNamespace(id=900)

    outcome = auto_approve_ready_proposals(
        enabled=True, approver=approve, audit_recorder=lambda **_kwargs: True,
        session_factory=cast(SessionFactory, object),
    )
    assert calls == [(2, True)]
    assert outcome.approved == (2,)


def test_unready_current_source_never_falls_back_to_an_older_proposal(monkeypatch) -> None:
    """Low confidence on the newest RHP cannot grant authority to an older DRHP.

    Beginner note:
        Readiness and chronology are separate checks. The source selector must
        inspect the complete inventory before the queue's confidence filter.
    """
    from backend.ipo.agents import auto_approval

    old, current = _proposal(1, Confidence.HIGH), _proposal(2, Confidence.LOW)
    current.issue_id = old.issue_id
    _install(monkeypatch, [old, current])
    monkeypatch.setattr(auto_approval, "list_documents", lambda *_args, **_kwargs: [
        SimpleNamespace(id=1, document_type="drhp", filing_date=dt.date(2025, 1, 1)),
        SimpleNamespace(id=2, document_type="rhp", filing_date=dt.date(2025, 2, 1)),
    ])
    calls: list[int] = []
    outcome = auto_approve_ready_proposals(
        enabled=True, approver=lambda proposal_id, **_kwargs: calls.append(proposal_id),
        audit_recorder=lambda **_kwargs: True, session_factory=cast(SessionFactory, object),
    )
    assert calls == []
    assert outcome.skipped_low_confidence == 1
    assert outcome.skipped_needs_review == 1
