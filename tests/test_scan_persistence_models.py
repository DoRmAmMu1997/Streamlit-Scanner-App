"""Tests for the SCAN-001 persistence schema (backend/storage/models.py).

These tests prove the schema satisfies SCAN-001's acceptance criteria using a
throwaway in-memory SQLite database — no real database file, no app wiring.

They double as a **worked example for SCAN-002 (Codex)**: the `db_session` fixture
below is exactly how to spin up a temporary SQLite database from the models
(`create_engine("sqlite://")` + `Base.metadata.create_all`). Reuse this pattern
for the repository/service tests in SCAN-002.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError

from backend.storage.models import (
    AIEvaluation,
    ForwardReturnStatus,
    ScanResult,
    ScanRun,
    ScanStatus,
    SignalForwardReturn,
)

# The ``db_session`` fixture these tests use lives in tests/conftest.py,
# shared with the other scan-history test modules.


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


def test_round_trip_persists_run_and_both_result_kinds(db_session):
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
    db_session.add(run)
    db_session.commit()

    # Re-query from the database (not the in-memory object) to prove it persisted.
    db_session.expire_all()
    loaded = db_session.scalars(select(ScanRun)).one()

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


def test_status_roundtrips_and_is_stored_as_its_lowercase_value(db_session):
    run = _make_run(status=ScanStatus.PARTIAL, error_message="2 symbols failed")
    db_session.add(run)
    db_session.commit()

    # As a Python object, status is the typed enum member.
    db_session.expire_all()
    loaded = db_session.scalars(select(ScanRun)).one()
    assert loaded.status is ScanStatus.PARTIAL

    # As a raw database value, it is the lowercase ``.value`` ("partial"),
    # NOT the Python name ("PARTIAL"). This is what values_callable guarantees,
    # and it keeps the stored data stable and human-readable.
    raw_value = db_session.execute(text("SELECT status FROM scan_runs")).scalar_one()
    assert raw_value == "partial"


def test_status_column_rejects_values_outside_scan_status_enum(db_session):
    """The database should reject typo statuses, not just the Python enum layer."""
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO scan_runs (started_at, status, screener_key, universe_key)
                VALUES (:started_at, 'finished', 'envelope', 'nifty_500')
                """
            ),
            {"started_at": "2026-06-04 09:30:00"},
        )
        db_session.commit()


# ---------------------------------------------------------------------------
# Money is exact: Numeric preserves the value, unlike binary float
# ---------------------------------------------------------------------------


def test_close_price_is_exact_decimal(db_session):
    run = _make_run()
    # 0.07 has no exact binary float representation; Numeric must store it exactly.
    run.results.append(ScanResult(symbol="IDEA", close_price=Decimal("12.07")))
    db_session.add(run)
    db_session.commit()

    db_session.expire_all()
    result = db_session.scalars(select(ScanResult)).one()
    assert isinstance(result.close_price, Decimal)
    assert result.close_price == Decimal("12.07")


# ---------------------------------------------------------------------------
# Deleting a run removes its results (no orphans)
# ---------------------------------------------------------------------------


def test_deleting_run_cascades_to_results(db_session):
    run = _make_run()
    run.results.extend(
        [ScanResult(symbol="RELIANCE"), ScanResult(symbol="TCS")]
    )
    db_session.add(run)
    db_session.commit()
    assert db_session.scalar(select(text("count(*)")).select_from(ScanResult)) == 2

    # Deleting the parent must remove its children — the cascade="all, delete-orphan"
    # on ScanRun.results (and the DB-level ON DELETE CASCADE) prevents orphan rows.
    db_session.delete(run)
    db_session.commit()

    assert db_session.scalar(select(text("count(*)")).select_from(ScanResult)) == 0
    assert db_session.scalar(select(text("count(*)")).select_from(ScanRun)) == 0


def test_ai_evaluation_round_trip_and_run_delete_cascade(db_session):
    run = _make_run()
    run.ai_evaluations.append(
        AIEvaluation(
            symbol="TCS",
            signal_date=dt.date(2026, 6, 13),
            outcome="approved",
            verdict_label="BUY",
            confidence=Decimal("0.9300"),
            model_name="gpt-test",
            prompt_version="v1",
            validated_verdict_json={"rating": "BUY"},
            provenance_json={"prompt_sha256": "a" * 64},
            created_at=dt.datetime(2026, 6, 13, 8, 0, tzinfo=dt.UTC),
        )
    )
    db_session.add(run)
    db_session.commit()

    loaded = db_session.scalars(select(AIEvaluation)).one()
    assert loaded.run_id == run.id
    assert loaded.confidence == Decimal("0.9300")

    db_session.delete(run)
    db_session.commit()
    assert db_session.scalar(select(text("count(*)")).select_from(AIEvaluation)) == 0


# ---------------------------------------------------------------------------
# VALID-001 — forward-return measurements (signal_forward_returns)
#
# These mirror the SCAN-001 tests above and double as the VALID-002 (Codex)
# template: the same db_session fixture, the same round-trip / enum / Decimal /
# cascade assertions, now over the forward-return table.
# ---------------------------------------------------------------------------


def _make_signal(db_session, **overrides) -> ScanResult:
    """Persist a run + one shortlisted result and return the result (id populated)."""
    run = _make_run()
    values = {
        "symbol": "RELIANCE",
        "signal_date": dt.date(2026, 1, 5),
        "close_price": Decimal("1234.5000"),
        "rating": "BUY",
    }
    values.update(overrides)
    result = ScanResult(**values)
    run.results.append(result)
    db_session.add(run)
    db_session.commit()
    return result


def test_forward_return_round_trip_with_pending_and_computed_rows(db_session):
    result = _make_signal(db_session)

    # One horizon already measured (computed), one still waiting (pending default).
    computed = SignalForwardReturn(
        result_id=result.id,
        horizon_days=20,
        status=ForwardReturnStatus.COMPUTED,
        entry_date=dt.date(2026, 1, 6),
        exit_date=dt.date(2026, 2, 3),
        entry_price=Decimal("100.0000"),
        exit_price=Decimal("112.5000"),
        forward_return_pct=Decimal("12.5000"),
        benchmark_key="nifty_50",
        benchmark_return_pct=Decimal("3.0000"),
        excess_return_pct=Decimal("9.5000"),
        max_adverse_excursion_pct=Decimal("-4.2000"),
        max_favorable_excursion_pct=Decimal("15.1000"),
        computed_at=dt.datetime(2026, 2, 3, 12, 0, tzinfo=dt.UTC),
    )
    pending = SignalForwardReturn(result_id=result.id, horizon_days=120)

    db_session.add_all([computed, pending])
    db_session.commit()

    db_session.expire_all()
    loaded = db_session.scalars(select(ScanResult)).one()
    by_horizon = {fr.horizon_days: fr for fr in loaded.forward_returns}
    assert set(by_horizon) == {20, 120}

    # The computed leg round-tripped, including the benchmark/excess and MAE/MFE.
    twenty = by_horizon[20]
    assert twenty.status is ForwardReturnStatus.COMPUTED
    assert twenty.forward_return_pct == Decimal("12.5000")
    assert twenty.excess_return_pct == Decimal("9.5000")
    assert twenty.max_adverse_excursion_pct == Decimal("-4.2000")
    # The back-reference resolves to the parent signal.
    assert twenty.result.symbol == "RELIANCE"

    # The pending leg defaulted its status and left prices NULL (no lookahead guess).
    assert by_horizon[120].status is ForwardReturnStatus.PENDING
    assert by_horizon[120].forward_return_pct is None
    assert by_horizon[120].computed_at is None


def test_forward_return_status_stored_as_lowercase_value(db_session):
    result = _make_signal(db_session)
    db_session.add(
        SignalForwardReturn(
            result_id=result.id,
            horizon_days=60,
            status=ForwardReturnStatus.INSUFFICIENT_DATA,
        )
    )
    db_session.commit()

    # Stored as the lowercase ``.value`` ("insufficient_data"), not the Python name —
    # the values_callable guarantee, same as ScanStatus.
    raw_value = db_session.execute(
        text("SELECT status FROM signal_forward_returns")
    ).scalar_one()
    assert raw_value == "insufficient_data"


def test_forward_return_status_rejects_values_outside_the_enum(db_session):
    """The database CHECK rejects typo statuses, not just the Python enum layer."""
    result = _make_signal(db_session)
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                """
                INSERT INTO signal_forward_returns (result_id, horizon_days, status, created_at)
                VALUES (:result_id, 20, 'done', :created_at)
                """
            ),
            {"result_id": result.id, "created_at": "2026-02-03 12:00:00"},
        )
        db_session.commit()


def test_forward_return_is_unique_per_result_and_horizon(db_session):
    """One measurement per (signal, horizon) — the idempotent-upsert contract."""
    result = _make_signal(db_session)
    db_session.add(SignalForwardReturn(result_id=result.id, horizon_days=20))
    db_session.commit()

    with pytest.raises(IntegrityError):
        db_session.add(SignalForwardReturn(result_id=result.id, horizon_days=20))
        db_session.commit()


def test_deleting_run_cascades_through_results_to_forward_returns(db_session):
    result = _make_signal(db_session)
    db_session.add_all(
        [
            SignalForwardReturn(result_id=result.id, horizon_days=20),
            SignalForwardReturn(result_id=result.id, horizon_days=60),
        ]
    )
    db_session.commit()
    assert (
        db_session.scalar(select(text("count(*)")).select_from(SignalForwardReturn)) == 2
    )

    # Deleting the parent run must reach all the way down: run → results → forward returns.
    run = db_session.scalars(select(ScanRun)).one()
    db_session.delete(run)
    db_session.commit()

    assert (
        db_session.scalar(select(text("count(*)")).select_from(SignalForwardReturn)) == 0
    )
    assert db_session.scalar(select(text("count(*)")).select_from(ScanResult)) == 0
