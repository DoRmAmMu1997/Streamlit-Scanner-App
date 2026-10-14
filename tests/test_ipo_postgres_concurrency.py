"""IPO scoring transaction contracts against disposable migrated PostgreSQL.

Beginner note:
    SQLite's single-writer behavior cannot prove PostgreSQL row-lock semantics.
    These tests opt in through SCANNER_TEST_POSTGRES_ADMIN_URL and use separate
    real sessions. Synchronization pauses calculation, never replaces persistence.
    Missing local PostgreSQL is a skip; the required CI lane treats absence as
    failure. No shared application database or external evidence source is used.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from threading import Barrier, Event

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_ipo_scoring_service import _AS_OF, _issue_data, _scored_issue

from backend.ipo import repository
from backend.ipo.repository import SessionFactory
from backend.ipo.scoring import service
from backend.ipo.scoring.state import IpoScoringSnapshot
from backend.storage.ipo_repository import get_ipo_scoring_state_values, update_ipo_issue_row
from backend.storage.models import IpoRecommendation, IpoScore


def _history_counts(session: Session, issue_id: int) -> tuple[int, int]:
    """Count both halves of persisted evaluation history independently.

    Args:
        session: Caller-owned inspection transaction.
        issue_id: Issue whose immutable history is inspected.

    Returns:
        Score count and recommendation count, including incomplete pairs.

    Beginner note:
        The public history list joins complete pairs. Counting both tables also
        catches an orphan insert that a successful joined read could conceal.
    """
    scores = session.scalar(select(func.count()).select_from(IpoScore).where(IpoScore.issue_id == issue_id))
    recommendations = session.scalar(
        select(func.count()).select_from(IpoRecommendation).where(
            IpoRecommendation.score_id.in_(select(IpoScore.id).where(IpoScore.issue_id == issue_id))
        )
    )
    assert scores is not None and recommendations is not None
    return scores, recommendations


def test_postgres_identical_concurrent_calculations_publish_one_pair(
    postgres_session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Concurrent identical jobs return the same complete, current evaluation.

    Beginner note:
        Both jobs finish their real reads before either publishes. Removing the
        publication serialization/idempotency guard would allow duplicate pairs,
        leak an integrity failure, or leave current state pointing at other history.
        A barrier establishes the overlap; no elapsed-time assertion is involved.
    """
    issue = _scored_issue(postgres_session_factory, tmp_path)
    snapshots_ready = Barrier(2)
    load_snapshot = service.load_ipo_scoring_snapshot

    def synchronized_snapshot(
        issue_id: int, *, as_of: dt.datetime, session_factory: SessionFactory = postgres_session_factory
    ) -> IpoScoringSnapshot:
        """Detach real evidence, then release both calculations together.

        Args:
            issue_id: Issue being scored.
            as_of: Frozen business clock supplied by the scoring service.
            session_factory: Real transaction factory forwarded unchanged.

        Returns:
            The unchanged, revision-validated evidence snapshot.
        """
        snapshot = load_snapshot(issue_id, as_of=as_of, session_factory=session_factory)
        snapshots_ready.wait(timeout=15)
        return snapshot

    monkeypatch.setattr(service, "load_ipo_scoring_snapshot", synchronized_snapshot)
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [
            workers.submit(service.rescore_issue, issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
            for _ in range(2)
        ]
        outcomes = [future.result(timeout=30) for future in futures]
    # Restore before the current reader, which also loads a scoring snapshot.
    monkeypatch.setattr(service, "load_ipo_scoring_snapshot", load_snapshot)
    assert {outcome.status for outcome in outcomes} == {"evaluated", "skipped_unchanged"}
    evaluation = outcomes[0].evaluation
    assert evaluation is not None and outcomes[1].evaluation == evaluation
    with postgres_session_factory() as session:
        assert _history_counts(session, issue.id) == (1, 1)
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
    assert current.fresh and current.evaluation == evaluation
    assert current.snapshot.state.current_score_id == evaluation.score_id
    assert current.snapshot.state.evaluated_revision == current.snapshot.state.input_revision


def test_postgres_older_calculation_cannot_supersede_newer_revision(
    postgres_session_factory: SessionFactory, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An older detached calculation retries and reuses the newer committed pair.

    Beginner note:
        Pause after the old snapshot's transaction closes, then commit changed
        pricing and its evaluation through independent sessions. If publication
        loses its revision compare-and-set, releasing the old calculation would
        insert obsolete history or supersede the newer current receipt.
    """
    issue = _scored_issue(postgres_session_factory, tmp_path)
    old_snapshot_ready = Event()
    newer_committed = Event()
    load_snapshot = service.load_ipo_scoring_snapshot

    def paused_factory() -> Iterator[Session]:
        """Identify the old worker without changing transaction behavior.

        Yields:
            A real independent session, committed or rolled back by the fixture.

        Beginner note:
            The wrapper's identity limits the pause to the old worker. Its
            transactions still come from the shared PostgreSQL engine; no lock
            or persistence operation is mocked.
        """
        with postgres_session_factory() as session:
            yield session

    old_worker_factory = contextmanager(paused_factory)

    def pause_old_snapshot(
        issue_id: int, *, as_of: dt.datetime, session_factory: SessionFactory = postgres_session_factory
    ) -> IpoScoringSnapshot:
        """Hold only the old worker's first detached evidence bundle.

        Args:
            issue_id: Issue being scored.
            as_of: Service's frozen business time.
            session_factory: Identifies the old worker while forwarding real I/O.

        Returns:
            An unchanged snapshot after the newer commit, or immediately on retry.
        """
        snapshot = load_snapshot(issue_id, as_of=as_of, session_factory=session_factory)
        if session_factory is old_worker_factory and not old_snapshot_ready.is_set():
            old_snapshot_ready.set()
            assert newer_committed.wait(timeout=20), "Newer revision was never committed"
        return snapshot

    monkeypatch.setattr(service, "load_ipo_scoring_snapshot", pause_old_snapshot)
    with ThreadPoolExecutor(max_workers=1) as workers:
        older = workers.submit(service.rescore_issue, issue.id, as_of=_AS_OF, session_factory=old_worker_factory)
        try:
            assert old_snapshot_ready.wait(timeout=15), "Old worker did not capture its snapshot"
            repository.update_issue(
                issue.id, _issue_data(price_band_high=Decimal("300")), session_factory=postgres_session_factory
            )
            newer = service.rescore_issue(issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
            assert newer.status == "evaluated" and newer.evaluation is not None
            committed = service.get_current_evaluation(
                issue.id, as_of=_AS_OF, session_factory=postgres_session_factory
            )
        finally:
            # Release on assertion/error too, so executor cleanup never hangs.
            newer_committed.set()
        retried = older.result(timeout=30)
    assert retried.status == "skipped_unchanged" and retried.evaluation == newer.evaluation
    assert repository.list_evaluations(issue.id, session_factory=postgres_session_factory) == [newer.evaluation]
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
    assert current.fresh and current.evaluation == newer.evaluation
    assert current.snapshot.state.input_revision == committed.snapshot.state.input_revision
    assert current.snapshot.state.evaluated_revision == committed.snapshot.state.input_revision
    with postgres_session_factory() as session:
        assert _history_counts(session, issue.id) == (1, 1)


def test_postgres_caller_rollback_restores_evidence_revision_and_selection(
    postgres_session_factory: SessionFactory, tmp_path: Path
) -> None:
    """An outer caller failure rolls back input and nested score publication.

    Beginner note:
        A service may flush and use a savepoint, but the caller owns commit.
        Independent readers must see the original receipt while writes are open,
        and after rollback both the evidence token and immutable history must be
        restored. This catches an internal commit or escaped savepoint insertion.
    """
    issue = _scored_issue(postgres_session_factory, tmp_path)
    original = service.rescore_issue(issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
    assert original.evaluation is not None
    with postgres_session_factory() as session:
        original_state = get_ipo_scoring_state_values(session, issue.id)
    assert original_state is not None

    with pytest.raises(RuntimeError, match="caller aborts transaction"), postgres_session_factory() as writer:
        changed = update_ipo_issue_row(writer, issue.id, {"price_band_high": Decimal("300")})
        assert changed is not None

        @contextmanager
        def caller_session() -> Iterator[Session]:
            """Lend the outer session while retaining the caller's commit authority.

            Yields:
                The existing caller transaction, without commit or rollback.

            Beginner note:
                Snapshot reads and publication share this transaction so the
                final caller rollback includes evidence and the nested pair.
            """
            yield writer

        staged = service.rescore_issue(issue.id, as_of=_AS_OF, session_factory=caller_session)
        assert staged.status == "evaluated" and staged.evaluation is not None
        staged_state = get_ipo_scoring_state_values(writer, issue.id)
        assert staged_state is not None and staged_state[0] > original_state[0]
        assert staged_state[1] == staged.evaluation.score_id and staged_state[2] == staged_state[0]
        assert _history_counts(writer, issue.id) == (2, 2)
        with postgres_session_factory() as reader:
            assert get_ipo_scoring_state_values(reader, issue.id) == original_state
            assert _history_counts(reader, issue.id) == (1, 1)
        raise RuntimeError("caller aborts transaction")

    with postgres_session_factory() as reader:
        assert get_ipo_scoring_state_values(reader, issue.id) == original_state
        assert _history_counts(reader, issue.id) == (1, 1)
    current = service.get_current_evaluation(issue.id, as_of=_AS_OF, session_factory=postgres_session_factory)
    assert current.fresh and current.evaluation == original.evaluation
    assert current.snapshot.inputs.issue.price_band_high == Decimal("242")
