"""REFACTOR-002 guard — keep raw database access inside ``backend/storage``.

Beginner note:
A *repository layer* is the one place that is allowed to talk to the database in
SQL terms. Everything else in the app (Streamlit pages, the scan service, jobs)
should call friendly helper functions like ``create_scan_run`` or
``get_latest_scan_runs`` and never build a ``select(...)`` statement, open a
database engine, or create a session by hand. That keeps two promises from the
REFACTOR-002 ticket true forever:

- "UI does not write SQL directly."
- "Scan service does not know DB internals."

The repository helpers already exist (``backend/storage/repository.py``) and every
caller already routes through them. This test is a *guard rail*: it reads the
source code of the app layers and **fails the build** if anyone re-introduces raw
database access outside ``backend/storage``. It is written in the same spirit as
``tests/test_supply_chain_policy.py`` — a cheap, static check that catches drift
before a PR can merge.

How the check works (no third-party tools, just the standard-library ``ast``):

1. Importing from the *top-level* ``sqlalchemy`` package is rejected — that
   namespace is where ``select``, ``insert``, ``update``, ``delete``, ``text``,
   ``create_engine`` and friends live. App layers should never need it.
2. SQLAlchemy submodule imports are rejected unless they are the explicit
   ``Session`` type hint or an exception class used for graceful failures.
3. Importing ``engine`` / ``SessionLocal`` / ``init_db`` through the storage
   package is rejected because those names let callers bypass the repository.
4. Direct ORM reads, writes, flushes, and transaction methods are rejected in
   modules that import SQLAlchemy's ``Session`` type. Requiring that type import
   keeps HTTP ``session.get(...)`` and pandas ``frame.query(...)`` valid.

Deliberately allowed, because they are not "DB internals":

- ``from sqlalchemy.orm import Session`` — a type hint for a session the *caller*
  owns and passes in. Services accept a ``Session`` parameter; they never make one.
- ``from sqlalchemy.exc import OperationalError`` — so the UI can catch a
  "database unavailable" error and degrade gracefully.
- Importing repository helpers or ORM model classes from ``backend.storage``.
"""

from __future__ import annotations

import ast
from collections.abc import Iterator
from pathlib import Path

# Repository root: the folder that holds ``app.py``, ``backend/``, ``ui/`` and
# this ``tests/`` directory. ``parents[1]`` walks up from ``tests/<thisfile>``.
ROOT = Path(__file__).resolve().parents[1]

# The "app layers" that must stay free of raw database access. We intentionally do
# NOT scan ``backend/storage`` (the sanctioned home of SQL), ``migrations/``
# (hand-written DDL), ``tests/`` (which may use raw SQL to verify behaviour), or
# ``Dependencies/`` (vendored helper code).
SCANNED_FILES: tuple[Path, ...] = (ROOT / "app.py",)
SCANNED_DIRS: tuple[Path, ...] = (
    ROOT / "backend",
    ROOT / "screeners",
    ROOT / "ui",
)

# A POSIX path fragment whose files are exempt even though they live under a
# scanned directory: the repository/models/database modules are *meant* to build
# SQL, open engines, and create sessions.
STORAGE_EXEMPT_FRAGMENT = "backend/storage/"

# Only two SQLAlchemy import shapes belong outside persistence: the ``Session``
# annotation used by caller-owned transactions and exception classes used for
# graceful UI/service failures. Everything else exposes query or engine details.
ALLOWED_SQLALCHEMY_IMPORTS = {"sqlalchemy.orm": frozenset({"Session"})}
ALLOWED_SQLALCHEMY_MODULES = frozenset({"sqlalchemy.exc"})

# ``backend.storage`` intentionally re-exports convenient public helpers, but
# these infrastructure objects would let callers create connections or sessions
# themselves. ``session_scope`` stays allowed because callers own transactions.
BANNED_STORAGE_IMPORT_NAMES = frozenset({"engine", "SessionLocal", "init_db"})

# Receiver variable names that, in this codebase, almost always refer to a
# SQLAlchemy ``Session`` or ``Connection``. We inspect these names only when the
# same module imports the SQLAlchemy ``Session`` type, which avoids confusing an
# HTTP client session with a database session.
SESSION_RECEIVER_NAMES = frozenset(
    {"session", "sess", "db_session", "db", "connection", "conn"}
)
SESSION_METHOD_NAMES = frozenset(
    {
        "add",
        "add_all",
        "begin",
        "commit",
        "delete",
        "execute",
        "expunge",
        "flush",
        "get",
        "merge",
        "query",
        "refresh",
        "rollback",
        "scalar",
        "scalars",
    }
)

# Shown in the failure message so a future contributor knows exactly what to do.
REMEDIATION = (
    "Route all database access through backend.storage repository helpers "
    "(e.g. create_scan_run, get_latest_scan_runs, create_audit_log_entry) and "
    "obtain sessions via backend.storage.session_scope. Only modules under "
    "backend/storage may build raw SQL, engines, or sessions."
)


def _iter_scanned_files() -> Iterator[Path]:
    """Yield every ``.py`` file in the scanned app layers, minus the exemptions."""
    for file_path in SCANNED_FILES:
        if file_path.is_file():
            yield file_path
    for directory in SCANNED_DIRS:
        if not directory.is_dir():
            continue
        for file_path in directory.rglob("*.py"):
            if STORAGE_EXEMPT_FRAGMENT in file_path.as_posix():
                continue
            if "__pycache__" in file_path.parts:
                continue
            yield file_path


def _receiver_root_name(node: ast.expr) -> str | None:
    """Return the identifier a method call is invoked on, if it is a simple name.

    For ``session.query(...)`` the receiver is ``session`` (an ``ast.Name``) and we
    return ``"session"``. For ``self.session.query(...)`` the receiver is the
    attribute ``self.session`` (an ``ast.Attribute``) and we return its final
    attribute name, ``"session"``. Anything more complex (a subscript, another
    call, ...) returns ``None`` and is simply not matched.
    """
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def _iter_violations(file_path: Path) -> Iterator[tuple[int, str]]:
    """Yield ``(line_number, human_reason)`` for each raw-DB-access pattern found."""
    source = file_path.read_text(encoding="utf-8")
    # ``filename`` only improves error messages if the file fails to parse.
    tree = ast.parse(source, filename=str(file_path))
    imports_sqlalchemy_session = any(
        isinstance(node, ast.ImportFrom)
        and node.module == "sqlalchemy.orm"
        and any(alias.name == "Session" for alias in node.names)
        for node in ast.walk(tree)
    )

    for node in ast.walk(tree):
        # 1. ``import sqlalchemy`` or ``import sqlalchemy.orm as orm``.
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == "sqlalchemy" or alias.name.startswith("sqlalchemy."):
                    yield (
                        node.lineno,
                        f"`import {alias.name}` "
                        "(build SQL only inside backend/storage)",
                    )

        # 2. ``from sqlalchemy[...] import ...``.
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module == "sqlalchemy":
                imported = ", ".join(alias.name for alias in node.names)
                yield (
                    node.lineno,
                    f"`from sqlalchemy import {imported}` "
                    "(the top-level sqlalchemy namespace is SQL construction)",
                )
            elif module.startswith("sqlalchemy.") and module not in ALLOWED_SQLALCHEMY_MODULES:
                allowed_names = ALLOWED_SQLALCHEMY_IMPORTS.get(module, frozenset())
                for alias in node.names:
                    if alias.name not in allowed_names:
                        yield (
                            node.lineno,
                            f"`from {module} import {alias.name}` "
                            "(SQLAlchemy internals belong in backend/storage)",
                        )
            elif module in {"backend.storage", "backend.storage.database"}:
                for alias in node.names:
                    if alias.name in BANNED_STORAGE_IMPORT_NAMES:
                        yield (
                            node.lineno,
                            f"`from {module} import {alias.name}` "
                            "(connections and session factories stay inside storage)",
                        )

        # 3. Direct SQLAlchemy ``Session`` methods in service/UI modules.
        elif (
            imports_sqlalchemy_session
            and isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in SESSION_METHOD_NAMES
        ):
            receiver = _receiver_root_name(node.func.value)
            if receiver in SESSION_RECEIVER_NAMES:
                yield (
                    node.lineno,
                    f"`{receiver}.{node.func.attr}(...)` "
                    "(use a backend.storage repository helper instead)",
                )


def test_no_raw_database_access_outside_storage_layer() -> None:
    """App layers must not build SQL, engines, or sessions themselves (REFACTOR-002).

    This is the actual guard. It is expected to PASS on the current tree (every
    caller already routes through the repository) and to FAIL the moment someone
    adds raw database access to ``app.py``, ``backend`` (outside ``storage``),
    ``screeners``, or ``ui``.
    """
    scanned = sorted(_iter_scanned_files())
    scanned_relative = {path.relative_to(ROOT).as_posix() for path in scanned}

    # Guard the guard: if ROOT were miscomputed or the app layers moved, the scan
    # would silently cover nothing and pass vacuously. Assert that it actually
    # reached representative, stable modules — and that the storage package is
    # correctly exempted (otherwise the guard would flag its legitimate SQL).
    assert "app.py" in scanned_relative
    assert "backend/scanning/service.py" in scanned_relative
    assert "ui/history_page.py" in scanned_relative
    assert "backend/storage/repository.py" not in scanned_relative

    violations: list[str] = []
    for file_path in scanned:
        relative = file_path.relative_to(ROOT).as_posix()
        for lineno, reason in _iter_violations(file_path):
            violations.append(f"{relative}:{lineno} -> {reason}")

    assert not violations, (
        "Raw database access found outside backend/storage:\n"
        + "\n".join(violations)
        + "\n\n"
        + REMEDIATION
    )


def test_guard_detects_known_raw_access_patterns(tmp_path: Path) -> None:
    """The detector itself must flag every pattern REFACTOR-002 forbids.

    Without this self-test the guard above could silently rot into a no-op (for
    example if a future edit broke the AST walk). We feed it a tiny sample file
    that mixes forbidden lines with the explicitly-allowed ones and assert that
    exactly the forbidden lines are reported.
    """
    sample = tmp_path / "offender.py"
    sample.write_text(
        "\n".join(
            [
                "import sqlalchemy",  # forbidden: whole-package access
                "from sqlalchemy import select, text",  # forbidden: SQL builders
                "from sqlalchemy.orm import sessionmaker",  # forbidden: session factory
                "from sqlalchemy.orm import Session  # allowed: type hint only",
                "from sqlalchemy.exc import OperationalError  # allowed: catch errors",
                "def go(session, frame):",
                "    frame.query('a > 1')  # allowed: pandas, not a DB session",
                "    return session.query(object)  # forbidden: legacy ORM API",
            ]
        ),
        encoding="utf-8",
    )

    reasons = [reason for _lineno, reason in _iter_violations(sample)]

    # Exactly four forbidden lines should be reported.
    assert len(reasons) == 4, reasons
    assert any("import sqlalchemy" in reason for reason in reasons)
    assert any("top-level sqlalchemy namespace" in reason for reason in reasons)
    assert any("sessionmaker" in reason for reason in reasons)
    assert any("session.query(...)" in reason for reason in reasons)

    # The allowed lines must never appear in the report.
    joined = " ".join(reasons)
    assert "OperationalError" not in joined
    assert "frame.query" not in joined
    assert "import Session " not in joined


def test_guard_detects_submodule_factory_and_direct_session_bypasses(tmp_path: Path) -> None:
    """Equivalent import paths and ORM methods must not bypass the boundary.

    These examples are deliberately different from the original self-test. A
    denylist that checks only top-level SQLAlchemy imports and ``query`` /
    ``execute`` would miss all of them even though each one exposes database
    mechanics outside ``backend/storage``.
    """
    sample = tmp_path / "subtle_offender.py"
    sample.write_text(
        "\n".join(
            [
                "from sqlalchemy.sql import select",
                "from sqlalchemy.dialects.postgresql import insert",
                "from sqlalchemy.exciting import create_engine",
                "from sqlalchemy.orm import Session",
                "from backend.storage import SessionLocal",
                "def load(session: Session):",
                "    row = session.get(object, 1)",
                "    session.flush()",
                "    return row",
            ]
        ),
        encoding="utf-8",
    )

    reasons = [reason for _lineno, reason in _iter_violations(sample)]
    joined = " ".join(reasons)

    assert "sqlalchemy.sql" in joined
    assert "sqlalchemy.dialects.postgresql" in joined
    assert "sqlalchemy.exciting" in joined
    assert "SessionLocal" in joined
    assert "session.get(...)" in joined
    assert "session.flush(...)" in joined


def test_guard_allows_non_database_session_and_dataframe_methods(tmp_path: Path) -> None:
    """HTTP ``session.get`` and pandas ``frame.query`` remain valid application code."""
    sample = tmp_path / "non_database_sessions.py"
    sample.write_text(
        "\n".join(
            [
                "import requests",
                "def fetch(session: requests.Session, frame):",
                "    response = session.get('https://example.com')",
                "    return response, frame.query('score > 0')",
            ]
        ),
        encoding="utf-8",
    )

    assert list(_iter_violations(sample)) == []
