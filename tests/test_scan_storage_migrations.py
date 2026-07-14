"""Tests for the SCAN-002 Alembic migration setup.

These tests use a temporary SQLite file instead of the real ``data/scanner.db``.
That proves migrations are safe to run in CI and on a developer laptop without
depending on any local scan history.

Beginner note:
A model change is not complete until an old database can reach the new shape and,
when safe, return to the old one. These tests compare Alembic's hand-written schema
with SQLAlchemy metadata so local SQLite and production PostgreSQL do not drift.
"""

from __future__ import annotations

import datetime as dt
import logging
import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import Session

from backend.storage import database
from backend.storage.models import Base, IpoIssue, IpoManualExtraction, IpoScore


def test_alembic_cli_does_not_echo_percent_encoded_database_password():
    """Alembic errors must not print credentials from a URL-encoded password.

    Beginner note: Alembic stores configuration with ConfigParser, where a
    percent sign has special interpolation syntax. A normal URL escape such as
    ``%40`` must be escaped for that configuration layer. Otherwise Alembic
    raises before connecting and its traceback includes the complete database
    URL, including the password.
    """
    secret = "dummy%40secret"
    env = os.environ.copy()
    env["DATABASE_URL"] = (
        f"postgresql+psycopg://scanner:{secret}@127.0.0.1:1/scanner"
        "?connect_timeout=1"
    )

    completed = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    output = completed.stdout + completed.stderr

    assert completed.returncode != 0
    assert secret not in output
    assert "dummy@secret" not in output


def test_alembic_upgrade_and_downgrade_use_temp_sqlite(monkeypatch, tmp_path: Path):
    """Upgrade creates the expected schema; downgrade removes it again.

    Beginner note:
        This broad smoke test is also the central table-name inventory. Adding the
        IPO-005 columns must not accidentally add, remove, or rename a table elsewhere
        in the shared metadata graph.
    """
    db_path = tmp_path / "scan-history.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")

    # Regression guard: Alembic's logging setup used to disable already-created
    # app loggers. That broke later tests that capture warnings from scanner
    # code, so this assertion protects the whole suite from order dependence.
    app_logger = logging.getLogger("backend.scanner_base")
    app_logger.disabled = False

    config = Config("alembic.ini")
    command.upgrade(config, "head")

    assert app_logger.disabled is False

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    inspector = inspect(engine)

    # Table and index checks keep the hand-written migration aligned with
    # backend/storage/models.py and the SCAN-001 / VALID-001 design docs.
    assert set(inspector.get_table_names()) == {
        "ai_evaluations",
        "alembic_version",
        "app_config",
        "audit_logs",
        "ipo_documents",
        "ipo_enrichment_signals",
        "ipo_extraction_proposals",
        "ipo_financials",
        "ipo_issues",
        "ipo_manual_extractions",
        "ipo_manual_financial_periods",
        "ipo_manual_peer_valuations",
        "ipo_recommendations",
        "ipo_scores",
        "ipo_subscriptions",
        "scan_runs",
        "scan_results",
        "signal_forward_returns",
        "user_roles",
    }
    assert {index["name"] for index in inspector.get_indexes("audit_logs")} >= {
        "ix_audit_logs_created_at",
        "ix_audit_logs_event",
        "ix_audit_logs_user_email",
    }
    assert {index["name"] for index in inspector.get_indexes("scan_runs")} >= {
        "ix_scan_runs_screener_key",
        "ix_scan_runs_status",
        "ix_scan_runs_universe_key",
    }
    assert "data_quality_json" in {
        column["name"] for column in inspector.get_columns("scan_runs")
    }
    assert {index["name"] for index in inspector.get_indexes("scan_results")} >= {
        "ix_scan_results_run_id",
        "ix_scan_results_symbol",
        "ix_scan_results_symbol_signal_date",  # VALID-001 forward-return lookup
    }
    assert {index["name"] for index in inspector.get_indexes("ai_evaluations")} >= {
        "ix_ai_evaluations_outcome",
        "ix_ai_evaluations_run_id",
        "ix_ai_evaluations_symbol",
    }
    foreign_keys = inspector.get_foreign_keys("scan_results")
    # The cascade option is important: deleting a run must not leave orphaned
    # result rows behind.
    assert foreign_keys[0]["referred_table"] == "scan_runs"
    assert foreign_keys[0]["options"] == {"ondelete": "CASCADE"}
    ai_foreign_keys = inspector.get_foreign_keys("ai_evaluations")
    assert ai_foreign_keys[0]["referred_table"] == "scan_runs"
    assert ai_foreign_keys[0]["options"] == {"ondelete": "CASCADE"}

    # VALID-001: forward-return rows hang off scan_results with the same cascade,
    # and the calculator's "pending rows" lookup is indexed.
    assert {
        index["name"] for index in inspector.get_indexes("signal_forward_returns")
    } >= {"ix_signal_forward_returns_status"}
    forward_keys = inspector.get_foreign_keys("signal_forward_returns")
    assert forward_keys[0]["referred_table"] == "scan_results"
    assert forward_keys[0]["options"] == {"ondelete": "CASCADE"}

    # IPO-001: every source/evaluation table is reachable from the issue root,
    # while a recommendation is paired one-to-one with its immutable score.
    assert {index["name"] for index in inspector.get_indexes("ipo_issues")} >= {
        "ix_ipo_issues_company_name",
        "ix_ipo_issues_status",
        "ix_ipo_issues_status_open_date",
        "ux_ipo_issues_sebi_company_key",
    }
    issue_columns = {column["name"] for column in inspector.get_columns("ipo_issues")}
    assert "sebi_company_key" in issue_columns
    document_columns = {column["name"] for column in inspector.get_columns("ipo_documents")}
    assert {
        "filing_date",
        "record_hash",
        "content_sha256",
        "downloaded_at",
        "file_path",
        "page_count",
        "parse_status",
    } <= document_columns
    assert {index["name"] for index in inspector.get_indexes("ipo_documents")} >= {
        "ix_ipo_documents_filing_date",
        "ux_ipo_documents_record_hash",
    }
    assert {index["name"] for index in inspector.get_indexes("ipo_scores")} >= {
        "ix_ipo_scores_issue_id",
        "ix_ipo_scores_issue_scored_at",
    }
    recommendation_indexes = {
        index["name"]: index for index in inspector.get_indexes("ipo_recommendations")
    }
    assert recommendation_indexes["ix_ipo_recommendations_score_id"]["unique"] == 1
    for table in ("ipo_documents", "ipo_financials", "ipo_subscriptions", "ipo_scores"):
        issue_fk = next(
            fk for fk in inspector.get_foreign_keys(table) if fk["referred_table"] == "ipo_issues"
        )
        assert issue_fk["options"] == {"ondelete": "CASCADE"}
    recommendation_fk = inspector.get_foreign_keys("ipo_recommendations")[0]
    assert recommendation_fk["referred_table"] == "ipo_scores"
    assert recommendation_fk["options"] == {"ondelete": "CASCADE"}
    assert {index["name"] for index in inspector.get_indexes("ipo_manual_extractions")} >= {
        "ix_ipo_manual_extractions_issue_submitted",
        "ix_ipo_manual_extractions_source_document_id",
    }
    extraction_columns = {
        column["name"] for column in inspector.get_columns("ipo_manual_extractions")
    }
    assert {
        "total_assets",
        "total_assets_page",
        "current_liabilities",
        "current_liabilities_page",
        "post_issue_equity_shares",
        "post_issue_equity_shares_page",
    } <= extraction_columns
    period_columns = {
        column["name"]
        for column in inspector.get_columns("ipo_manual_financial_periods")
    }
    assert {
        "profit_before_tax",
        "profit_before_tax_page",
        "finance_cost",
        "finance_cost_page",
    } <= period_columns
    extraction_fks = {
        fk["referred_table"]: fk["options"]
        for fk in inspector.get_foreign_keys("ipo_manual_extractions")
    }
    assert extraction_fks == {
        "ipo_documents": {"ondelete": "SET NULL"},
        "ipo_issues": {"ondelete": "CASCADE"},
    }
    for table in ("ipo_manual_financial_periods", "ipo_manual_peer_valuations"):
        child_fk = inspector.get_foreign_keys(table)[0]
        assert child_fk["referred_table"] == "ipo_manual_extractions"
        assert child_fk["options"] == {"ondelete": "CASCADE"}

    # IPO-006..010: the review queue and enrichment tables hang off the issue
    # root; a proposal additionally links to its document (CASCADE) and, once
    # approved, to the immutable manual revision it became (SET NULL).
    score_columns = {column["name"] for column in inspector.get_columns("ipo_scores")}
    assert "inputs_fingerprint" in score_columns
    recommendation_columns = {
        column["name"] for column in inspector.get_columns("ipo_recommendations")
    }
    assert "caution_flags_json" in recommendation_columns
    assert {
        index["name"] for index in inspector.get_indexes("ipo_extraction_proposals")
    } >= {
        "ix_ipo_extraction_proposals_issue_id",
        "ix_ipo_extraction_proposals_document_id",
        "ix_ipo_extraction_proposals_manual_extraction_id",
    }
    proposal_fks = {
        fk["referred_table"]: fk["options"]
        for fk in inspector.get_foreign_keys("ipo_extraction_proposals")
    }
    assert proposal_fks == {
        "ipo_issues": {"ondelete": "CASCADE"},
        "ipo_documents": {"ondelete": "CASCADE"},
        "ipo_manual_extractions": {"ondelete": "SET NULL"},
    }
    assert {
        index["name"] for index in inspector.get_indexes("ipo_enrichment_signals")
    } >= {
        "ix_ipo_enrichment_signals_issue_id",
        "ix_ipo_enrichment_signals_captured_at",
    }
    signal_fk = inspector.get_foreign_keys("ipo_enrichment_signals")[0]
    assert signal_fk["referred_table"] == "ipo_issues"
    assert signal_fk["options"] == {"ondelete": "CASCADE"}
    engine.dispose()

    command.downgrade(config, "base")

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    assert inspect(engine).get_table_names() == ["alembic_version"]
    engine.dispose()


def test_migration_matches_orm_metadata(monkeypatch, tmp_path: Path):
    """The hand-written migration must build the same schema as the ORM models.

    Schema A comes from ``alembic upgrade head``; schema B comes from
    ``Base.metadata.create_all``. Comparing two SQLAlchemy-built SQLite schemas
    (instead of a hardcoded expectation) keeps the guard robust against reflection
    quirks while still catching any column, index, or foreign-key drift between the
    migration and ``backend/storage/models.py``.
    """
    migrated_db = tmp_path / "migrated.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{migrated_db.as_posix()}")
    command.upgrade(Config("alembic.ini"), "head")
    migrated_schema = _reflect_schema(
        create_engine(f"sqlite:///{migrated_db.as_posix()}", future=True)
    )

    orm_engine = create_engine(f"sqlite:///{(tmp_path / 'orm.db').as_posix()}", future=True)
    Base.metadata.create_all(orm_engine)
    orm_schema = _reflect_schema(orm_engine)

    assert migrated_schema == orm_schema


def test_ipo002_downgrade_refuses_to_discard_ingested_identity(monkeypatch, tmp_path: Path):
    """Downgrade fails before DDL when IPO-002-only values would be lost."""
    db_path = tmp_path / "ipo002-downgrade.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ipo_issues "
                "(company_name, sebi_company_key, issue_type, status, source_confidence, "
                "created_at, updated_at) VALUES "
                "('Example Limited', 'example limited', 'unknown', 'drhp_filed', 'high', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="discard IPO-002 filing identities"):
        command.downgrade(config, "20260629ipo001")

    engine = create_engine(database_url, future=True)
    assert "sebi_company_key" in {
        column["name"] for column in inspect(engine).get_columns("ipo_issues")
    }
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT sebi_company_key FROM ipo_issues")) == (
            "example limited"
        )
    engine.dispose()


def test_ipo003_downgrade_refuses_to_discard_download_provenance(
    monkeypatch,
    tmp_path: Path,
) -> None:
    """Downgrade fails before DDL when verified PDF cache metadata would be lost."""
    db_path = tmp_path / "ipo003-downgrade.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    digest = "a" * 64
    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ipo_issues "
                "(company_name, issue_type, status, source_confidence, created_at, updated_at) "
                "VALUES ('Example Limited', 'mainboard', 'rhp_filed', 'high', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ipo_documents "
                "(issue_id, document_type, document_url, source_confidence, "
                "content_sha256, downloaded_at, file_path, parse_status, created_at) "
                "VALUES (1, 'rhp', 'https://www.sebi.gov.in/filings/example', 'high', "
                ":digest, CURRENT_TIMESTAMP, :path, 'pending', CURRENT_TIMESTAMP)"
            ),
            {"digest": digest, "path": f"ipo/documents/{digest}.pdf"},
        )
    engine.dispose()

    with pytest.raises(RuntimeError, match="discard IPO-003 document-cache metadata"):
        command.downgrade(config, "20260630ipo002")

    engine = create_engine(database_url, future=True)
    columns = {column["name"] for column in inspect(engine).get_columns("ipo_documents")}
    assert "content_sha256" in columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT content_sha256 FROM ipo_documents")) == digest
    engine.dispose()


def test_ipo004_downgrade_refuses_to_discard_manual_revisions(
    monkeypatch, tmp_path: Path
) -> None:
    """Downgrade must stop before deleting immutable administrator evidence."""
    db_path = tmp_path / "ipo004-downgrade.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    engine = create_engine(database_url, future=True)
    with Session(engine) as session:
        issue = IpoIssue(
            company_name="Example Limited",
            issue_type="mainboard",
            status="rhp_filed",
            source_confidence="high",
        )
        session.add(
            IpoManualExtraction(
                issue=issue,
                source_document_url="https://www.sebi.gov.in/filings/example",
                source_content_sha256="a" * 64,
                financial_amount_unit="crore_inr",
                issue_amount_unit="crore_inr",
                equity_share_unit="lakh_shares",
                net_worth=1,
                net_worth_page=1,
                total_debt=0,
                total_debt_page=1,
                cash=0,
                cash_page=1,
                cash_flow_from_operations=0,
                cash_flow_from_operations_page=1,
                equity_shares=1,
                equity_shares_page=1,
                eps=0,
                eps_page=1,
                nav_book_value=0,
                nav_book_value_page=1,
                objects_of_issue="General corporate purposes",
                objects_of_issue_page=1,
                fresh_issue_amount=0,
                fresh_issue_amount_page=1,
                ofs_amount=0,
                ofs_amount_page=1,
                promoter_holding_pre_issue=1,
                promoter_holding_pre_issue_page=1,
                promoter_holding_post_issue=1,
                promoter_holding_post_issue_page=1,
                entered_by_email="admin@example.com",
                submitted_at=dt.datetime(2026, 7, 1, tzinfo=dt.UTC),
            )
        )
        session.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="discard IPO-004 manual extraction revisions"):
        command.downgrade(config, "20260630ipo003")

    engine = create_engine(database_url, future=True)
    assert "ipo_manual_extractions" in inspect(engine).get_table_names()
    engine.dispose()


def test_ipo005_upgrade_preserves_legacy_revisions_and_downgrades_losslessly(
    monkeypatch, tmp_path: Path
) -> None:
    """An IPO-004 row receives null additions and can return to IPO-004 safely.

    Beginner note:
    Nullable migration columns are a compatibility promise, not permission for
    new partial submissions. This test creates the old shape before upgrading so
    it proves real deployed history remains readable rather than only testing a
    fresh database at the newest schema.
    """
    db_path = tmp_path / "ipo005-legacy.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "20260701ipo004")

    engine = create_engine(database_url, future=True)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ipo_issues "
                "(company_name, issue_type, status, source_confidence, created_at, updated_at) "
                "VALUES ('Legacy Limited', 'mainboard', 'rhp_filed', 'high', "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO ipo_manual_extractions "
                "(issue_id, source_document_url, source_content_sha256, "
                "financial_amount_unit, issue_amount_unit, equity_share_unit, "
                "net_worth, net_worth_page, total_debt, total_debt_page, cash, cash_page, "
                "cash_flow_from_operations, cash_flow_from_operations_page, "
                "equity_shares, equity_shares_page, eps, eps_page, nav_book_value, "
                "nav_book_value_page, objects_of_issue, objects_of_issue_page, "
                "fresh_issue_amount, fresh_issue_amount_page, ofs_amount, ofs_amount_page, "
                "promoter_holding_pre_issue, promoter_holding_pre_issue_page, "
                "promoter_holding_post_issue, promoter_holding_post_issue_page, "
                "entered_by_email, submitted_at) VALUES "
                "(1, 'https://www.sebi.gov.in/filings/legacy', :digest, "
                "'crore_inr', 'crore_inr', 'lakh_shares', "
                "1, 1, 0, 1, 0, 1, 0, 1, 1, 1, 0, 1, 0, 1, "
                "'General corporate purposes', 1, 0, 1, 0, 1, 1, 1, 1, 1, "
                "'admin@example.com', CURRENT_TIMESTAMP)"
            ),
            {"digest": "a" * 64},
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(database_url, future=True)
    with engine.connect() as connection:
        row = connection.execute(
            text(
                "SELECT total_assets, current_liabilities, post_issue_equity_shares "
                "FROM ipo_manual_extractions"
            )
        ).one()
        assert row == (None, None, None)
    engine.dispose()

    command.downgrade(config, "20260701ipo004")
    engine = create_engine(database_url, future=True)
    columns = {
        column["name"] for column in inspect(engine).get_columns("ipo_manual_extractions")
    }
    assert "total_assets" not in columns
    with engine.connect() as connection:
        assert connection.scalar(text("SELECT COUNT(*) FROM ipo_manual_extractions")) == 1
    engine.dispose()


def test_ipo005_downgrade_refuses_to_discard_ratio_inputs(
    monkeypatch, tmp_path: Path
) -> None:
    """Downgrade stops before deleting any sourced IPO-005 accounting fact.

    Beginner note:
        A reversible migration is only reversible while its new columns are empty.
        Once an administrator enters evidence, refusing the downgrade is safer than
        reporting success after silently deleting that immutable provenance.
    """
    db_path = tmp_path / "ipo005-protected.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    engine = create_engine(database_url, future=True)
    with Session(engine) as session:
        issue = IpoIssue(
            company_name="Example Limited",
            issue_type="mainboard",
            status="rhp_filed",
            source_confidence="high",
        )
        session.add(
            IpoManualExtraction(
                issue=issue,
                source_document_url="https://www.sebi.gov.in/filings/example",
                source_content_sha256="a" * 64,
                financial_amount_unit="crore_inr",
                issue_amount_unit="crore_inr",
                equity_share_unit="lakh_shares",
                net_worth=1,
                net_worth_page=1,
                total_debt=0,
                total_debt_page=1,
                cash=0,
                cash_page=1,
                cash_flow_from_operations=0,
                cash_flow_from_operations_page=1,
                equity_shares=1,
                equity_shares_page=1,
                eps=0,
                eps_page=1,
                nav_book_value=0,
                nav_book_value_page=1,
                objects_of_issue="General corporate purposes",
                objects_of_issue_page=1,
                fresh_issue_amount=0,
                fresh_issue_amount_page=1,
                ofs_amount=0,
                ofs_amount_page=1,
                promoter_holding_pre_issue=1,
                promoter_holding_pre_issue_page=1,
                promoter_holding_post_issue=1,
                promoter_holding_post_issue_page=1,
                total_assets=10,
                total_assets_page=1,
                current_liabilities=2,
                current_liabilities_page=1,
                post_issue_equity_shares=2,
                post_issue_equity_shares_page=1,
                entered_by_email="admin@example.com",
                submitted_at=dt.datetime(2026, 7, 3, tzinfo=dt.UTC),
            )
        )
        session.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="discard IPO-005 manual ratio inputs"):
        command.downgrade(config, "20260701ipo004")

    engine = create_engine(database_url, future=True)
    columns = {
        column["name"] for column in inspect(engine).get_columns("ipo_manual_extractions")
    }
    assert "total_assets" in columns
    engine.dispose()


def test_ipo006_downgrade_refuses_to_discard_screener_artifacts(
    monkeypatch, tmp_path: Path
) -> None:
    """Downgrade stops before deleting any IPO-006 evaluation provenance.

    Beginner note:
        Every evaluation written by the ipo-006 scoring service stamps an
        ``inputs_fingerprint`` on its score row, so one fingerprinted score is
        enough evidence that the new columns and tables carry real history.
        The guard must fire before any DDL runs, leaving the schema untouched.
    """
    db_path = tmp_path / "ipo006-downgrade.db"
    database_url = f"sqlite:///{db_path.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    command.upgrade(config, "head")

    engine = create_engine(database_url, future=True)
    with Session(engine) as session:
        issue = IpoIssue(
            company_name="Example Limited",
            issue_type="mainboard",
            status="rhp_filed",
            source_confidence="high",
        )
        session.add(
            IpoScore(
                issue=issue,
                total_score=50,
                contributions_json={},
                missing_data_json=[],
                reasons_json=[],
                model_version="ipo-006-v1",
                inputs_fingerprint="a" * 64,
            )
        )
        session.commit()
    engine.dispose()

    with pytest.raises(RuntimeError, match="discard IPO-006 screener artifacts"):
        command.downgrade(config, "20260703ipo005")

    engine = create_engine(database_url, future=True)
    tables = set(inspect(engine).get_table_names())
    assert {"ipo_extraction_proposals", "ipo_enrichment_signals"} <= tables
    score_columns = {
        column["name"] for column in inspect(engine).get_columns("ipo_scores")
    }
    assert "inputs_fingerprint" in score_columns
    engine.dispose()


def test_ensure_database_schema_creates_tables_and_short_circuits(monkeypatch, tmp_path: Path):
    """The runtime bootstrap applies migrations once, then skips on later calls.

    This guards the fix for the "no such table: scan_runs" startup bug: the app
    and the daily CLI call ``ensure_database_schema()`` instead of relying on a
    manual ``alembic upgrade head`` that fresh checkouts never ran.
    """
    db_path = tmp_path / "bootstrap.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    assert database.ensure_database_schema() is True

    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    assert set(inspect(engine).get_table_names()) == {
        "ai_evaluations",
        "alembic_version",
        "app_config",
        "audit_logs",
        "ipo_documents",
        "ipo_enrichment_signals",
        "ipo_extraction_proposals",
        "ipo_financials",
        "ipo_issues",
        "ipo_manual_extractions",
        "ipo_manual_financial_periods",
        "ipo_manual_peer_valuations",
        "ipo_recommendations",
        "ipo_scores",
        "ipo_subscriptions",
        "scan_runs",
        "scan_results",
        "signal_forward_returns",
        "user_roles",
    }
    engine.dispose()

    # The second call must short-circuit on the per-process flag. Pointing the
    # environment at an unusable URL proves no new migration run is attempted:
    # if one were, it would fail loudly instead of returning True.
    monkeypatch.setenv("DATABASE_URL", "driver://not-a-real-database")
    assert database.ensure_database_schema() is True


def test_ensure_database_schema_preserves_logging_configuration(monkeypatch, tmp_path: Path):
    """The bootstrap must never re-run logging setup from alembic.ini.

    ``migrations/env.py`` calls ``fileConfig`` when the alembic Config carries an
    ini file name. That would replace the root logger's handlers — silently
    discarding the SEC-002 secret-redaction filter installed by the app. The
    helper builds its Config programmatically, so existing handlers and filters
    must survive untouched.
    """
    db_path = tmp_path / "bootstrap-logging.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    root_logger = logging.getLogger()
    marker_handler = logging.NullHandler()
    marker_filter = logging.Filter("tests.schema.bootstrap.marker")
    root_logger.addHandler(marker_handler)
    root_logger.addFilter(marker_filter)
    handlers_before = list(root_logger.handlers)
    try:
        assert database.ensure_database_schema() is True
        assert root_logger.handlers == handlers_before
        assert marker_filter in root_logger.filters
    finally:
        root_logger.removeHandler(marker_handler)
        root_logger.removeFilter(marker_filter)


def test_ensure_database_schema_returns_false_when_database_is_unusable(
    monkeypatch, tmp_path: Path
):
    """Bootstrap failures degrade gracefully and stay retryable.

    Scan persistence is best-effort in the UI ("continuing without history"), so
    the bootstrap must warn-and-return rather than crash the app. The flag stays
    unset on failure so a later call can retry once the environment is fixed.
    """
    blocker = tmp_path / "blocker"
    blocker.write_text("a file where a directory is expected")
    monkeypatch.setenv(
        "DATABASE_URL",
        f"sqlite:///{(blocker / 'impossible.db').as_posix()}",
    )
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)

    assert database.ensure_database_schema() is False
    assert database._schema_ensured is False


def test_ensure_database_schema_detects_schema_drift(monkeypatch, tmp_path: Path, caplog):
    """A database stamped at HEAD but missing a table is reported as drift.

    This reproduces the real failure where ``alembic_version`` says HEAD yet
    ``audit_logs`` was never created (migration history stitched across
    branches). ``upgrade head`` is then a no-op, so ``ensure_database_schema``
    must verify the tables really exist, return ``False``, and log actionable
    guidance instead of leaving a later INSERT to fail with "no such table".
    """
    db_path = tmp_path / "drift.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{db_path.as_posix()}")
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)
    monkeypatch.setattr(database, "_schema_drift_logged", False, raising=False)

    # Build the correct schema first, then simulate drift by dropping one table
    # while the alembic_version row keeps claiming HEAD.
    assert database.ensure_database_schema() is True
    monkeypatch.setattr(database, "_schema_ensured", False, raising=False)
    engine = create_engine(f"sqlite:///{db_path.as_posix()}", future=True)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE audit_logs")
    finally:
        engine.dispose()

    with caplog.at_level(logging.ERROR, logger="backend.storage.database"):
        result = database.ensure_database_schema()

    assert result is False
    # Stays unset so a later call retries once the database is rebuilt.
    assert database._schema_ensured is False
    assert "schema drift" in caplog.text.lower()
    assert "audit_logs" in caplog.text


def _reflect_schema(engine) -> dict[str, dict[str, object]]:
    """Return a comparable {table: columns/indexes/foreign_keys} structure.

    Alembic's bookkeeping ``alembic_version`` table is skipped so a migrated
    database and a ``create_all`` database describe the same application schema.
    The engine is disposed before returning.
    """
    inspector = inspect(engine)
    schema: dict[str, dict[str, object]] = {}
    for table in inspector.get_table_names():
        if table == "alembic_version":
            continue
        columns = {
            column["name"]: (str(column["type"]), bool(column["nullable"]))
            for column in inspector.get_columns(table)
        }
        indexes = {index["name"] for index in inspector.get_indexes(table)}
        foreign_keys = {
            (
                tuple(fk["constrained_columns"]),
                fk["referred_table"],
                tuple(fk["referred_columns"]),
                fk.get("options", {}).get("ondelete"),
            )
            for fk in inspector.get_foreign_keys(table)
        }
        schema[table] = {"columns": columns, "indexes": indexes, "foreign_keys": foreign_keys}
    engine.dispose()
    return schema
