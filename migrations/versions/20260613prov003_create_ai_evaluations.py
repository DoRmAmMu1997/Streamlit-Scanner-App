"""Create durable AI evaluation receipts for PROV-003.

Beginner note:
This migration adds the ``ai_evaluations`` table — an append-only *ledger* of
every AI verdict attempt (approved, rejected, or error) tied to a scan run. It
exists so an AI decision can be audited months later: which model and prompt
version ran, the confidence, and a trusted receipt — without storing any raw
scraped text or raw model response. ``scan_results`` stays shortlist-only, so an
approved decision appears in both tables while rejected/error ones remain
auditable here without being shown as signals. The ``outcome`` CHECK constraint
and the FK ``ON DELETE CASCADE`` keep the ledger consistent with its parent run.

Revision ID: 20260613prov003
Revises: 20260610scan004
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260613prov003"
down_revision = "20260610scan004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_evaluations",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("run_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("symbol", sa.String(length=50), nullable=False),
        sa.Column("signal_date", sa.Date(), nullable=True),
        sa.Column("outcome", sa.String(length=16), nullable=False),
        sa.Column("verdict_label", sa.String(length=50), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=8, scale=4), nullable=True),
        sa.Column("model_name", sa.String(length=100), nullable=False),
        sa.Column("prompt_version", sa.String(length=100), nullable=False),
        sa.Column("validated_verdict_json", sa.JSON(), nullable=False),
        sa.Column("provenance_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "outcome IN ('approved', 'rejected', 'error')",
            name="ck_ai_evaluations_outcome",
        ),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["scan_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ai_evaluations_outcome",
        "ai_evaluations",
        ["outcome"],
        unique=False,
    )
    op.create_index(
        "ix_ai_evaluations_run_id",
        "ai_evaluations",
        ["run_id"],
        unique=False,
    )
    op.create_index(
        "ix_ai_evaluations_symbol",
        "ai_evaluations",
        ["symbol"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_ai_evaluations_symbol", table_name="ai_evaluations")
    op.drop_index("ix_ai_evaluations_run_id", table_name="ai_evaluations")
    op.drop_index("ix_ai_evaluations_outcome", table_name="ai_evaluations")
    op.drop_table("ai_evaluations")
