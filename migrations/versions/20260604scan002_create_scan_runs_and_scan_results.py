"""Create scan_runs and scan_results.

This is the first real persistence migration for SCAN-002. It turns the
SQLAlchemy ORM models from SCAN-001 into physical tables. Keep this migration
boring and explicit: initial schema migrations are much easier to review when
every column and index is visible in one place.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260604scan002"
down_revision = None
branch_labels = None
depends_on = None


def _big_int_primary_key() -> sa.BigInteger:
    """Return the same primary-key type shape used by the ORM models.

    SQLite only auto-increments a column that is literally INTEGER PRIMARY KEY.
    Postgres can use BIGINT. The SQLAlchemy variant gives each database the
    shape it expects while keeping one migration.
    """
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the scan-history tables and indexes."""
    big_int_primary_key = _big_int_primary_key()
    # Store enum values as a VARCHAR plus a CHECK constraint, not as a native
    # Postgres enum. That keeps SQLite and Postgres behavior aligned and makes a
    # future "add status value" migration straightforward.
    scan_status = sa.Enum(
        "running",
        "success",
        "partial",
        "failed",
        name="scan_status",
        native_enum=False,
        create_constraint=True,
        length=16,
    )

    # scan_runs is the audit header: one row per attempt to run a screener over a
    # universe. It stores the reproducibility facts before we get to per-symbol
    # results.
    op.create_table(
        "scan_runs",
        sa.Column("id", big_int_primary_key, nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", scan_status, nullable=False),
        sa.Column("screener_key", sa.String(length=100), nullable=False),
        sa.Column("universe_key", sa.String(length=100), nullable=False),
        sa.Column("params_json", sa.JSON(), nullable=True),
        sa.Column("data_snapshot_date", sa.Date(), nullable=True),
        sa.Column("app_version", sa.String(length=50), nullable=True),
        sa.Column("git_commit_sha", sa.String(length=40), nullable=True),
        sa.Column("triggered_by", sa.String(length=100), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_runs_screener_key", "scan_runs", ["screener_key"])
    op.create_index("ix_scan_runs_status", "scan_runs", ["status"])
    op.create_index("ix_scan_runs_universe_key", "scan_runs", ["universe_key"])

    # scan_results is the child table: one row per shortlisted stock produced by
    # the parent run. The raw/provenance JSON columns let deterministic and AI
    # screeners share this same table without losing their extra fields.
    op.create_table(
        "scan_results",
        sa.Column("id", big_int_primary_key, nullable=False),
        sa.Column("run_id", big_int_primary_key, nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=True),
        sa.Column("close_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("rating", sa.String(length=20), nullable=True),
        sa.Column("final_score", sa.Numeric(6, 2), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("raw_result_json", sa.JSON(), nullable=True),
        sa.Column("provenance_json", sa.JSON(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["scan_runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_scan_results_run_id", "scan_results", ["run_id"])
    op.create_index("ix_scan_results_symbol", "scan_results", ["symbol"])


def downgrade() -> None:
    """Drop child objects before parent objects so foreign keys do not block us."""
    op.drop_index("ix_scan_results_symbol", table_name="scan_results")
    op.drop_index("ix_scan_results_run_id", table_name="scan_results")
    op.drop_table("scan_results")
    op.drop_index("ix_scan_runs_universe_key", table_name="scan_runs")
    op.drop_index("ix_scan_runs_status", table_name="scan_runs")
    op.drop_index("ix_scan_runs_screener_key", table_name="scan_runs")
    op.drop_table("scan_runs")
