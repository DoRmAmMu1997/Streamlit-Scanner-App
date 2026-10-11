"""Shared database fixtures for the scan-history test suite.

Before this file existed, five test modules each carried their own copy of the
same recipe: build a throwaway SQLite engine, enable foreign-key enforcement,
create the ORM schema, and wrap sessions in a commit-on-success /
rollback-on-error factory shaped like ``backend.storage.database.session_scope``.
Centralizing the recipe keeps the copies from drifting apart.

Two engine flavors exist on purpose:

- ``db_engine`` is in-memory SQLite: fastest, vanishes with the engine, ideal
  for repository/model/service unit tests that run inside one connection.
- ``file_db_engine`` is a file-backed temp database built with the production
  ``_make_engine`` factory, so integration tests exercise the exact pragmas the
  real ``data/scanner.db`` uses (foreign keys, WAL, busy timeout) and separate
  sessions can observe each other's committed rows.

Module-specific fakes, builders, and non-database fixtures stay in their own
test files - this conftest only owns the database recipe.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import AbstractContextManager, contextmanager

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.storage.database import _make_engine
from backend.storage.models import Base

SessionFactory = Callable[[], AbstractContextManager[Session]]


def _session_scope_factory(engine: Engine) -> SessionFactory:
    """Build a ``session_scope``-shaped factory bound to a test engine.

    Mirrors ``backend.storage.database.session_scope`` (commit on success,
    rollback on error) but points at the test database instead of the real one.
    """

    @contextmanager
    def factory() -> Iterator[Session]:
        with Session(engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return factory


@pytest.fixture
def db_engine() -> Iterator[Engine]:
    """An in-memory SQLite engine with the scan-history tables created."""
    engine = create_engine("sqlite://", future=True)

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        # Match the production SQLite engine behavior so relationship and raw
        # database cascades are both available in tests.
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def db_session(db_engine: Engine) -> Iterator[Session]:
    """Yield one Session on the in-memory engine for direct ORM tests."""
    with Session(db_engine) as active_session:
        yield active_session


@pytest.fixture
def session_factory(db_engine: Engine) -> SessionFactory:
    """A transactional session factory bound to the in-memory engine."""
    return _session_scope_factory(db_engine)


@pytest.fixture
def file_db_engine(tmp_path) -> Iterator[Engine]:
    """A file-backed temp SQLite engine built by the production factory.

    Beginner note:
    A file-backed temp DB is still fast, but it is closer to the real
    ``data/scanner.db`` than an in-memory database. Separate SQLAlchemy
    sessions can open separate connections and still see the same rows, which
    is exactly what a scan service plus history query needs to prove.
    """
    db_path = tmp_path / "test_scan_history.db"
    engine = _make_engine(f"sqlite:///{db_path.as_posix()}")
    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def file_session_factory(file_db_engine: Engine) -> SessionFactory:
    """A transactional session factory bound to the file-backed engine."""
    return _session_scope_factory(file_db_engine)


@pytest.fixture
def postgres_admin_url() -> str:
    """Require opt-in to a PostgreSQL test server, never an application database.

    Returns:
        Explicit provisioning URL for a role with CREATEDB permission.

    Beginner note:
        Absence is an honest local skip. Once configured, every connection error
        fails the tests. CI sets this variable and the separate required flag so
        an accidentally removed URL cannot turn its PostgreSQL lane green.
    """
    import os

    value = os.environ.get("SCANNER_TEST_POSTGRES_ADMIN_URL", "").strip()
    if not value:
        if os.environ.get("SCANNER_REQUIRE_POSTGRES_TESTS") == "1":
            pytest.fail("SCANNER_TEST_POSTGRES_ADMIN_URL is required for this test lane")
        pytest.skip("No disposable PostgreSQL test server configured")
    return value


@pytest.fixture
def postgres_database_pair(postgres_admin_url: str) -> Iterator[tuple[str, str]]:
    """Own two private databases for migrated-versus-ORM schema comparison.

    Yields:
        URLs for the migration database and the ORM reference database.

    Beginner note:
        Nested cleanup drops the first database even if provisioning the second
        fails. Neither fixture rebuilds a shared schema or uses DATABASE_URL.
    """
    from backend.storage.verification_database import disposable_postgres_database

    with (
        disposable_postgres_database(postgres_admin_url) as migrated,
        disposable_postgres_database(postgres_admin_url) as metadata,
    ):
        yield migrated, metadata


@pytest.fixture
def postgres_session_factory(postgres_admin_url: str) -> Iterator[SessionFactory]:
    """Yield independent transactions on one private, fully migrated database.

    Args:
        postgres_admin_url: Explicit test-server provisioning URL, never DATABASE_URL.

    Yields:
        Commit-on-success session scopes using distinct pooled connections.

    Beginner note:
        Migrate synchronously before any worker starts: Alembic temporarily reads
        DATABASE_URL, which is process-wide. Real PostgreSQL transactions then
        exercise row locking rather than SQLite's database-wide writer lock.
        Server-side timeouts bound broken locks even if a worker cannot finish;
        dispose all pooled connections before dropping the UUID-owned database.
    """
    from alembic import command

    from backend.storage.verification_database import disposable_postgres_database
    from backend.storage.verification_smoke import _migration_url, migration_config

    with disposable_postgres_database(postgres_admin_url) as database_url:
        with _migration_url(database_url):
            command.upgrade(migration_config(), "head")
        engine = create_engine(
            database_url,
            future=True,
            connect_args={
                "connect_timeout": 5,
                "options": "-c statement_timeout=15000 -c lock_timeout=10000",
            },
        )
        try:
            yield _session_scope_factory(engine)
        finally:
            engine.dispose()
