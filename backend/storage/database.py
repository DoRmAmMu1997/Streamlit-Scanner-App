"""Database engine, session factory, and SQLite defaults for scan history.

Beginner note:
SQLAlchemy splits database work into a few layers:

1. An ``Engine`` knows how to connect to the database named by the URL.
2. A ``Session`` is the short-lived unit of work that loads, inserts, updates,
   and commits ORM objects.
3. The ORM models in ``models.py`` describe table shapes, but they do not open
   connections by themselves.

This module owns layer 1 and layer 2. The rest of the app should ask this file
for sessions, then ask ``repository.py`` to do actual scan-history reads/writes.
That separation keeps Streamlit reruns from accidentally holding stale database
state across button clicks.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import DATA_DIR, _clean_env_value, load_environment
from backend.storage.models import Base


def get_database_url() -> str:
    """Return the database URL that SCAN-002 should use.

    ``DATABASE_URL`` wins when it is set because deployment can point the app at
    Postgres without changing code. Local development falls back to a SQLite file
    under ``data/``. That file is ignored by git, so real scan history and local
    experiments never get committed by accident.
    """
    load_environment()
    url = _clean_env_value(os.getenv("DATABASE_URL"))
    if url:
        return url

    # ``DATA_DIR`` is the same runtime data folder used by the candle and
    # fundamentals caches. Creating it here makes `alembic upgrade head` work in
    # a fresh checkout before any scanner has run.
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(DATA_DIR / 'scanner.db').as_posix()}"


def _make_engine(url: str | None = None) -> Engine:
    """Create an engine with the SQLite settings this Streamlit app needs.

    The helper accepts an optional URL so tests can point it at a temporary
    SQLite database without touching the real ``data/scanner.db`` file. The
    module-level ``engine`` below uses the default environment-backed URL.
    """
    database_url = url or get_database_url()
    connect_args = {}
    if database_url.startswith("sqlite"):
        # Streamlit can re-run the app and use worker threads while the same
        # module-level engine stays imported. SQLite defaults to one-thread-only
        # connections, so we relax that guard and keep sessions short-lived.
        connect_args["check_same_thread"] = False

    created_engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(created_engine, "connect")
        def _apply_sqlite_pragmas(dbapi_connection, _connection_record):
            # SQLite parses FOREIGN KEY declarations but does not enforce them
            # unless this pragma is enabled for every new connection. Without
            # it, deleting a scan_runs row would leave orphan scan_results rows.
            dbapi_connection.execute("PRAGMA foreign_keys=ON")
            # Wait for a write lock instead of immediately raising "database is
            # locked" when a Streamlit rerun reads while a scan writes. pysqlite
            # already defaults to 5000ms; setting it keeps that budget explicit
            # and stable even if the driver default changes.
            dbapi_connection.execute("PRAGMA busy_timeout=5000")
            # WAL lets the history page read while a scan writes, which a default
            # DELETE-journal database cannot do. WAL persists in the database file,
            # so this is effectively a no-op after the first connection.
            dbapi_connection.execute("PRAGMA journal_mode=WAL")

    return created_engine


# Keep the engine and session factory at module scope. Creating an engine is
# cheap-ish but not free, and SQLAlchemy is designed for one reusable engine per
# database URL. Sessions created from this factory are still per-operation.
engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)


def init_db() -> None:
    """Create tables from ORM metadata for local experiments and tests.

    Alembic migrations are the production path. This helper exists for small
    local scripts that need a quick throwaway database.
    """
    Base.metadata.create_all(engine)


@contextmanager
def session_scope() -> Iterator[Session]:
    """Open one short-lived transaction and close it safely.

    Usage pattern for future service code:

    ``with session_scope() as session:``
        call repository helpers with that session

    If the block exits normally, changes are committed. If any exception
    escapes, the transaction is rolled back so a half-written scan run does not
    remain in the database.
    """
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
