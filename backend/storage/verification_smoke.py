"""Provider-free migration/repository verification inside the application image.

Beginner note:
    This is an explicit verification command, not an application startup hook.
    It requires a separate PostgreSQL provisioning URL and owns disposable
    databases and a private DATA_DIR; it never scans or sends notifications.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess  # nosec B404
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
from tempfile import TemporaryDirectory

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage.schema_verification import reflect_schema
from backend.storage.verification_database import disposable_postgres_database


@contextmanager
def _migration_url(url: str) -> Iterator[None]:
    """Temporarily direct Alembic's environment reader at a private database.

    Beginner note:
        Alembic reads DATABASE_URL at invocation time. Restore the original
        value even on failure so an integration test cannot redirect later tests.
        This helper is synchronous; callers must not run it concurrently.
    """
    original = os.environ.get("DATABASE_URL")
    os.environ["DATABASE_URL"] = url
    try:
        yield
    finally:
        if original is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original


def migration_config() -> Config:
    """Return a config anchored to the installed application migration directory.

    Beginner note:
        A programmatic Config preserves the application's logging/redaction
        setup. Resolving from __file__ also works from a non-repository cwd.
    """
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).resolve().parents[2] / "migrations"))
    return config


def _seed_history(engine: Engine) -> tuple[int, int]:
    """Insert initial-revision rows using only columns that existed then.

    Beginner note:
        Current ORM INSERTs cannot seed an old schema: they mention columns
        which do not exist yet. Bound SQL uses the real historical shape and
        lets both dialects generate IDs so sequence state remains realistic.
    """
    with engine.begin() as connection:
        run_id: int = connection.execute(text(
            "INSERT INTO scan_runs (started_at, finished_at, status, screener_key, universe_key) "
            "VALUES (:started, :started, 'success', 'verification_legacy', 'nifty_100') RETURNING id"
        ), {"started": dt.datetime(2026, 6, 4, tzinfo=dt.UTC).isoformat()}).scalar_one()
        result_id: int = connection.execute(text(
            "INSERT INTO scan_results (run_id, symbol, close_price, raw_result_json, created_at) "
            "VALUES (:run_id, 'LEGACY', 123.4500, :payload, :created) RETURNING id"
        ), {"run_id": run_id, "payload": '{"symbol":"LEGACY","history_marker":"retain"}',
            "created": dt.datetime(2026, 6, 4, tzinfo=dt.UTC).isoformat()}).scalar_one()
    return int(run_id), int(result_id)


def _verify_repository(engine: Engine, legacy_run_id: int, legacy_result_id: int) -> None:
    """Prove durable repository writes, rollback, uniqueness and database cascade.

    Beginner note:
        Separate transactions ensure reads observe committed storage rather than
        a Session identity map. A Core parent DELETE bypasses ORM cascades, so
        missing database ON DELETE CASCADE really fails this check. All queries
        remain inside storage; the smoke command owns the transaction lifetime.
    """
    from backend.storage import repository
    from backend.storage.models import ScanResult, ScanRun, ScanStatus, UserRole

    with Session(engine) as session:
        legacy = repository.get_scan_run(session, legacy_run_id)
        if legacy is None or legacy.status != ScanStatus.SUCCESS:
            raise AssertionError("Historical scan header was lost or changed")
        rows = repository.get_scan_results(session, legacy_run_id)
        if len(rows) != 1 or rows[0].id != legacy_result_id:
            raise AssertionError("Historical result identity was lost")
        payload = rows[0].raw_result_json
        if rows[0].close_price != Decimal("123.4500") or not payload or payload.get("history_marker") != "retain":
            raise AssertionError("Historical price or JSON evidence was changed")
    with Session(engine) as session, session.begin():
        run = repository.create_scan_run(session, screener_key="verification_current", universe_key="nifty_100")
        run_id = run.id
        repository.save_scan_results(session, run, [{"symbol": "SMOKE", "close": Decimal("456.7800")}])
        repository.finish_scan_run(session, run, status=ScanStatus.SUCCESS)
        repository.set_user_role(session, email="smoke@example.invalid", role="admin", assigned_by=None)
    with Session(engine) as session:
        stored = repository.get_scan_run(session, run_id)
        if stored is None or stored.status != ScanStatus.SUCCESS:
            raise AssertionError("Repository write/read failed")
        if repository.get_scan_results(session, run_id)[0].close_price != Decimal("456.7800"):
            raise AssertionError("Repository price precision changed")
    # Direct duplicate INSERT proves actual database uniqueness instead of
    # relying on the repository's friendly upsert to avoid a duplicate itself.
    with Session(engine) as session:
        session.add(UserRole(email="smoke@example.invalid", role="admin", assigned_by=None))
        try:
            session.commit()
        except IntegrityError:
            session.rollback()
        else:
            raise AssertionError("Database accepted a duplicate user-role identity")
    # A deliberate exception proves caller-owned rollback, with readback from
    # a fresh transaction; failed writes must not create phantom scan history.
    with Session(engine) as session:
        rolled_back = repository.create_scan_run(
            session, screener_key="verification_rollback", universe_key="nifty_100"
        )
        rolled_back_id = rolled_back.id
        session.rollback()
    with Session(engine) as session, session.begin():
        if repository.get_scan_run(session, rolled_back_id) is not None:
            raise AssertionError("Rolled-back scan survived")
        session.execute(delete(ScanRun).where(ScanRun.id == run_id))
        if session.scalar(select(func.count()).select_from(ScanResult).where(ScanResult.run_id == run_id)) != 0:
            raise AssertionError("Database did not cascade result deletion")
        if repository.get_scan_run(session, legacy_run_id) is None:
            raise AssertionError("Deleting the scratch run deleted unrelated history")
        if repository.delete_user_role(session, "smoke@example.invalid") != "admin":
            raise AssertionError("Repository delete failed")


def verify_database_pair(migrated_url: str, metadata_url: str) -> dict[str, bool]:
    """Upgrade seeded history and compare with a separate ORM-created database.

    Args:
        migrated_url: Empty private database to build through the migration chain.
        metadata_url: Different empty private database to build from ORM metadata.

    Returns:
        Three proof flags only after every assertion succeeds.

    Raises:
        AssertionError: Database identity, history, repository, or schema differs.

    Beginner note:
        Refuse nonempty databases BEFORE any DDL. Provisioning and eventual
        deletion belong to the caller, and this function never drops a schema.
        Comparison is within one dialect, not a lossy SQLite/PostgreSQL mapping.
    """
    from backend.storage.database import _make_engine
    from backend.storage.models import Base

    if migrated_url == metadata_url:
        raise AssertionError("Verification requires two different private databases")
    migrated, metadata = _make_engine(migrated_url), _make_engine(metadata_url)
    try:
        if migrated.dialect.name != metadata.dialect.name:
            raise AssertionError("Verification databases must use the same dialect")
        if inspect(migrated).get_table_names() or inspect(metadata).get_table_names():
            raise AssertionError("Verification databases must be empty")
        config = migration_config()
        if len(ScriptDirectory.from_config(config).get_heads()) != 1:
            raise AssertionError("The migration chain must have exactly one head")
        with _migration_url(migrated_url):
            command.upgrade(config, "20260604scan002")
            legacy_run_id, legacy_result_id = _seed_history(migrated)
            command.upgrade(config, "head")
        _verify_repository(migrated, legacy_run_id, legacy_result_id)
        Base.metadata.create_all(metadata)
        actual, expected = reflect_schema(migrated), reflect_schema(metadata)
        differences = [
            f"{table}.{part}"
            for table in actual.keys() | expected.keys()
            for part in actual.get(table, {}).keys() | expected.get(table, {}).keys()
            if actual.get(table, {}).get(part) != expected.get(table, {}).get(part)
        ]
        if differences:
            raise AssertionError("Migration/ORM schema drift: " + ", ".join(sorted(differences)))
        return {"history_preserved": True, "repository_round_trip": True, "schema_converged": True}
    finally:
        migrated.dispose()
        metadata.dispose()


def verify_relocated_universe_sources() -> None:
    """Read packaged source universes after moving mutable data to an empty dir.

    Beginner note:
        PR116 separates curated source CSVs from generated DATA_DIR files. This
        check must read the package's sources even when the private runtime
        directory contains no universes. It performs no downloads or mappings.
    """
    from backend.config import DATA_DIR
    from backend.universe_builder import HEMANT_SOURCE_FILES, load_symbol_list_csv

    for source in HEMANT_SOURCE_FILES.values():
        if source.resolve().is_relative_to(DATA_DIR.resolve()):
            raise AssertionError("Curated universe source incorrectly follows relocated DATA_DIR")
        if not load_symbol_list_csv(source):
            raise AssertionError("Packaged curated universe source is empty")


def run_image_verification() -> None:
    """Verify image sources/storage after the launcher sets private environment.

    Beginner note:
        Run in the launcher's fresh process so import-time path constants read
        the relocated DATA_DIR. Suppress driver errors to avoid echoing a DSN.
        Assertion messages contain only our own fixed verification diagnostics.
    """
    try:
        verify_relocated_universe_sources()
        admin_url = os.environ["SCANNER_TEST_POSTGRES_ADMIN_URL"]
        with (
            disposable_postgres_database(admin_url) as migrated,
            disposable_postgres_database(admin_url) as metadata,
        ):
            result = verify_database_pair(migrated, metadata)
        print(json.dumps({**result, "relocated_universe_sources": True}, sort_keys=True))
    except AssertionError as exc:
        raise SystemExit(str(exc)) from None
    except Exception as exc:
        raise SystemExit(f"Application image verification failed ({type(exc).__name__})") from None


def main() -> int:
    """Run image verification using explicitly authorized scratch resources.

    Beginner note:
        The package creates its normal lazy engine at import time but does not
        connect to it. The child replaces DATABASE_URL with harmless SQLite
        before config import; PostgreSQL writes use only generated databases.
        Failure messages omit connection URLs and raw database exceptions.
    """
    admin_url = os.environ.get("SCANNER_TEST_POSTGRES_ADMIN_URL", "").strip()
    if not admin_url:
        raise SystemExit("SCANNER_TEST_POSTGRES_ADMIN_URL is required")
    # backend.storage imports config before module execution. A fresh child
    # receives DATA_DIR before that import, matching real image startup.
    with TemporaryDirectory(prefix="scanner-image-verification-") as data_dir:
        env = os.environ.copy()
        env.update(DATABASE_URL="sqlite://", DATA_DIR=data_dir, ALERT_ENABLED="false")
        # Fixed argv uses this interpreter without a shell; URLs stay in env.
        completed = subprocess.run(  # nosec B603
            [sys.executable, "-c", "from backend.storage.verification_smoke import run_image_verification; "
             "run_image_verification()"],
            env=env, check=False, timeout=180,
        )
        if completed.returncode:
            raise SystemExit(completed.returncode)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
