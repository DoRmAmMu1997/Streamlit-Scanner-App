"""Add retry scheduling state for forward-return validation.

Revision ID: 20260909valid005
Revises: 20260906obs004a
Create Date: 2026-09-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260909valid005"
down_revision = "20260906obs004a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add attempt timing and benchmark-only retry state.

    Beginner note:
    Existing rows already contain the best available attempt evidence:
    ``computed_at`` for terminal calculations and ``created_at`` for pending
    rows. The backfill preserves that history. A legacy computed stock row with
    no benchmark return enters the retry queue once; the service later clears it
    when its universe intentionally has no configured benchmark.
    """
    op.add_column(
        "signal_forward_returns",
        sa.Column("last_attempted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "signal_forward_returns",
        sa.Column(
            "benchmark_retry_pending",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            "UPDATE signal_forward_returns "
            "SET last_attempted_at = COALESCE(computed_at, created_at)"
        )
    )
    op.execute(
        sa.text(
            "UPDATE signal_forward_returns "
            "SET benchmark_retry_pending = true "
            "WHERE status = 'computed' AND benchmark_return_pct IS NULL"
        )
    )


def downgrade() -> None:
    """Remove retry metadata while preserving all measurement facts."""
    with op.batch_alter_table("signal_forward_returns") as batch_op:
        batch_op.drop_column("benchmark_retry_pending")
        batch_op.drop_column("last_attempted_at")
