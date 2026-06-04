"""Tests for the SCAN-002 database connection helpers."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest
from sqlalchemy import select, text
from sqlalchemy.orm import sessionmaker

from backend.config import DATA_DIR
from backend.storage.models import Base, ScanResult, ScanRun


def _make_run() -> ScanRun:
    return ScanRun(
        started_at=dt.datetime(2026, 6, 4, 9, 30, tzinfo=dt.UTC),
        screener_key="envelope",
        universe_key="nifty_500",
    )


def test_get_database_url_prefers_clean_env_value(monkeypatch):
    from backend.storage.database import get_database_url

    monkeypatch.setenv("DATABASE_URL", ' "sqlite:///tmp/custom-scanner.db" ')

    assert get_database_url() == "sqlite:///tmp/custom-scanner.db"


def test_get_database_url_defaults_to_local_sqlite_file(monkeypatch):
    from backend.storage.database import get_database_url

    monkeypatch.delenv("DATABASE_URL", raising=False)

    assert get_database_url() == f"sqlite:///{(DATA_DIR / 'scanner.db').as_posix()}"


def test_make_engine_enables_sqlite_foreign_key_cascade(tmp_path: Path):
    from backend.storage.database import _make_engine

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
        connection.execute(text("DELETE FROM scan_runs WHERE id = :run_id"), {"run_id": run_id})

    with Session() as session:
        assert session.scalars(select(ScanResult)).all() == []

    engine.dispose()


def test_session_scope_commits_and_rolls_back(monkeypatch, tmp_path: Path):
    from backend.storage import database

    engine = database._make_engine(f"sqlite:///{(tmp_path / 'session-scope.db').as_posix()}")
    Base.metadata.create_all(engine)
    TestSessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    monkeypatch.setattr(database, "SessionLocal", TestSessionLocal)

    with database.session_scope() as session:
        session.add(_make_run())

    with TestSessionLocal() as session:
        assert session.scalar(select(text("count(*)")).select_from(ScanRun)) == 1

    with pytest.raises(RuntimeError, match="boom"):
        with database.session_scope() as session:
            session.add(_make_run())
            raise RuntimeError("boom")

    with TestSessionLocal() as session:
        assert session.scalar(select(text("count(*)")).select_from(ScanRun)) == 1

    engine.dispose()
