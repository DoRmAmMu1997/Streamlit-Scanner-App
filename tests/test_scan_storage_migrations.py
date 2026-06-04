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

from backend.storage.models import Base


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


def test_migration_matches_orm_metadata(monkeypatch, tmp_path: Path):
    """The hand-written migration must build the same schema as the ORM models.

    Schema A comes from ``alembic upgrade head``; schema B comes from
    ``Base.metadata.create_all``. Comparing two SQLAlchemy-built SQLite schemas
    (instead of a hardcoded expectation) keeps the guard robust against reflection
    quirks while still catching any column, index, or foreign-key drift between the
    migration and ``backend/storage/models.py``.
    """
    migrated_db = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{migrated_db.as_posix()}")
    command.upgrade(Config("alembic.ini"), "head")
    migrated_schema = _reflect_schema(
        create_engine(f"sqlite:///{migrated_db.as_posix()}", future=True)
    )

    orm_engine = create_engine(f"sqlite:///{(tmp_path / 'orm.db').as_posix()}", future=True)
    Base.metadata.create_all(orm_engine)
    orm_schema = _reflect_schema(orm_engine)

    assert migrated_schema == orm_schema


def _reflect_schema(engine) -> dict[str, dict[str, object]]:
    """Return a comparable {table: columns/indexes/foreign_keys} structure.

    Alembic's bookkeeping ``alembic_version`` table is skipped so a migrated
    database and a ``create_all`` database describe the same application schema.
    The engine is disposed before returning.
    """
    inspector = inspect(engine)
    schema: dict[str, dict[str, object]] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        columns = {
            column["name"]: (str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        }
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        foreign_keys = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(table)
        }
        schema[table] = {"columns": columns, "indexes": indexes, "foreign_keys": foreign_keys}
    engine.dispose()
    return schema
