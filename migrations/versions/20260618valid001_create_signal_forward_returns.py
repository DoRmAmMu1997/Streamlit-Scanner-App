"""Create signal_forward_returns for VALID-001 forward-return validation.

This turns the VALID-001 ORM schema (``SignalForwardReturn`` in
``backend/storage/models.py``) into a physical table, and adds the
``(symbol, signal_date)`` composite index on ``scan_results`` that SCAN-001
deliberately deferred until the forward-return work needed it.

Like the SCAN-002 initial migration, keep this boring and explicit: every column
and index is visible here so the migration is reviewable on its own, and so the
``test_migration_matches_orm_metadata`` drift guard stays green against the models.
The forward-return *math* that fills these rows is VALID-002 (owner: Codex).
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260618valid001"
down_revision = "20260617obs003"
branch_labels = None
depends_on = None


def _big_int_primary_key() -> sa.BigInteger:
    """The same id type the ORM uses: BIGINT on Postgres, INTEGER on SQLite."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create signal_forward_returns and the deferred scan_results date index."""
    big_int_primary_key = _big_int_primary_key()
    # Stored as VARCHAR + CHECK (not a native Postgres enum), mirroring scan_status,
    # so adding a future status value is a normal migration rather than an ALTER TYPE.
    forward_return_status = sa.Enum(
        "pending",
        "computed",
        "insufficient_data",
        name="forward_return_status",
        native_enum=False,
        create_constraint=True,
        length=20,
    )

    # One row per (signal, horizon): a ScanResult fans out to 20/60/120-day rows.
    op.create_table(
        "signal_forward_returns",
        sa.Column("id", big_int_primary_key, nullable=False),
        sa.Column("result_id", big_int_primary_key, nullable=False),
        sa.Column("horizon_days", sa.Integer(), nullable=False),
        sa.Column("status", forward_return_status, nullable=False),
        sa.Column("entry_date", sa.Date(), nullable=True),
        sa.Column("exit_date", sa.Date(), nullable=True),
        sa.Column("entry_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("exit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("forward_return_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("benchmark_key", sa.String(length=50), nullable=True),
        sa.Column("benchmark_entry_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("benchmark_exit_price", sa.Numeric(18, 4), nullable=True),
        sa.Column("benchmark_return_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("excess_return_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("max_adverse_excursion_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("max_favorable_excursion_pct", sa.Numeric(9, 4), nullable=True),
        sa.Column("computed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["result_id"], ["scan_results.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One measurement per signal per horizon — the idempotent-upsert key. Leading
        # with result_id, this also serves "all horizons for this signal", so no
        # separate result_id index is created (it would be redundant).
        sa.UniqueConstraint(
            "result_id", "horizon_days", name="uq_forward_return_result_horizon"
        ),
    )
    # The calculator's hot path is "give me the pending rows".
    op.create_index(
        "ix_signal_forward_returns_status", "signal_forward_returns", ["status"]
    )

    # SCAN-001 §4.6 parked this composite index "for VALID-*, when queries that need
    # it actually exist". The forward-return lookup ("signals for symbol S on/after
    # date D") is that query.
    op.create_index(
        "ix_scan_results_symbol_signal_date",
        "scan_results",
        ["symbol", "signal_date"],
    )


def downgrade() -> None:
    """Drop child objects before parent objects so foreign keys do not block us."""
    op.drop_index("ix_scan_results_symbol_signal_date", table_name="scan_results")
    op.drop_index(
        "ix_signal_forward_returns_status", table_name="signal_forward_returns"
    )
    op.drop_table("signal_forward_returns")
