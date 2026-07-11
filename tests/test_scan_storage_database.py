"""Tests for the SCAN-002 database connection helpers.

These tests stay close to the database layer instead of the repository layer.
They answer beginner-level questions like: which URL gets used, does SQLite
really enforce cascades, and does ``session_scope`` commit/rollback correctly?
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.config import DATA_DIR
from backend.storage.models import Base, ScanResult, ScanRun


def _make_run() -> ScanRun:
    """Build the smallest valid parent run row used by database-layer tests."""
    return ScanRun(
        started_at=dt.datetime(2026, 6, 4, 9, 30, tzinfo=dt.UTC),
        screener_key="envelope",
        universe_key="nifty_500",
    )


def test_get_database_url_prefers_clean_env_value(monkeypatch):
    """DATABASE_URL should override the local SQLite default.

    The value is intentionally wrapped in quotes and spaces to match common .env
    edits. ``_clean_env_value`` should normalize that before SQLAlchemy sees it.
    """
    from backend.storage.database import get_database_url

    monkeypatch.setenv("DATABASE_URL", ' "sqlite:///tmp/custom-scanner.db" ')

    assert get_database_url() == "sqlite:///tmp/custom-scanner.db"


def test_get_database_url_defaults_to_local_sqlite_file(monkeypatch):
    """A fresh local checkout should use data/scanner.db automatically."""
    from backend.storage.database import get_database_url

    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert get_database_url() == f"sqlite:///{(DATA_DIR / 'scanner.db').as_posix()}"


def test_make_engine_enables_sqlite_foreign_key_cascade(tmp_path: Path):
    """Deleting a parent row through raw SQL should delete child rows too."""
    from backend.storage.database import _make_engine

    # Use a real temporary file instead of in-memory SQLite because this is the
    # same shape as the default local development database.
    engine = _make_engine(f"sqlite:///{(tmp_path / 'scan-history.db').as_posix()}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)

    with Session() as session:
        run = _make_run()
        run.results.append(ScanResult(symbol="RELIANCE"))
        session.add(run)
        session.commit()
        run_id = run.id

    with engine.begin() as connection:
        # Bypass ORM relationship cleanup on purpose. This proves SQLite's own
        # ON DELETE CASCADE is active because our engine installed the PRAGMA.
        connection.execute(text("DELETE FROM scan_runs WHERE id = :run_id"), {"run_id": run_id})

    with Session() as session:
        assert session.scalars(select(ScanResult)).all() == []

    engine.dispose()


def test_session_scope_commits_and_rolls_back(monkeypatch, tmp_path: Path):
    """The context manager should commit success and roll back failure."""
    from backend.storage import database

    engine = database._make_engine(f"sqlite:///{(tmp_path / 'session-scope.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    # Point the module-level context manager at our temporary session factory so
    # the test never touches the user's real data/scanner.db file.
    monkeypatch.setattr(database, "SessionLocal", TestSessionLocal)

    with database.session_scope() as session:
        session.add(_make_run())

    with TestSessionLocal() as session:
        assert session.scalar(select(text("count(*)")).select_from(ScanRun)) == 1

    with pytest.raises(RuntimeError, match="boom"), database.session_scope() as session:
        session.add(_make_run())
        raise RuntimeError("boom")

    with TestSessionLocal() as session:
        assert session.scalar(select(text("count(*)")).select_from(ScanRun)) == 1

    engine.dispose()


def test_make_engine_applies_sqlite_concurrency_pragmas(tmp_path: Path):
    """Every SQLite connection should enforce keys and survive write contention.

    ``foreign_keys`` keeps the ON DELETE CASCADE working; ``busy_timeout`` makes a
    blocked writer wait instead of immediately raising "database is locked"; and
    WAL lets the history page read while a scan writes. These matter once SCAN-003
    runs under Streamlit's worker threads.
    """
    from backend.storage.database import _make_engine

    engine = _make_engine(f"sqlite:///{(tmp_path / 'pragmas.db').as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar() == 5000
            assert connection.exec_driver_sql("PRAGMA journal_mode").scalar().lower() == "wal"
    finally:
        engine.dispose()


def test_make_engine_enables_pool_pre_ping_only_for_server_databases(tmp_path: Path):
    """DEPLOY-004: dead pooled Postgres connections must be replaced on checkout.

    Managed Postgres proxies and PgBouncer idle-kill TCP connections, so the
    first scan after a quiet stretch used to inherit a dead pooled connection.
    ``pool_pre_ping`` probes on checkout and reconnects transparently. SQLite
    is a local file with no server to lose, so its engine must stay exactly as
    before (no pre-ping). Building the Postgres engine is safe without a
    server: SQLAlchemy connects lazily, and the pinned psycopg driver only has
    to import.
    """
    from backend.storage.database import _make_engine

    sqlite_engine = _make_engine(f"sqlite:///{(tmp_path / 'pre-ping.db').as_posix()}")
    try:
        assert sqlite_engine.pool._pre_ping is False
    finally:
        sqlite_engine.dispose()

    postgres_engine = _make_engine("postgresql+psycopg://scanner:secret@localhost:5432/scanner")
    try:
        assert postgres_engine.pool._pre_ping is True
    finally:
        postgres_engine.dispose()


def test_missing_expected_tables_empty_when_schema_complete(tmp_path: Path):
    """A fully built database reports no missing tables (the healthy path)."""
    from backend.storage.database import _make_engine, _missing_expected_tables

    engine = _make_engine(f"sqlite:///{(tmp_path / 'complete.db').as_posix()}")
    try:
        Base.metadata.create_all(engine)
        assert _missing_expected_tables(engine) == set()
    finally:
        engine.dispose()


def test_missing_expected_tables_flags_drift(tmp_path: Path):
    """A database missing an ORM table is detected as drift.

    This mirrors the real "stamped at HEAD but ``audit_logs`` never created"
    failure: every ORM table exists except one, so the helper must name exactly
    that gap so ``ensure_database_schema`` can warn instead of letting a later
    INSERT fail with "no such table".
    """
    from backend.storage.database import _make_engine, _missing_expected_tables

    engine = _make_engine(f"sqlite:///{(tmp_path / 'drift.db').as_posix()}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE audit_logs"))
        assert _missing_expected_tables(engine) == {"audit_logs"}
    finally:
        engine.dispose()
