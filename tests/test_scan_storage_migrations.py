"""Tests for the SCAN-002 Alembic migration setup.

These tests use a temporary SQLite file instead of the real ``data/scanner.db``.
That proves migrations are safe to run in CI and on a developer laptop without
depending on any local scan history.
"""

from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect


def test_alembic_upgrade_and_downgrade_use_temp_sqlite(monkeypatch, tmp_path: Path):
    """Upgrade creates the expected schema; downgrade removes it again."""
    db_path = tmp_path / "scan-history.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    # Regression guard: Alembic's logging setup used to disable already-created
    # app loggers. That broke later tests that capture warnings from scanner
    # code, so this assertion protects the whole suite from order dependence.
    app_logger = logging.getLogger("backend.scanner_base")
    app_logger.disabled = False

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    assert app_logger.disabled is False

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    inspector = inspect(engine)

    # Table and index checks keep the hand-written migration aligned with
    # backend/storage/models.py and the SCAN-001 design doc.
    assert set(inspector.get_table_names()) == {"alembic_version", "scan_runs", "scan_results"}
    assert {index["name"] for index in inspector.get_indexes("scan_runs")} >= {
        "ix_scan_runs_screener_key",
        "ix_scan_runs_status",
        "ix_scan_runs_universe_key",
    }
    assert {index["name"] for index in inspector.get_indexes("scan_results")} >= {
        "ix_scan_results_run_id",
        "ix_scan_results_symbol",
    }
    foreign_keys = inspector.get_foreign_keys("scan_results")
    # The cascade option is important: deleting a run must not leave orphaned
    # result rows behind.
    assert foreign_keys[0]["referred_table"] == "scan_runs"
    assert foreign_keys[0]["options"] == {"ondelete": "CASCADE"}
    engine.dispose()

    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()
