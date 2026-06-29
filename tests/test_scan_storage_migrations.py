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

from backend.storage import database
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
    # backend/storage/models.py and the SCAN-001 / VALID-001 design docs.
    assert set(inspector.get_table_names()) == {
        "ai_evaluations",
        "alembic_version",
        "app_config",
        "audit_logs",
        "scan_runs",
        "scan_results",
        "signal_forward_returns",
        "user_roles",
    }
    assert {index["name"] for index in inspector.get_indexes("audit_logs")} >= {
        "ix_audit_logs_created_at",
        "ix_audit_logs_event",
        "ix_audit_logs_user_email",
    }
    assert {index["name"] for index in inspector.get_indexes("scan_runs")} >= {
        "ix_scan_runs_screener_key",
        "ix_scan_runs_status",
        "ix_scan_runs_universe_key",
    }
    assert "data_quality_json" in {
        column["name"] for column in inspector.get_columns("scan_runs")
    }
    assert {index["name"] for index in inspector.get_indexes("scan_results")} >= {
        "ix_scan_results_run_id",
        "ix_scan_results_symbol",
        "ix_scan_results_symbol_signal_date",  # VALID-001 forward-return lookup
    }
    assert {index["name"] for index in inspector.get_indexes("ai_evaluations")} >= {
        "ix_ai_evaluations_outcome",
        "ix_ai_evaluations_run_id",
        "ix_ai_evaluations_symbol",
    }
    foreign_keys = inspector.get_foreign_keys("scan_results")
    # The cascade option is important: deleting a run must not leave orphaned
    # result rows behind.
    assert foreign_keys[0]["referred_table"] == "scan_runs"
    assert foreign_keys[0]["options"] == {"ondelete": "CASCADE"}
    ai_foreign_keys = inspector.get_foreign_keys("ai_evaluations")
    assert ai_foreign_keys[0]["referred_table"] == "scan_runs"
    assert ai_foreign_keys[0]["options"] == {"ondelete": "CASCADE"}

    # VALID-001: forward-return rows hang off scan_results with the same cascade,
    # and the calculator's "pending rows" lookup is indexed.
    assert {
        index["name"] for index in inspector.get_indexes("signal_forward_returns")
    } >= {"ix_signal_forward_returns_status"}
    forward_keys = inspector.get_foreign_keys("signal_forward_returns")
    assert forward_keys[0]["referred_table"] == "scan_results"
    assert forward_keys[0]["options"] == {"ondelete": "CASCADE"}
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


def test_ensure_database_schema_creates_tables_and_short_circuits(monkeypatch, tmp_path: Path):
    """The runtime bootstrap applies migrations once, then skips on later calls.

    This guards the fix for the "no such table: scan_runs" startup bug: the app
    and the daily CLI call ``ensure_database_schema()`` instead of relying on a
    manual ``alembic upgrade head`` that fresh checkouts never ran.
    """
    db_path = tmp_path / "bootstrap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    assert database.ensure_database_schema() is True

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    assert set(inspect(engine).get_table_names()) == {
        "ai_evaluations",
        "alembic_version",
        "app_config",
        "audit_logs",
        "scan_runs",
        "scan_results",
        "signal_forward_returns",
        "user_roles",
    }
    engine.dispose()

    # The second call must short-circuit on the per-process flag. Pointing the
    # environment at an unusable URL proves no new migration run is attempted:
    # if one were, it would fail loudly instead of returning True.
    monkeypatch.setenv("DATABASE_URL", "driver://not-a-real-database")
    assert database.ensure_database_schema() is True


def test_ensure_database_schema_preserves_logging_configuration(monkeypatch, tmp_path: Path):
    """The bootstrap must never re-run logging setup from alembic.ini.

    ``migrations/env.py`` calls ``fileConfig`` when the alembic Config carries an
    ini file name. That would replace the root logger's handlers — silently
    discarding the SEC-002 secret-redaction filter installed by the app. The
    helper builds its Config programmatically, so existing handlers and filters
    must survive untouched.
    """
    db_path = tmp_path / "bootstrap-logging.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    root_logger = logging.getLogger()
    marker_handler = logging.NullHandler()
    marker_filter = logging.Filter("tests.schema.bootstrap.marker")
    root_logger.addHandler(marker_handler)
    root_logger.addFilter(marker_filter)
    handlers_before = list(root_logger.handlers)
    try:
        assert database.ensure_database_schema() is True
        assert root_logger.handlers == handlers_before
        assert marker_filter in root_logger.filters
    finally:
        root_logger.removeHandler(marker_handler)
        root_logger.removeFilter(marker_filter)


def test_ensure_database_schema_returns_false_when_database_is_unusable(
    monkeypatch, tmp_path: Path
):
    """Bootstrap failures degrade gracefully and stay retryable.

    Scan persistence is best-effort in the UI ("continuing without history"), so
    the bootstrap must warn-and-return rather than crash the app. The flag stays
    unset on failure so a later call can retry once the environment is fixed.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{(blocker / 'impossible.db').as_posix()}",
    )
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    assert database.ensure_database_schema() is False
    assert database._schema_ensured is False


def test_ensure_database_schema_detects_schema_drift(monkeypatch, tmp_path: Path, caplog):
    """A database stamped at HEAD but missing a table is reported as drift.

    This reproduces the real failure where ``alembic_version`` says HEAD yet
    ``audit_logs`` was never created (migration history stitched across
    branches). ``upgrade head`` is then a no-op, so ``ensure_database_schema``
    must verify the tables really exist, return ``False``, and log actionable
    guidance instead of leaving a later INSERT to fail with "no such table".
    """
    db_path = tmp_path / "drift.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)
    monkeypatch.setattr(database, "_schema_drift_logged", False, raising=False)

    # Build the correct schema first, then simulate drift by dropping one table
    # while the alembic_version row keeps claiming HEAD.
    assert database.ensure_database_schema() is True
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE audit_logs")
    finally:
        engine.dispose()

    with caplog.at_level(logging.ERROR, logger="backend.storage.database"):
        result = database.ensure_database_schema()

    assert result is False
    # Stays unset so a later call retries once the database is rebuilt.
    assert database._schema_ensured is False
    assert "schema drift" in caplog.text.lower()
    assert "audit_logs" in caplog.text


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
