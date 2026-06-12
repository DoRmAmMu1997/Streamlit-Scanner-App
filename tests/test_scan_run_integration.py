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

The three tests cover the three persisted end states a future history UI needs
to trust:

- SUCCESS: rows were produced and every symbol path completed.
- PARTIAL: useful rows exist, but the run also recorded per-symbol problems.
- FAILED: the screener aborted before producing usable rows.

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
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from backend.scanning import ScanStatus, run_scan
from backend.storage.database import _make_engine
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
    # Reuse the production engine factory so this integration test exercises the
    # exact SQLite settings real scan history uses: foreign-key cascades plus the
    # WAL/busy-timeout concurrency pragmas. tests/test_scan_storage_database.py
    # uses the same factory.
    engine = _make_engine(f"sqlite:///{db_path.as_posix()}")
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

    Beginner note:
    In the real app, loader failures mean "we could not fetch candles for one
    symbol" (rate limit, missing security id, timeout, etc.). That is different
    from a screener compute failure, where candles existed but strategy math
    failed for that symbol. SCAN-003 treats either kind as PARTIAL.
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


def _fake_universe() -> pd.DataFrame:
    """A small offline universe shared by the integration tests."""
    return pd.DataFrame(
        {
            "symbol": ["RELIANCE", "TCS", "INFY"],
            "company_name": ["Reliance Industries", "Tata Consultancy", "Infosys"],
        }
    )


def test_full_scan_run_persists_results_and_history_can_be_queried(
    integration_engine,
    integration_session_factory,
):
    """Prove legacy evidence survives canonical enrichment in real SQLite.

    Unlike a mocked repository test, this exercises the complete service and
    storage path. The old ``provenance`` object remains in the raw audit row,
    while the dedicated provenance column gains stable contract keys.
    """
    fake_universe = _fake_universe()
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

    # The service must not mutate the caller's params (app.py reuses this dict to
    # render charts after a scan); the compute callback run_scan injects must stay
    # inside the copy it makes, never leaking back to the caller.
    assert "compute_failure_callback" not in scan_params
    assert callable(scan_params["progress_callback"])

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
            "screener_key": "fake_integration_screener",
            "screener_version": None,
            "triggered_rules": ["fake_breakout", "volume_confirmation"],
            "indicator_values": {},
            "params_snapshot": {
                "start_date": "2026-01-01",
                "end_date": "2026-06-02",
                "min_score": "80.5",
            },
            "data_snapshot_date": "2026-06-02",
            "source": None,
            "notes": None,
            "ai": None,
        }

        # ``raw_result_json`` is the complete normalized screener row, so the
        # author's original receipt remains visible exactly where legacy
        # readers expect it. The added canonical envelope is stored alongside
        # it and extracted into the dedicated provenance column.
        assert rows[0].raw_result_json["provenance"] == {
            "rules": ["fake_breakout", "volume_confirmation"],
            "observed_at": "2026-06-01",
        }
        assert rows[0].raw_result_json["provenance_json"] == rows[0].provenance_json
        assert rows[1].rating == "WATCH"
        # Row 2 supplied signal_date as the string "2026-06-01"; confirm it parsed.
        assert rows[1].signal_date == date(2026, 6, 1)
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

    Beginner note:
    Partial is not "bad enough to throw everything away." It means the scan
    still produced useful shortlist rows, but operators need a durable warning
    that some symbols were skipped or failed. That durable warning lives on the
    parent ``scan_runs`` row, while the usable shortlist still lives in
    ``scan_results``.
    """
    fake_universe = _fake_universe()
    fake_loader = _FakeDataLoader(
        last_failures=[
            {
                "symbol": "INFY",
                "security_id": "1594",
                "message": "offline fixture load failure",
            }
        ]
    )
    # Keep params minimal here. The integration point we care about is not a
    # screener's strategy knobs; it is that SCAN-003 persists the end date as the
    # data snapshot and stores the final PARTIAL status.
    scan_params = {
        "start_date": date(2026, 1, 1),
        "end_date": date(2026, 6, 2),
    }

    def partial_screener(_universe_df, _data_loader, params):
        """Return one usable row while reporting one per-symbol compute failure.

        ``run_scan`` injects ``compute_failure_callback`` into the params copy it
        passes to screeners. A real BaseScanner calls this callback when one
        symbol's indicator math fails but the wider scan can continue.
        """
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
        # Re-open a new Session on purpose. If the service only changed in-memory
        # objects and forgot to commit, this independent history query would see
        # nothing.
        runs = get_latest_scan_runs(session, limit=5)
        assert len(runs) == 1

        saved_run = runs[0]
        assert saved_run.id == result.run_id
        assert saved_run.status is ScanStatus.PARTIAL
        assert saved_run.screener_key == "fake_partial_screener"
        assert saved_run.triggered_by == "job:daily_scan"
        assert saved_run.finished_at is not None
        assert saved_run.error_message == result.error_message

        # Even though the parent run is PARTIAL, the good row should still be
        # queryable. This is the behavior a future history page needs: show the
        # user both the warning and the usable shortlist.
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

    Beginner note:
    A FAILED run is different from PARTIAL. Here the screener raises before it
    can return a valid result table, so there should be a parent audit row but no
    child ``scan_results`` rows.
    """
    fake_universe = _fake_universe()
    fake_loader = _FakeDataLoader()

    def failed_screener(_universe_df, _data_loader, _params):
        """Raise before producing rows, like a fatal screener bug would.

        The raw message intentionally looks like it contains a credential. The
        service should preserve the safe exception type (RuntimeError) and drop
        the unsafe raw text from returned/persisted messages.
        """
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
    # The service result may be shown directly to a caller, so check it before
    # checking the database copy.
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
        # The persisted message is the one a future scan-history UI will render.
        # Keeping this assertion at the database boundary catches accidental raw
        # exception storage, not just accidental raw exception return values.
        assert "RuntimeError" in (saved_run.error_message or "")
        assert "SUPERSECRET" not in (saved_run.error_message or "")
        assert get_scan_results(session, saved_run.id) == []


def test_scan_history_lists_multiple_runs(
    integration_engine,
    integration_session_factory,
):
    """Two scans should both persist and be queryable as scan history.

    The single-run tests above prove one run is queryable. This proves the
    history *list* a future SCAN-004 page will render accumulates multiple runs
    through the real service, not just one.

    The assertions are deliberately order-independent: ``get_latest_scan_runs``
    sorts by ``started_at`` only, so two runs created microseconds apart can share
    a timestamp and come back in either order. Asserting a strict newest-first
    order here would be flaky; proving both runs are present is the durable
    integration guarantee.
    """

    def _empty_screener(_universe_df, _data_loader, _params):
        """A clean screener with an empty shortlist (SUCCESS, no result rows)."""
        return pd.DataFrame()

    common = dict(
        universe_key="fake_universe",
        run_callable=_empty_screener,
        universe_df=_fake_universe(),
        params={"start_date": date(2026, 1, 1), "end_date": date(2026, 6, 2)},
        session_factory=integration_session_factory,
    )
    first = run_scan(
        screener_key="first_screener",
        data_loader=_FakeDataLoader(),
        **common,
    )
    second = run_scan(
        screener_key="second_screener",
        data_loader=_FakeDataLoader(),
        **common,
    )

    assert first.status is ScanStatus.SUCCESS
    assert second.status is ScanStatus.SUCCESS
    assert first.run_id != second.run_id

    with Session(integration_engine) as session:
        runs = get_latest_scan_runs(session, limit=5)
        assert {run.id for run in runs} == {first.run_id, second.run_id}
        assert {run.screener_key for run in runs} == {
            "first_screener",
            "second_screener",
        }
