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

import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from sqlalchemy import create_engine, event, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from backend.config import get_settings
from backend.storage.models import Base

logger = logging.getLogger(__name__)


def get_database_url() -> str:
    """Return the database URL that SCAN-002 should use.

    ``DATABASE_URL`` wins when it is set because deployment can point the app at
    Postgres without changing code. Local development falls back to a SQLite file
    under ``data/``. That file is ignored by git, so real scan history and local
    experiments never get committed by accident.

    Beginner note:
    SQLAlchemy uses URLs for every database backend. SQLite URLs point at a local
    file, while Postgres URLs normally include a host, database name, username,
    and password. This helper hides that difference from the repository layer.
    """
    settings = get_settings()
    if not settings.database_url_from_env:
        # ``DATA_DIR`` is the same runtime data folder used by the candle and
        # fundamentals caches. Creating it here makes `alembic upgrade head` work
        # in a fresh checkout before any scanner has run.
        #
        # Production startup validation requires DATABASE_URL and DATA_DIR to be
        # explicit, so this fallback directory creation is for local development
        # and migration commands, not a hidden production storage choice.
        settings.data_dir.mkdir(parents=True, exist_ok=True)
    return settings.database_url


def _make_engine(url: str | None = None) -> Engine:
    """Create an engine with the SQLite settings this Streamlit app needs.

    The helper accepts an optional URL so tests can point it at a temporary
    SQLite database without touching the real ``data/scanner.db`` file. The
    module-level ``engine`` below uses the default environment-backed URL.
    """
    database_url = url or get_database_url()
    connect_args = {}
    engine_kwargs: dict[str, Any] = {}
    if database_url.startswith("sqlite"):
        # Streamlit can re-run the app and use worker threads while the same
        # module-level engine stays imported. SQLite defaults to one-thread-only
        # connections, so we relax that guard and keep sessions short-lived.
        connect_args["check_same_thread"] = False
    else:
        # DEPLOY-004: server databases sit behind infrastructure that silently
        # drops idle TCP connections (managed Postgres proxies, PgBouncer,
        # cloud NAT). A pooled connection can therefore be dead by the next
        # morning's first scan, which would fail with "server closed the
        # connection unexpectedly". ``pool_pre_ping`` issues a lightweight
        # liveness probe on checkout and transparently replaces dead
        # connections. SQLite is a local file with no server to lose, so its
        # engine arguments stay exactly as they were.
        engine_kwargs["pool_pre_ping"] = True

    created_engine = create_engine(
        database_url,
        connect_args=connect_args,
        future=True,
        **engine_kwargs,
    )

    if database_url.startswith("sqlite"):

        @event.listens_for(created_engine, "connect")
        def _apply_sqlite_pragmas(dbapi_connection, _connection_record):
            """Apply SQLite-only safety switches to each low-level connection.

            Beginner note:
            SQLAlchemy's engine may open multiple DB-API connections over time.
            SQLite PRAGMAs live on the connection, not just the database file, so
            this hook repeats the settings every time a connection is created.
            """
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


# ``ensure_database_schema`` runs at most one real migration pass per process.
# Streamlit re-runs ``main()`` on every widget interaction, and first-boot
# sessions can arrive on worker threads, so the flag is guarded by a lock.
_schema_ensured = False
_schema_lock = threading.Lock()
# Guard so a drifted database logs its loud guidance once per process instead of
# on every Streamlit rerun. See ``ensure_database_schema`` for why drift happens.
_schema_drift_logged = False


def _missing_expected_tables(target_engine: Engine) -> set[str]:
    """Return ORM-declared tables that are absent from the live database.

    A database can report its Alembic version as ``head`` yet be missing tables
    when its migration history was stitched together across branches/worktrees
    (a real hazard of this repo's multi-agent workflow). When that happens
    ``alembic upgrade head`` is a no-op and the gap stays invisible until a later
    INSERT fails with a cryptic "no such table". Comparing the ORM's declared
    tables against the live database surfaces the drift up front instead.
    """
    existing = set(inspect(target_engine).get_table_names())
    return set(Base.metadata.tables) - existing


def ensure_database_schema() -> bool:
    """Apply Alembic migrations to the configured database, once per process.

    A fresh checkout has no ``scan_runs``/``scan_results`` tables until
    ``alembic upgrade head`` runs, so every scan used to fail its history write
    with "no such table: scan_runs". The app and the daily CLI call this on
    startup instead of relying on that manual step. ``upgrade`` is idempotent:
    an already-migrated database is a fast no-op.

    The Alembic ``Config`` is built programmatically — without ``alembic.ini`` —
    on purpose. ``migrations/env.py`` only calls ``logging.config.fileConfig``
    when the config carries an ini file name, and that call would replace the
    root logger's handlers, discarding the SEC-002 secret-redaction filter the
    app installs. The database URL needs no plumbing either: env.py reads it
    from ``get_database_url()``, the same source the engine above uses.

    Returns ``True`` when the schema is ready. Failures are logged (the root
    redaction filter masks any credentials in the URL) and return ``False``;
    scan persistence stays best-effort, matching "continuing without history".
    A database stamped at ``head`` but missing tables (schema drift) is also
    reported as not-ready so the gap is loud instead of a later cryptic INSERT
    error.
    """
    global _schema_ensured, _schema_drift_logged
    with _schema_lock:
        if _schema_ensured:
            return True
        try:
            # Local imports keep alembic out of the app's hot import path; this
            # module is imported by every scan-related page.
            from alembic import command
            from alembic.config import Config

            from backend.config.settings import PROJECT_ROOT

            config = Config()
            config.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
            command.upgrade(config, "head")
            # ``upgrade head`` is a no-op when ``alembic_version`` already says
            # head, so it cannot repair a database whose recorded version
            # disagrees with the tables actually present. Verify the ORM's tables
            # really exist against the database Alembic just migrated
            # (``get_database_url()`` at call time, which the module engine also
            # targets at runtime) via a short-lived engine. Kept inside this
            # ``try`` so a verify hiccup degrades to best-effort like a migration
            # failure rather than crashing startup.
            verify_engine = _make_engine()
            try:
                missing = _missing_expected_tables(verify_engine)
            finally:
                verify_engine.dispose()
        except Exception:  # noqa: BLE001 - startup must not crash on a DB issue.
            logger.warning(
                "Could not apply or verify scan-history migrations; scans will "
                "continue without persisted history.",
                exc_info=True,
            )
            return False
        if missing:
            if not _schema_drift_logged:
                logger.error(
                    "Database schema drift: Alembic reports HEAD but these "
                    "tables are missing: %s. The database is inconsistent "
                    "(its version was likely stamped across branches/worktrees). "
                    "Rebuild it -- locally: stop the app, delete data/scanner.db "
                    "(and the -wal/-shm files), and restart so migrations "
                    "recreate it (local scan history is lost); managed database: "
                    "re-migrate from a known-good baseline. Persistence stays "
                    "best-effort until then.",
                    ", ".join(sorted(missing)),
                )
                _schema_drift_logged = True
            return False
        _schema_ensured = True
        return True


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
