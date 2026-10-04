"""Provision verification databases without touching application schemas."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError
from sqlalchemy.pool import NullPool


@contextmanager
def disposable_postgres_database(admin_url: str) -> Iterator[str]:
    """Create and finally drop a randomly named PostgreSQL database.

    Args:
        admin_url: Explicit test-server URL whose role can CREATE DATABASE.
            Its configured database is only a maintenance connection target.

    Yields:
        A URL for the newly created private database, using the pinned driver.

    Raises:
        ValueError: The URL cannot be parsed or does not name PostgreSQL.
        RuntimeError: Engine setup, provisioning or cleanup fails; configuration
            and credentials are suppressed from the public exception message.

    Beginner note:
        A shared ``public`` schema is never disposable. The only DROP target is
        the locally generated UUID name whose CREATE succeeded in this context.
        NullPool and AUTOCOMMIT are necessary: PostgreSQL CREATE/DROP DATABASE
        cannot run in a transaction, and pooled scratch connections block DROP.
        FORCE terminates only connections to our own scratch database on supported PostgreSQL.
    """
    # URL parsing and engine construction can fail before engine.connect. Keep
    # those phases inside the same safe-error policy: parser/plugin errors may
    # otherwise repeat opaque values from the supplied connection setting.
    try:
        url = make_url(admin_url)
    except (ArgumentError, TypeError, ValueError):
        raise ValueError("Verification provisioning requires a valid PostgreSQL URL") from None
    if url.get_backend_name() != "postgresql":
        raise ValueError("Verification provisioning requires a PostgreSQL URL")
    url = url.set(drivername="postgresql+psycopg")
    # No caller-controlled identifiers enter these DDL strings. Even a hostile
    # database component in admin_url cannot change the generated DROP target.
    database_name = "scanner_verify_" + uuid4().hex
    try:
        engine = create_engine(
            url, isolation_level="AUTOCOMMIT", poolclass=NullPool,
            connect_args={"connect_timeout": 5},
        )
    except Exception:
        raise RuntimeError("Cannot configure the disposable PostgreSQL verification connection") from None
    created = False
    try:
        try:
            with engine.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{database_name}" TEMPLATE template0')
                # CREATE is already committed. Track ownership before connection
                # cleanup can raise so finally still drops our private database.
                created = True
        except Exception:
            raise RuntimeError(
                "Cannot provision disposable PostgreSQL database; check the explicit test URL and CREATEDB privilege"
            ) from None
        yield url.set(database=database_name).render_as_string(hide_password=False)
    finally:
        try:
            if created:
                try:
                    with engine.connect() as connection:
                        connection.exec_driver_sql(f'DROP DATABASE "{database_name}" WITH (FORCE)')
                except Exception:
                    raise RuntimeError(f"Cannot clean up disposable PostgreSQL database {database_name}") from None
        finally:
            engine.dispose()
