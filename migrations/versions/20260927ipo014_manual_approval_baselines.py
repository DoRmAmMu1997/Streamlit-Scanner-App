"""Add trusted manual generations without upgrading legacy proposal authority.

Revision ID: 20260927ipo014
Revises: 20260921ipo013
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260927ipo014"
down_revision = "20260921ipo013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Count committed manual writes, leaving all existing proposals unknown.

    Beginner note:
        NULL is not generation zero: timestamps cannot prove the state a model
        observed before extraction. Batch recreation retains named constraints,
        foreign keys and reflected partial indexes on SQLite.
    """
    with op.batch_alter_table("ipo_scoring_state") as batch:
        batch.add_column(sa.Column("manual_revision", sa.Integer(), nullable=False, server_default="0"))
        batch.create_check_constraint("ck_ipo_scoring_state_manual_revision", "manual_revision >= 0")
    op.execute(sa.text(
        "UPDATE ipo_scoring_state SET manual_revision = "
        "(SELECT COUNT(*) FROM ipo_manual_extractions m WHERE m.issue_id = ipo_scoring_state.issue_id)"
    ))
    with op.batch_alter_table("ipo_extraction_proposals") as batch:
        batch.add_column(sa.Column("baseline_manual_revision", sa.Integer(), nullable=True))
        batch.create_check_constraint(
            "ck_ipo_extraction_proposals_manual_baseline",
            "baseline_manual_revision IS NULL OR baseline_manual_revision >= 0",
        )


def downgrade() -> None:
    """Remove authority metadata while retaining every immutable history row."""
    with op.batch_alter_table("ipo_extraction_proposals") as batch:
        batch.drop_constraint("ck_ipo_extraction_proposals_manual_baseline", type_="check")
        batch.drop_column("baseline_manual_revision")
    with op.batch_alter_table("ipo_scoring_state") as batch:
        batch.drop_constraint("ck_ipo_scoring_state_manual_revision", type_="check")
        batch.drop_column("manual_revision")
