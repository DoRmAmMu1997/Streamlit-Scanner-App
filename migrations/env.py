"""Alembic environment for the scanner persistence schema.

Beginner note:
Alembic runs this file whenever you call commands such as
``python -m alembic upgrade head``. Its job is to connect Alembic to our app's
SQLAlchemy metadata so migrations know which tables they manage.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from backend.storage.database import get_database_url
from backend.storage.models import Base

config = context.config

if config.config_file_name is not None:
    # ``disable_existing_loggers=False`` matters in tests and in Streamlit. The
    # default logging setup can silence already-imported app loggers, which would
    # make later caplog tests and runtime diagnostics mysteriously quiet.
    fileConfig(config.config_file_name, disable_existing_loggers=False)

# Autogenerate compares this metadata against the live database. Even though this
# first migration is hand-reviewed, keeping target_metadata set now makes future
# schema migrations easier and less error-prone.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Run migrations without opening a database connection.

    Offline mode produces SQL text instead of applying it. It is not the normal
    local workflow, but Alembic expects every env.py to support it.
    """
    context.configure(
        url=get_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations with a live SQLAlchemy connection.

    This is the path used by ``alembic upgrade head``. We read DATABASE_URL at
    command time so tests can point migrations at a temporary SQLite file and
    deployments can point them at Postgres.
    """
    config.set_main_option("sqlalchemy.url", get_database_url())
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
