"""TEST-002 integration coverage for one complete scan run.

What makes this an integration test
-----------------------------------
Unit tests usually isolate one function. This test intentionally crosses the
same boundaries a real scan crosses after SCAN-003:

1. A caller provides a universe, data loader, screener callable, and params.
2. ``backend.scanning.run_scan`` creates a ``scan_runs`` audit row.
3. The fake screener returns shortlisted rows.
4. The repository saves ``scan_results`` and marks the run finished.
5. History queries read the saved run and results back.

The important safety rule: every external dependency is fake. There is no Dhan
client, no Streamlit browser, no LLM, no Screener.in request, and no real
``data/scanner.db`` write. The only database is a temporary SQLite file created
by pytest for this test.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from decimal import Decimal

import pandas as pd
import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.scanning import ScanStatus, run_scan
from backend.storage.models import Base
from backend.storage.repository import get_latest_scan_runs, get_scan_results


@pytest.fixture
def integration_engine(tmp_path) -> Iterator[Engine]:
    """Create a throwaway SQLite database that behaves like local scan history.

    Beginner note:
    A file-backed temp DB is still fast, but it is closer to the real
    ``data/scanner.db`` than an in-memory database. Separate SQLAlchemy sessions
    can open separate connections and still see the same rows, which is exactly
    what a scan service plus history query needs to prove.
    """
    db_path = tmp_path / "test_scan_history.db"
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        future=True,
    )

    @event.listens_for(engine, "connect")
    def _enable_sqlite_foreign_keys(dbapi_connection, _connection_record):
        """Match the app's SQLite safety setting for parent/child rows."""
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    Base.metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def integration_session_factory(integration_engine):
    """Return the transaction helper that ``run_scan`` expects.

    The production service defaults to ``backend.storage.database.session_scope``.
    Tests inject this factory instead so the real service/repository code writes
    to the temporary database above, never to the developer's local history file.
    """

    @contextmanager
    def factory():
        with Session(integration_engine) as session:
            try:
                yield session
                session.commit()
            except Exception:
                session.rollback()
                raise

    return factory


class _FakeDataLoader:
    """Offline stand-in for the real Dhan-backed daily data loader.

    ``run_scan`` only needs failure bookkeeping from the loader. The extra stats
    mirror the fields ``app.py`` reads after a real scan, making the fake look
    like the production object without opening any broker/network connection.
    """

    def __init__(self, last_failures: list[dict] | None = None) -> None:
        # A partial integration test can pass loader failures here to exercise
        # the real SCAN-003 PARTIAL classification. The happy-path test leaves it
        # empty, matching a clean Dhan/cache load.
        self.last_failures: list[dict] = list(last_failures or [])
        self.last_cache_hits = 2
        self.last_cache_misses = 0
        self.last_api_attempts = 0
        self.last_rate_limit_retries = 0


def test_full_scan_run_persists_results_and_history_can_be_queried(
    integration_engine,
    integration_session_factory,
):
    """Run the real scan service with fake inputs and query the saved history."""
    fake_universe = pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TCS", "INFY"],
            "company_name": ["Reliance Industries", "Tata Consultancy", "Infosys"],
        }
    )
    fake_loader = _FakeDataLoader()
    scan_params = {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 6, 2),
        "min_score": Decimal("80.5"),
        # A UI progress callback is a function and must be dropped from the
        # persisted params snapshot. JSON columns cannot store callables.
        "progress_callback": lambda *_args: None,
    }
    screener_seen: dict[str, object] = {}

    def fake_screener(universe_df, data_loader, params):
        """Deterministic screener that proves the service passes real inputs in."""
        screener_seen["symbols"] = tuple(universe_df["symbol"])
        screener_seen["loader_is_fake"] = data_loader is fake_loader
        screener_seen["has_compute_callback"] = callable(
            params.get("compute_failure_callback")
        )
        return pd.DataFrame(
            [
                {
                    "symbol": "RELIANCE",
                    "rating": "BUY",
                    "signal_date": date(2026, 6, 1),
                    "close": Decimal("1234.5678"),
                    "final_score": Decimal("91.50"),
                    "reason": "fake breakout with rising volume",
                    "provenance": {
                        "rules": ["fake_breakout", "volume_confirmation"],
                        "observed_at": date(2026, 6, 1),
                    },
                },
                {
                    "symbol": "TCS",
                    "rating": "WATCH",
                    "signal_date": "2026-06-01",
                    "close_price": Decimal("3890.25"),
                    "final_score": Decimal("82.25"),
                    "reason": "fake pullback near support",
                    "extra_note": "kept in raw_result_json",
                },
            ]
        )

    result = run_scan(
        screener_key="fake_integration_screener",
        universe_key="fake_universe",
        run_callable=fake_screener,
        universe_df=fake_universe,
        data_loader=fake_loader,
        params=scan_params,
        triggered_by="ui:test@example.com",
        session_factory=integration_session_factory,
    )

    assert result.status is ScanStatus.SUCCESS
    assert result.run_id is not None
    assert result.error_message is None
    assert list(result.results["symbol"]) == ["RELIANCE", "TCS"]
    assert screener_seen == {
        "symbols": ("RELIANCE", "TCS", "INFY"),
        "loader_is_fake": True,
        "has_compute_callback": True,
    }

    # Query through the public repository helpers, the same API a future history
    # page should use. This proves the scan was not merely returned in memory; it
    # was actually committed to the temporary database.
    with Session(integration_engine) as session:
        runs = get_latest_scan_runs(session, limit=5)
        assert len(runs) == 1

        saved_run = runs[0]
        assert saved_run.id == result.run_id
        assert saved_run.status is ScanStatus.SUCCESS
        assert saved_run.screener_key == "fake_integration_screener"
        assert saved_run.universe_key == "fake_universe"
        assert saved_run.triggered_by == "ui:test@example.com"
        assert saved_run.started_at is not None
        assert saved_run.finished_at is not None
        assert saved_run.started_at <= saved_run.finished_at
        assert saved_run.error_message is None
        assert saved_run.data_snapshot_date == date(2026, 6, 2)
        assert saved_run.params_json == {
            "start_date": "2026-01-01",
            "end_date": "2026-06-02",
            "min_score": "80.5",
        }

        rows = get_scan_results(session, saved_run.id)
        assert [row.symbol for row in rows] == ["RELIANCE", "TCS"]
        assert rows[0].signal_date == date(2026, 6, 1)
        assert rows[0].close_price == Decimal("1234.5678")
        assert rows[0].final_score == Decimal("91.50")
        assert rows[0].reason == "fake breakout with rising volume"
        assert rows[0].raw_result_json["close"] == "1234.5678"
        assert rows[0].provenance_json == {
            "rules": ["fake_breakout", "volume_confirmation"],
            "observed_at": "2026-06-01",
        }
        assert rows[1].rating == "WATCH"
        assert rows[1].close_price == Decimal("3890.2500")
        assert rows[1].raw_result_json["extra_note"] == "kept in raw_result_json"


def test_partial_scan_run_persists_status_message_and_results(
    integration_engine,
    integration_session_factory,
):
    """A partial integration run should persist both usable rows and failures.

    Service-level unit tests already prove the PARTIAL decision in isolation.
    This integration case crosses the database boundary too: it verifies the
    returned PARTIAL status, the committed ``scan_runs`` status/error text, and
    the saved ``scan_results`` rows all agree in the same temp SQLite database.
    """
    fake_universe = pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TCS", "INFY"],
            "company_name": ["Reliance Industries", "Tata Consultancy", "Infosys"],
        }
    )
    fake_loader = _FakeDataLoader(
        last_failures=[
            {
                "symbol": "INFY",
                "security_id": "1594",
                "message": "offline fixture load failure",
            }
        ]
    )
    scan_params = {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 6, 2),
    }

    def partial_screener(_universe_df, _data_loader, params):
        """Return one usable row while reporting one per-symbol compute failure."""
        params["compute_failure_callback"](
            {"symbol": "TCS", "message": "offline fixture compute failure"}
        )
        return pd.DataFrame(
            [
                {
                    "symbol": "RELIANCE",
                    "rating": "BUY",
                    "signal_date": date(2026, 6, 1),
                    "close": Decimal("2500.00"),
                    "reason": "usable row survived partial scan",
                }
            ]
        )

    result = run_scan(
        screener_key="fake_partial_screener",
        universe_key="fake_universe",
        run_callable=partial_screener,
        universe_df=fake_universe,
        data_loader=fake_loader,
        params=scan_params,
        triggered_by="job:daily_scan",
        session_factory=integration_session_factory,
    )

    assert result.status is ScanStatus.PARTIAL
    assert result.run_id is not None
    assert result.error_message == (
        "1 symbol(s) failed to load and 1 failed to compute."
    )
    assert result.compute_failures == [
        {"symbol": "TCS", "message": "offline fixture compute failure"}
    ]
    assert list(result.results["symbol"]) == ["RELIANCE"]

    with Session(integration_engine) as session:
        runs = get_latest_scan_runs(session, limit=5)
        assert len(runs) == 1

        saved_run = runs[0]
        assert saved_run.id == result.run_id
        assert saved_run.status is ScanStatus.PARTIAL
        assert saved_run.screener_key == "fake_partial_screener"
        assert saved_run.triggered_by == "job:daily_scan"
        assert saved_run.finished_at is not None
        assert saved_run.error_message == result.error_message

        rows = get_scan_results(session, saved_run.id)
        assert [row.symbol for row in rows] == ["RELIANCE"]
        assert rows[0].close_price == Decimal("2500.0000")
        assert rows[0].raw_result_json["reason"] == "usable row survived partial scan"


def test_failed_scan_run_persists_secret_safe_error_and_no_results(
    integration_engine,
    integration_session_factory,
):
    """A failed integration run should persist FAILED without leaking secrets.

    The fake exception contains a deliberately obvious secret marker. The
    integration assertion checks both service output and committed database rows
    so a future history UI cannot accidentally expose raw broker/API exception
    text from ``scan_runs.error_message``.
    """
    fake_universe = pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TCS"],
            "company_name": ["Reliance Industries", "Tata Consultancy"],
        }
    )
    fake_loader = _FakeDataLoader()

    def failed_screener(_universe_df, _data_loader, _params):
        """Raise before producing rows, like a fatal screener bug would."""
        raise RuntimeError("token=SUPERSECRET should never be persisted")

    result = run_scan(
        screener_key="fake_failed_screener",
        universe_key="fake_universe",
        run_callable=failed_screener,
        universe_df=fake_universe,
        data_loader=fake_loader,
        params={"start_date": date(2026, 1, 1), "end_date": date(2026, 6, 2)},
        triggered_by="job:daily_scan",
        session_factory=integration_session_factory,
    )

    assert result.status is ScanStatus.FAILED
    assert result.run_id is not None
    assert result.results.empty
    assert "RuntimeError" in (result.error_message or "")
    assert "SUPERSECRET" not in (result.error_message or "")

    with Session(integration_engine) as session:
        runs = get_latest_scan_runs(session, limit=5)
        assert len(runs) == 1

        saved_run = runs[0]
        assert saved_run.id == result.run_id
        assert saved_run.status is ScanStatus.FAILED
        assert saved_run.screener_key == "fake_failed_screener"
        assert saved_run.finished_at is not None
        assert "RuntimeError" in (saved_run.error_message or "")
        assert "SUPERSECRET" not in (saved_run.error_message or "")
        assert get_scan_results(session, saved_run.id) == []
