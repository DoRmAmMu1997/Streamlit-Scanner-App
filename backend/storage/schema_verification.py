"""Read-only, dialect-local schema fingerprints for migration verification.

Beginner note:
    Compare migrated and ORM-created databases on the SAME database dialect.
    PostgreSQL rewrites expressions when storing them; reflecting both sides
    lets its own parser normalize them without deleting meaningful SQL text.
    This helper never repairs a schema or disposes the caller's engine.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any, cast

from sqlalchemy import Boolean, inspect
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SAWarning


def _freeze(value: Any) -> Any:
    """Make reflected options comparable without losing expressions or order.

    Args:
        value: SQLAlchemy reflection data, including dialect-specific clauses.

    Returns:
        An immutable value retaining mapping keys, sequence order and SQL text.

    Beginner note:
        SQL literals are case and whitespace sensitive. Do not lowercase text,
        remove spaces or strip casts to make an unexplained discrepancy pass.
    """
    if isinstance(value, Mapping):
        return tuple(sorted((key, _freeze(item)) for key, item in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if value is None or isinstance(value, (str, int, bool)):
        return value
    return str(value)


def _sqlite_index_definitions(engine: Engine, table: str) -> frozenset[tuple[object, ...]]:
    """Retain index key semantics that generic SQLite reflection leaves out.

    Args:
        engine: Caller-owned SQLite verification engine.
        table: Reflected table name, quoted as an identifier before each PRAGMA.

    Returns:
        Index origins, uniqueness, partial-index flags, and ordered key/auxiliary
        columns with their sort direction and collation. Explicit index names
        remain significant; generated constraint-index names are excluded.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: SQLite cannot return the index metadata.

    Beginner note:
        BINARY and NOCASE unique indexes accept different values even when their
        names and columns match. ``index_xinfo`` exposes those comparison rules
        and DESC order, which SQLAlchemy's generic reflection silently omits.
        ``index_list`` also includes indexes backing UNIQUE and PRIMARY KEY
        constraints. Their generated suffixes depend on declaration order, so
        compare their actual definitions instead of incidental autoindex names.
        See https://www.sqlite.org/pragma.html#pragma_index_xinfo.
    """
    quote = engine.dialect.identifier_preparer.quote_identifier
    definitions: set[tuple[object, ...]] = set()
    with engine.connect() as connection:
        indexes = connection.exec_driver_sql(f"PRAGMA index_list({quote(table)})").all()
        for index in indexes:
            columns = connection.exec_driver_sql(f"PRAGMA index_xinfo({quote(index[1])})").all()
            # Preserve index order and key/auxiliary status; table-column numeric
            # offsets are incidental because the logical column name is retained.
            keys = tuple((column[0], *column[2:]) for column in columns)
            name = index[1] if index[3] == "c" else None
            definitions.add((name, index[2], index[3], index[4], keys))
    return frozenset(definitions)


def reflect_schema(engine: Engine) -> dict[str, dict[str, object]]:
    """Fingerprint database-enforced contracts, excluding Alembic bookkeeping.

    Args:
        engine: Caller-owned engine pointing at a verification database.

    Returns:
        Tables with columns, defaults, primary keys, checks, unique constraints,
        complete index definitions and foreign-key targets/actions/options.

    Raises:
        sqlalchemy.exc.SQLAlchemyError: The caller's database cannot be queried
            or its schema cannot be reflected.
        sqlalchemy.exc.SAWarning: Index reflection is incomplete, for example
            when the dialect cannot describe an expression index. Such a schema
            must not be reported as matching from partial evidence.

    Beginner note:
        A matching index name says nothing about its columns or WHERE clause.
        Index/check/unique names are retained. FK and PK names are excluded:
        migrations name some SQLite batch-rebuilt FKs that the ORM leaves
        unnamed; enforcement depends on ordered columns, targets and options.
        Both sides use the same dialect/version, avoiding formatting guesses.
    """
    inspector = inspect(engine)
    schema: dict[str, dict[str, object]] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        columns = {
            column["name"]: (
                str(column["type"]),
                bool(column["nullable"]),
                (
                    cast(str, column["default"])[1:-1]
                    if engine.dialect.name == "sqlite"
                    and isinstance(column["type"], Boolean)
                    and column.get("default") in {"'0'", "'1'"}
                    else column.get("default")
                ),
                _freeze(column.get("computed")),
                _freeze(column.get("identity")),
            )
            for column in inspector.get_columns(table)
        }
        # Retain every reflected index field: expressions, predicates, column
        # sorting, include columns and unique flags all affect index behavior.
        # A skipped expression index is incomplete evidence, not convergence.
        with warnings.catch_warnings():
            warnings.simplefilter("error", SAWarning)
            indexes = {_freeze(index) for index in inspector.get_indexes(table)}
        foreign_keys = {
            (
                tuple(fk["constrained_columns"]),
                fk.get("referred_schema"),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                _freeze(fk.get("options", {})),
            )
            for fk in inspector.get_foreign_keys(table)
        }
        sqlite_foreign_key_actions: object = None
        if engine.dialect.name == "sqlite":
            # SQLite's SQL-text parser can omit inline REFERENCES actions.
            # PRAGMA reports the actual enforcement contract independently of
            # DDL formatting, including composite-key order (the seq column).
            quoted_table = engine.dialect.identifier_preparer.quote_identifier(table)
            with engine.connect() as connection:
                rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list({quoted_table})").all()
            sqlite_foreign_key_actions = frozenset(tuple(row[1:]) for row in rows)
        schema[table] = {
            "columns": columns,
            "primary_key": tuple(inspector.get_pk_constraint(table)["constrained_columns"]),
            "checks": {_freeze(check) for check in inspector.get_check_constraints(table)},
            "unique_constraints": {_freeze(unique) for unique in inspector.get_unique_constraints(table)},
            "indexes": indexes,
            "sqlite_index_definitions": (
                _sqlite_index_definitions(engine, table) if engine.dialect.name == "sqlite" else None
            ),
            "foreign_keys": foreign_keys,
            "sqlite_foreign_key_actions": sqlite_foreign_key_actions,
        }
    return schema
