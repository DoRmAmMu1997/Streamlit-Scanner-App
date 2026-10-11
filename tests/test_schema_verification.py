"""Regression proofs that the schema fingerprint detects enforcement drift."""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.exc import IntegrityError
from test_scan_storage_migrations import _reflect_schema


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("CHECK (amount > 0)", "CHECK (amount >= 0)"),
        ("UNIQUE (code)", "UNIQUE (amount)"),
        ("PRIMARY KEY (id, code)", "PRIMARY KEY (code, id)"),
    ],
    ids=["check-expression", "unique-columns", "primary-key-order"],
)
def test_constraint_drift_is_visible(tmp_path: Path, before: str, after: str) -> None:
    """Keep identical column types/names while changing a real constraint.

    Beginner note:
        The old comparator only saw columns and index names, so it could approve
        a migration that accepts forbidden values or changes row identity.
    """
    schemas = []
    for position, constraint in enumerate((before, after)):
        engine = create_engine(f"sqlite:///{tmp_path / f'{position}.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql(
                f"CREATE TABLE sample (id INTEGER NOT NULL, code TEXT NOT NULL, "
                f"amount INTEGER NOT NULL, {constraint})"
            )
        schemas.append(_reflect_schema(engine))
    assert schemas[0] != schemas[1]


@pytest.mark.parametrize(
    ("before", "after"),
    [
        ("CREATE INDEX sample_idx ON sample (id, code)", "CREATE INDEX sample_idx ON sample (code, id)"),
        ("CREATE INDEX sample_idx ON sample (code)", "CREATE UNIQUE INDEX sample_idx ON sample (code)"),
        ("CREATE INDEX sample_idx ON sample (id) WHERE id > 0", "CREATE INDEX sample_idx ON sample (id) WHERE id >= 0"),
    ],
    ids=["index-column-order", "index-uniqueness", "index-predicate"],
)
def test_same_named_index_drift_is_visible(tmp_path: Path, before: str, after: str) -> None:
    """Changing an index definition must matter even when its name survives.

    Beginner note:
        A partial unique index only protects rows matching its predicate. A name
        comparison cannot prove either the uniqueness or the protected rows.
    """
    schemas = []
    for position, statement in enumerate((before, after)):
        engine = create_engine(f"sqlite:///{tmp_path / f'{position}.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE sample (id INTEGER, code TEXT)")
            connection.exec_driver_sql(statement)
        schemas.append(_reflect_schema(engine))
    assert schemas[0] != schemas[1]


@pytest.mark.parametrize("index_kind", ["index", "unique", "primary_key"])
def test_sqlite_unique_index_collation_drift_is_visible(tmp_path: Path, index_kind: str) -> None:
    """Expose different accepted values despite identical reflected index columns.

    Beginner note:
        SQLAlchemy omits SQLite index collations without a warning. Previously
        the comparator treated BINARY and NOCASE unique indexes as equivalent,
        even though only one accepts two differently capitalized spellings.
        Table-level UNIQUE and PRIMARY KEY rules use implicit indexes and need
        the same check.
    """
    schemas = []
    accepted_case_variant = []
    for position, collation in enumerate(("BINARY", "NOCASE")):
        engine = create_engine(f"sqlite:///{tmp_path / f'collation-{position}.db'}")
        try:
            with engine.begin() as connection:
                if index_kind != "index":
                    constraint = "UNIQUE" if index_kind == "unique" else "PRIMARY KEY"
                    connection.exec_driver_sql(
                        f"CREATE TABLE sample (code TEXT, {constraint} (code COLLATE {collation}))"
                    )
                else:
                    connection.exec_driver_sql("CREATE TABLE sample (code TEXT)")
                    connection.exec_driver_sql(
                        f"CREATE UNIQUE INDEX sample_idx ON sample (code COLLATE {collation})"
                    )
                connection.exec_driver_sql("INSERT INTO sample VALUES ('Mixed')")
                try:
                    connection.exec_driver_sql("INSERT INTO sample VALUES ('mixed')")
                except IntegrityError:
                    accepted_case_variant.append(False)
                else:
                    accepted_case_variant.append(True)
            schemas.append(_reflect_schema(engine))
        finally:
            engine.dispose()
    assert accepted_case_variant == [True, False]
    assert schemas[0] != schemas[1]


def test_sqlite_implicit_index_numbering_does_not_create_false_drift(tmp_path: Path) -> None:
    """Compare constraint definitions independently of SQLite autoindex suffixes.

    Beginner note:
        Reordering two equivalent UNIQUE declarations changes SQLite's generated
        index names. The new collation guard must retain both constraints while
        ignoring those incidental names, so harmless migration formatting does
        not become a false schema mismatch.
    """
    schemas = []
    for position, constraints in enumerate(("UNIQUE (id), UNIQUE (code)", "UNIQUE (code), UNIQUE (id)")):
        engine = create_engine(f"sqlite:///{tmp_path / f'autoindex-{position}.db'}")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(f"CREATE TABLE sample (id INTEGER, code TEXT, {constraints})")
            schemas.append(_reflect_schema(engine))
        finally:
            engine.dispose()
    assert schemas[0] == schemas[1]


def test_sqlite_index_sort_direction_drift_is_visible(tmp_path: Path) -> None:
    """Retain descending index order that SQLite's generic reflection drops.

    Beginner note:
        Both indexes have the same name and column list, but the second key's
        direction changes the ordering that the index supports. The fingerprint
        must compare the actual key definition, not just the column names.
    """
    schemas = []
    for position, direction in enumerate(("ASC", "DESC")):
        engine = create_engine(f"sqlite:///{tmp_path / f'direction-{position}.db'}")
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql("CREATE TABLE sample (id INTEGER, code TEXT)")
                connection.exec_driver_sql(f"CREATE INDEX sample_idx ON sample (id ASC, code {direction})")
            schemas.append(_reflect_schema(engine))
        finally:
            engine.dispose()
    assert schemas[0] != schemas[1]


@pytest.mark.parametrize("defaults", [("0", "1"), ("'A B'", "'A  B'"), ("'Mixed'", "'mixed'")])
def test_server_default_drift_preserves_literal_semantics(tmp_path: Path, defaults: tuple[str, str]) -> None:
    """Defaults and significant text inside SQL quotes must stay distinguishable.

    Beginner note:
        Removing all whitespace or lowercasing SQL also changes string literals.
        These pairs expose normalization that would hide different stored values.
    """
    schemas = []
    for position, default in enumerate(defaults):
        engine = create_engine(f"sqlite:///{tmp_path / f'{position}.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE TABLE sample (id INTEGER, code TEXT DEFAULT {default})")
        schemas.append(_reflect_schema(engine))
    assert schemas[0] != schemas[1]


def test_foreign_key_update_action_drift_is_visible(tmp_path: Path) -> None:
    """Keep FK names/targets/deletion rules equal while changing update behavior.

    Beginner note:
        The previous comparator retained ON DELETE but discarded ON UPDATE;
        that omission could approve a migration that breaks parent-key updates.
    """
    schemas = []
    for position, action in enumerate(("CASCADE", "RESTRICT")):
        engine = create_engine(f"sqlite:///{tmp_path / f'{position}.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql(
                "CREATE TABLE sample (parent_id INTEGER REFERENCES parent(id) "
                f"ON DELETE CASCADE ON UPDATE {action})"
            )
        schemas.append(_reflect_schema(engine))
    assert schemas[0] != schemas[1]


@pytest.mark.parametrize("boolean", [0, 1])
def test_sqlite_boolean_default_spellings_store_identical_values(tmp_path: Path, boolean: int) -> None:
    """Prove the precise SQLite Boolean formatting exception before normalizing.

    Beginner note:
        The migration uses DEFAULT 0; SQLAlchemy's string default emits '0'.
        SQLite BOOLEAN has numeric affinity, so both store integer zero. This
        exception must never be applied to TEXT, where quoted content matters.
    """
    schemas = []
    values = []
    for position, default in enumerate((str(boolean), f"'{boolean}'")):
        engine = create_engine(f"sqlite:///{tmp_path / f'bool-{position}.db'}")
        with engine.begin() as connection:
            connection.exec_driver_sql(f"CREATE TABLE sample (id INTEGER, flag BOOLEAN DEFAULT {default})")
            connection.exec_driver_sql("INSERT INTO sample (id) VALUES (1)")
            values.append(connection.exec_driver_sql("SELECT flag, typeof(flag) FROM sample").one())
        schemas.append(_reflect_schema(engine))
    assert values == [(boolean, "integer"), (boolean, "integer")]
    assert schemas[0] == schemas[1]


def test_unreflectable_sqlite_expression_index_is_not_silently_omitted(tmp_path: Path) -> None:
    """Unsupported reflection must fail closed instead of comparing incomplete schemas.

    Beginner note:
        SQLAlchemy warns and skips SQLite expression indexes. Ignoring that
        warning would let a missing enforcement index evade this comparator.
    """
    from sqlalchemy.exc import SAWarning

    engine = create_engine(f"sqlite:///{tmp_path / 'expression.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE sample (code TEXT)")
        connection.exec_driver_sql("CREATE UNIQUE INDEX ix_code ON sample (lower(code))")
    with pytest.raises(SAWarning, match="expression-based index"):
        _reflect_schema(engine)
