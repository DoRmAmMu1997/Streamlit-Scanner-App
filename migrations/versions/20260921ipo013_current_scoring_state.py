"""Separate current verification from immutable IPO scoring history.

Revision ID: 20260921ipo013
Revises: 20260909valid005
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260921ipo013"
down_revision = "20260909valid005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Backfill display pointers without granting legacy receipts freshness.

    Beginner note:
        Joining the recommendation excludes orphan scores. Equal calculation
        times use the greatest id as a deterministic tie break. No historical
        receipt or timestamp is rewritten; verification remains explicitly NULL.
    """
    op.create_table(
        "ipo_scoring_state",
        sa.Column("issue_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  sa.ForeignKey("ipo_issues.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("input_revision", sa.Integer(), server_default="0", nullable=False),
        sa.Column("current_score_id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"),
                  sa.ForeignKey("ipo_scores.id", ondelete="SET NULL"), nullable=True),
        sa.Column("evaluated_revision", sa.Integer(), nullable=True),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("input_revision >= 0", name="ck_ipo_scoring_state_input_revision"),
        sa.CheckConstraint("evaluated_revision >= 0 AND evaluated_revision <= input_revision",
                           name="ck_ipo_scoring_state_evaluated_revision"),
    )
    op.execute(sa.text(
        "INSERT INTO ipo_scoring_state (issue_id, input_revision, current_score_id) "
        "SELECT i.id, 0, (SELECT s.id FROM ipo_scores s "
        "JOIN ipo_recommendations r ON r.score_id = s.id WHERE s.issue_id = i.id "
        "ORDER BY s.scored_at DESC, s.id DESC LIMIT 1) FROM ipo_issues i"
    ))


def downgrade() -> None:
    """Remove mutable selection only; every historical pair is retained."""
    op.drop_table("ipo_scoring_state")
