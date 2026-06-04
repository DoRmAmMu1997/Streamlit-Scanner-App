"""Database engine, session factory, and SQLite defaults for scan history."""

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
    """Return DATABASE_URL, falling back to a local git-ignored SQLite file."""
    load_environment()
    url = _clean_env_value(os.getenv("DATABASE_URL"))
    if url:
        return url

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(DATA_DIR / 'scanner.db').as_posix()}"


def _make_engine(url: str | None = None) -> Engine:
    """Create an engine with the SQLite settings this Streamlit app needs."""
    database_url = url or get_database_url()
    connect_args = {}
    if database_url.startswith("sqlite"):
        connect_args["check_same_thread"] = False

    created_engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(created_engine, "connect")
        def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
            dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return created_engine


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
    """Open a short-lived transaction and close it after commit or rollback."""
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
