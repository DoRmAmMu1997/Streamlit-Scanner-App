"""Tests for the SCAN-001 persistence schema (backend/storage/models.py).

These tests prove the schema satisfies SCAN-001's acceptance criteria using a
throwaway in-memory SQLite database — no real database file, no app wiring.

They double as a **worked example for SCAN-002 (Codex)**: the `session` fixture
below is exactly how to spin up a temporary SQLite database from the models
(`create_engine("sqlite://")` + `Base.metadata.create_all`). Reuse this pattern
for the repository/service tests in SCAN-002.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from backend.storage.models import Base, ScanResult, ScanRun, ScanStatus


@pytest.fixture
def session():
    """Yield a SQLAlchemy Session backed by a fresh in-memory SQLite database.

    Beginner note: ``sqlite://`` (no path) is an in-memory database. SQLAlchemy
    keeps it on a single shared connection for the test thread, so the tables we
    create with ``Base.metadata.create_all`` are visible to the Session that
    follows. The database vanishes when the engine is disposed — perfect test
    isolation with zero cleanup on disk.
    """
    engine = create_engine("sqlite://", future=True)

    # Turn on SQLite foreign-key enforcement so the FK's ON DELETE CASCADE can be
    # exercised at the database level (SQLite leaves FK enforcement OFF by default).
    # SCAN-002 should register the same listener on the real engine.
    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    with Session(engine) as active_session:
        yield active_session
    engine.dispose()


def _make_run(**overrides) -> ScanRun:
    """Build a ScanRun with sensible defaults; override any field per test."""
    values = {
        "started_at": dt.datetime(2026, 6, 4, 9, 30, tzinfo=dt.UTC),
        "screener_key": "envelope",
        "universe_key": "nifty_500",
        "params_json": {"ema_period": 200, "max_symbols": 500},
        "data_snapshot_date": dt.date(2026, 6, 3),
        "git_commit_sha": "c9b1620639d40c5a4e20cde877a0847c4b3533d9",
        "triggered_by": "cli",
    }
    values.update(overrides)
    return ScanRun(**values)


# ---------------------------------------------------------------------------
# Round-trip: a run plus AI and non-AI results survive a write/read cycle
# ---------------------------------------------------------------------------


def test_round_trip_persists_run_and_both_result_kinds(session):
    run = _make_run()

    # A deterministic screener result: provenance is triggered rules + indicators.
    deterministic = ScanResult(
        symbol="RELIANCE",
        signal_date=dt.date(2026, 6, 3),
        close_price=Decimal("1234.5000"),
        rating="BUY",
        reason="close below 200-EMA lower envelope",
        raw_result_json={"close": 1234.5, "ema_200": 1402.1, "latest_close": 1234.5},
        provenance_json={
            "triggered_rules": ["close <= lower_envelope"],
            "indicator_values": {"ema_200": 1402.1, "rsi_14": 31.8},
            "screener_version": "envelope@1",
        },
    )

    # An AI screener result: provenance carries model + prompt version + sources.
    ai = ScanResult(
        symbol="TCS",
        signal_date=dt.date(2026, 6, 3),
        close_price=Decimal("3890.0000"),
        rating="STRONG BUY",
        final_score=Decimal("82.00"),
        reason="Agent: improving margins, clean balance sheet",
        raw_result_json={"verdict": "STRONG BUY", "criteria_passed": 8},
        provenance_json={
            "model": "claude-sonnet-4-6",
            "prompt_version": "fundamentals_v1",
            "source_labels": ["screener.in", "concall_q4"],
        },
    )

    run.results.extend([deterministic, ai])
    session.add(run)
    session.commit()

    # Re-query from the database (not the in-memory object) to prove it persisted.
    session.expire_all()
    loaded = session.scalars(select(ScanRun)).one()

    # Run header survived, including the JSON params and the default status.
    assert loaded.status is ScanStatus.RUNNING  # default applied on insert
    assert loaded.screener_key == "envelope"
    assert loaded.params_json == {"ema_period": 200, "max_symbols": 500}
    assert loaded.data_snapshot_date == dt.date(2026, 6, 3)

    # Both result rows are linked to the run via the relationship.
    by_symbol = {r.symbol: r for r in loaded.results}
    assert set(by_symbol) == {"RELIANCE", "TCS"}

    # Non-AI provenance round-tripped as a nested dict/list structure.
    assert by_symbol["RELIANCE"].provenance_json["triggered_rules"] == [
        "close <= lower_envelope"
    ]
    # AI provenance round-tripped, including model + prompt version.
    assert by_symbol["TCS"].provenance_json["model"] == "claude-sonnet-4-6"
    assert by_symbol["TCS"].final_score == Decimal("82.00")

    # The back-reference resolves too: result.run points at the parent.
    assert by_symbol["TCS"].run.id == loaded.id


# ---------------------------------------------------------------------------
# Status enum: typed in Python, stored as its lowercase string value
# ---------------------------------------------------------------------------


def test_status_roundtrips_and_is_stored_as_its_lowercase_value(session):
    run = _make_run(status=ScanStatus.PARTIAL, error_message="2 symbols failed")
    session.add(run)
    session.commit()

    # As a Python object, status is the typed enum member.
    session.expire_all()
    loaded = session.scalars(select(ScanRun)).one()
    assert loaded.status is ScanStatus.PARTIAL

    # As a raw database value, it is the lowercase ``.value`` ("partial"),
    # NOT the Python name ("PARTIAL"). This is what values_callable guarantees,
    # and it keeps the stored data stable and human-readable.
    raw_value = session.execute(text("SELECT status FROM scan_runs")).scalar_one()
    assert raw_value == "partial"


def test_status_column_rejects_values_outside_scan_status_enum(session):
    """The database should reject typo statuses, not just the Python enum layer."""
    with pytest.raises(IntegrityError):
        session.execute(
            text(
                """
                INSERT INTO scan_runs (started_at, status, screener_key, universe_key)
                VALUES (:started_at, 'finished', 'envelope', 'nifty_500')
                """
            ),
            {"started_at": "2026-06-04 09:30:00"},
        )
        session.commit()


# ---------------------------------------------------------------------------
# Money is exact: Numeric preserves the value, unlike binary float
# ---------------------------------------------------------------------------


def test_close_price_is_exact_decimal(session):
    run = _make_run()
    # 0.07 has no exact binary float representation; Numeric must store it exactly.
    run.results.append(ScanResult(symbol="IDEA", close_price=Decimal("12.07")))
    session.add(run)
    session.commit()

    session.expire_all()
    result = session.scalars(select(ScanResult)).one()
    assert isinstance(result.close_price, Decimal)
    assert result.close_price == Decimal("12.07")


# ---------------------------------------------------------------------------
# Deleting a run removes its results (no orphans)
# ---------------------------------------------------------------------------


def test_deleting_run_cascades_to_results(session):
    run = _make_run()
    run.results.extend(
        [ScanResult(symbol="RELIANCE"), ScanResult(symbol="TCS")]
    )
    session.add(run)
    session.commit()
    assert session.scalar(select(text("count(*)")).select_from(ScanResult)) == 2

    # Deleting the parent must remove its children — the cascade="all, delete-orphan"
    # on ScanRun.results (and the DB-level ON DELETE CASCADE) prevents orphan rows.
    session.delete(run)
    session.commit()

    assert session.scalar(select(text("count(*)")).select_from(ScanResult)) == 0
    assert session.scalar(select(text("count(*)")).select_from(ScanRun)) == 0
