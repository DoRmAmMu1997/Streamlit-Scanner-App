"""Safety and behavior regressions for disposable database verification."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import inspect


def test_repository_smoke_upgrades_seeded_history(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute migrations and the repository scenario on private SQLite files.

    Beginner note:
        Empty-database migration success does not prove historical rows survive
        an upgrade, nor that the current repository can use the migrated schema.
    """
    from backend.storage.verification_smoke import verify_database_pair

    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    result = verify_database_pair(
        f"sqlite:///{tmp_path / 'migrated.db'}", f"sqlite:///{tmp_path / 'metadata.db'}"
    )
    assert result == {"history_preserved": True, "repository_round_trip": True, "schema_converged": True}


def test_disposable_postgres_rejects_other_dialects() -> None:
    """A test-server setting cannot accidentally authorize SQLite/application DDL.

    Beginner note:
        Only a specifically configured PostgreSQL provisioning connection may
        create a scratch database; no application URL is an implicit fallback.
    """
    from backend.storage.verification_database import disposable_postgres_database

    with pytest.raises(ValueError, match="PostgreSQL"), disposable_postgres_database("sqlite://"):
        pytest.fail("Unsupported provisioning URL was accepted")


def test_disposable_postgres_hides_malformed_url_components() -> None:
    """A malformed provisioning URL must not echo its opaque contents.

    Beginner note:
        URL parsing used to happen before the safe-error boundary. A malformed
        port was repeated verbatim by ValueError even though the helper promises
        secret-safe failures. This fails before any connection can be attempted.
    """
    from backend.storage.verification_database import disposable_postgres_database

    marker = "PRIVATE_CONFIGURATION_VALUE"
    with pytest.raises(ValueError) as error, disposable_postgres_database(
        f"postgresql://fixture:fixture@127.0.0.1:{marker}/postgres"
    ):
        pytest.fail("Malformed connection URL was accepted")
    assert marker not in str(error.value)
    assert "valid PostgreSQL URL" in str(error.value)


def test_disposable_postgres_hides_engine_configuration_errors() -> None:
    """Engine construction must use the same safe boundary as connection setup.

    Beginner note:
        An unknown SQLAlchemy plugin fails before a connection exists and echoes
        its URL option. This fake option demonstrates the leak without reaching
        a server; all configuration details must stay out of the public error.
    """
    from backend.storage.verification_database import disposable_postgres_database

    marker = "PRIVATE_UNKNOWN_PLUGIN"
    with pytest.raises(Exception) as error, disposable_postgres_database(
        f"postgresql://fixture:fixture@127.0.0.1/postgres?plugin={marker}"
    ):
        pytest.fail("An unknown engine plugin was accepted")
    assert marker not in str(error.value)
    assert isinstance(error.value, RuntimeError)


def test_disposable_postgres_rejects_database_override_before_engine_setup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reject a query database that would override both generated scratch URLs.

    Beginner note:
        SQLAlchemy lets a ``dbname`` query option override the URL path passed
        to psycopg. Replacing only the path used to leave both supposedly private
        URLs targeting the caller's database. Refusal must happen before CREATE
        or any connection, and diagnostics must not repeat the supplied value.
    """
    from backend.storage import verification_database

    created_engines = []

    def unexpected_engine(*args, **kwargs):
        """Record an unsafe early engine setup without contacting PostgreSQL."""
        created_engines.append(True)
        raise AssertionError("Database override reached engine construction")

    monkeypatch.setattr(verification_database, "create_engine", unexpected_engine)
    marker = "PRIVATE_EXISTING_DATABASE"
    with pytest.raises(ValueError, match="query") as error, verification_database.disposable_postgres_database(
        f"postgresql://fixture:fixture@127.0.0.1/postgres?dbname={marker}"
    ):
        pytest.fail("A database-overriding URL was accepted")
    assert created_engines == []
    assert marker not in str(error.value)


def test_disposable_postgres_driver_targets_generated_database_and_keeps_tls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Check actual driver arguments, not just the displayed scratch URL path.

    Beginner note:
        A safe-looking URL is insufficient when query options can override its
        path. This no-network fixture runs SQLAlchemy's real psycopg argument
        conversion and proves the generated CREATE target is also the effective
        connection database. Legitimate TLS options must survive the boundary.
    """
    from contextlib import contextmanager
    from types import SimpleNamespace

    from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
    from sqlalchemy.engine import make_url

    from backend.storage import verification_database

    commands: list[str] = []

    @contextmanager
    def connection():
        """Record scratch CREATE/DROP statements without opening a connection."""
        yield SimpleNamespace(exec_driver_sql=commands.append)

    monkeypatch.setattr(verification_database, "create_engine", lambda *_args, **_kwargs: SimpleNamespace(
        connect=connection, dispose=lambda: None,
    ))
    with verification_database.disposable_postgres_database(
        "postgresql://fixture:fixture@127.0.0.1/postgres?sslmode=verify-full"
    ) as private_url:
        created_name = commands[0].split('"')[1]
        _args, kwargs = PGDialect_psycopg().create_connect_args(make_url(private_url))
        assert kwargs["dbname"] == created_name
        assert created_name.startswith("scanner_verify_")
        assert kwargs["sslmode"] == "verify-full"
    assert commands[1] == f'DROP DATABASE "{created_name}" WITH (FORCE)'


def test_disposable_postgres_cleans_up_when_create_connection_exit_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """Track a successful CREATE before its connection context can raise.

    Beginner note:
        Creation is committed immediately in AUTOCOMMIT. If connection cleanup
        then fails, the private database still exists and must be dropped. This
        fixture records DDL without reaching a server and proves the DROP target
        is exactly the generated CREATE target, never the configured database.
    """
    from contextlib import contextmanager
    from types import SimpleNamespace

    from backend.storage import verification_database

    commands: list[str] = []
    disposed: list[bool] = []

    @contextmanager
    def connection():
        """Model a close failure only after the first committed DDL statement.

        Beginner note:
            The second context is the finally cleanup path and closes normally;
            changing the flag after the first context exits would miss that path.
        """
        yield SimpleNamespace(exec_driver_sql=commands.append)
        if len(commands) == 1:
            raise RuntimeError("Fixture close failure")

    monkeypatch.setattr(verification_database, "create_engine", lambda *_args, **_kwargs: SimpleNamespace(
        connect=connection, dispose=lambda: disposed.append(True),
    ))
    with pytest.raises(RuntimeError, match="Cannot provision"), verification_database.disposable_postgres_database(
        "postgresql://fixture:fixture@127.0.0.1/existing_database"
    ):
        pytest.fail("A failed provisioning connection was yielded")
    assert len(commands) == 2
    created_name = commands[0].split('"')[1]
    assert created_name.startswith("scanner_verify_")
    assert commands[1] == f'DROP DATABASE "{created_name}" WITH (FORCE)'
    assert disposed == [True]


def test_explicit_unreachable_postgres_is_failure() -> None:
    """A requested but unreachable server must fail rather than silently skip.

    Beginner note:
        This loopback closed-port URL never reaches a provider or application
        database. It distinguishes a missing optional setting from broken CI.
    """
    from backend.storage.verification_database import disposable_postgres_database

    with (
        pytest.raises(RuntimeError, match="provision disposable PostgreSQL"),
        disposable_postgres_database("postgresql+psycopg://test:test@127.0.0.1:1/postgres?connect_timeout=1"),
    ):
        pytest.fail("The intentionally unreachable server accepted a connection")


def test_postgres_migrations_and_repository(postgres_database_pair: tuple[str, str]) -> None:
    """Run the same history and enforcement contract against a real PG server.

    Beginner note:
        The fixture provisions TWO unique databases, never drops public tables
        in a configured database, and fails if an explicitly requested server
        is unavailable. CI supplies that setting, so this test cannot skip there.
    """
    from backend.storage.verification_smoke import verify_database_pair

    assert verify_database_pair(*postgres_database_pair)["schema_converged"] is True


def test_disposable_postgres_cleanup(postgres_admin_url: str) -> None:
    """Exception cleanup removes only the database created by this context.

    Beginner note:
        The nested fixture outlives the scratch database so the readback can
        prove the database was dropped even when the body raises an exception.
    """
    from sqlalchemy import create_engine, text
    from sqlalchemy.engine import make_url

    from backend.storage.verification_database import disposable_postgres_database

    database_name = ""
    with (
        pytest.raises(LookupError, match="intentional fixture failure"),
        disposable_postgres_database(postgres_admin_url) as url,
    ):
        database_name = make_url(url).database or ""
        engine = create_engine(url)
        try:
            assert inspect(engine).get_table_names() == []
        finally:
            engine.dispose()
        raise LookupError("intentional fixture failure")
    engine = create_engine(make_url(postgres_admin_url).set(drivername="postgresql+psycopg"))
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                text("SELECT count(*) FROM pg_database WHERE datname = :name"), {"name": database_name}
            ) == 0
    finally:
        engine.dispose()


def test_smoke_cli_requires_explicit_provisioning_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """The image CLI must not borrow DATABASE_URL if its test setting is absent.

    Beginner note:
        Operators may run a smoke command from an application container. Merely
        having production credentials in that container cannot authorize writes.
    """
    import os

    env = os.environ.copy()
    env.pop("SCANNER_TEST_POSTGRES_ADMIN_URL", None)
    env["DATABASE_URL"] = "sqlite:///DO_NOT_CREATE_SMOKE.db"
    completed = subprocess.run(
        [sys.executable, "-m", "backend.storage.verification_smoke"],
        env=env, capture_output=True, text=True, check=False, timeout=30,
    )
    assert completed.returncode != 0
    assert "SCANNER_TEST_POSTGRES_ADMIN_URL is required" in completed.stderr
    assert not Path("DO_NOT_CREATE_SMOKE.db").exists()


def test_required_postgres_lane_cannot_silently_skip() -> None:
    """Removing the CI URL must be a failing fixture, not a green skipped lane.

    Beginner note:
        This runs only the single PostgreSQL case in a child with no URL, so it
        cannot contact a server. The child must fail before provisioning begins.
    """
    import os

    env = os.environ.copy()
    env.pop("SCANNER_TEST_POSTGRES_ADMIN_URL", None)
    env.update(DATABASE_URL="sqlite://", SCANNER_REQUIRE_POSTGRES_TESTS="1")
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests/test_database_verification_smoke.py",
         "-k", "test_postgres_migrations_and_repository", "-q", "--tb=short"],
        env=env, capture_output=True, text=True, check=False, timeout=60,
    )
    assert result.returncode == 1
    assert "SCANNER_TEST_POSTGRES_ADMIN_URL is required for this test lane" in result.stdout
    assert "1 skipped" not in result.stdout


def test_nonempty_database_is_refused_before_migration(tmp_path: Path) -> None:
    """An accidentally reused target must retain its existing table and rows.

    Beginner note:
        Provisioning contexts normally guarantee emptiness. This independent
        guard ensures direct helper callers cannot rewrite an existing schema.
    """
    from sqlalchemy import create_engine, text

    from backend.storage.verification_smoke import verify_database_pair

    url = f"sqlite:///{tmp_path / 'occupied.db'}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE sentinel (marker TEXT)"))
            connection.execute(text("INSERT INTO sentinel VALUES ('retain')"))
        with pytest.raises(AssertionError, match="must be empty"):
            verify_database_pair(url, f"sqlite:///{tmp_path / 'reference.db'}")
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT marker FROM sentinel")) == "retain"
        assert inspect(engine).get_table_names() == ["sentinel"]
    finally:
        engine.dispose()


@pytest.mark.parametrize("source_in_data_dir", [False, True])
def test_image_sources_are_read_only_and_outside_runtime_data(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_in_data_dir: bool
) -> None:
    """Read a packaged CSV and reject a source accidentally moved into DATA_DIR.

    Beginner note:
        This fixture uses a real CSV parser without fetching an instrument
        master. Distinct source/runtime roots are the important image contract;
        the current product source-path mapping is checked in hosted smoke.
    """
    from backend import config, universe_builder
    from backend.storage.verification_smoke import verify_relocated_universe_sources

    data_dir = tmp_path / "runtime"
    data_dir.mkdir()
    source = (data_dir if source_in_data_dir else tmp_path) / "curated.csv"
    content = "symbol\nEXAMPLE\n"
    source.write_text(content, encoding="utf-8")
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(universe_builder, "HEMANT_SOURCE_FILES", {"example": source})
    if source_in_data_dir:
        with pytest.raises(AssertionError, match="relocated DATA_DIR"):
            verify_relocated_universe_sources()
    else:
        verify_relocated_universe_sources()
    assert source.read_text(encoding="utf-8") == content
