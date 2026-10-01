"""Harden OBS-004 universe-health baseline validity.

Revision ID: 20260906obs004a
Revises: 20260904obs004
Create Date: 2026-09-06

Existing rows predate single-read collection and explicit read-failure states.
They remain useful history, but cannot prove they are safe comparison baselines,
so this migration labels them ``legacy_unknown``. New ORM writes explicitly use
``valid``, ``missing``, or ``unreadable``.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "20260906obs004a"
down_revision = "20260904obs004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add observation authority and an index serving latest-valid lookup.

    Beginner note:
    The temporary server default is intentional: it backfills every existing row
    as ``legacy_unknown`` while the column is added. We then change only the
    default for future direct inserts to ``valid``. The old rows keep their
    conservative label and therefore cannot replace a known-good baseline.
    """
    op.add_column(
        "universe_health_snapshots",
        sa.Column(
            "observation_status",
            sa.String(length=32),
            nullable=False,
            server_default="legacy_unknown",
        ),
    )
    with op.batch_alter_table("universe_health_snapshots") as batch_op:
        batch_op.alter_column(
            "observation_status",
            existing_type=sa.String(length=32),
            nullable=False,
            server_default="valid",
        )

    op.drop_index(
        "ix_universe_health_snapshots_key_captured",
        table_name="universe_health_snapshots",
    )
    op.create_index(
        "ix_universe_health_snapshots_key_status_captured_id",
        "universe_health_snapshots",
        ["universe_key", "observation_status", "captured_at", "id"],
    )


def downgrade() -> None:
    """Restore the original OBS-004 shape without deleting snapshot rows."""
    op.drop_index(
        "ix_universe_health_snapshots_key_status_captured_id",
        table_name="universe_health_snapshots",
    )
    op.create_index(
        "ix_universe_health_snapshots_key_captured",
        "universe_health_snapshots",
        ["universe_key", "captured_at"],
    )
    with op.batch_alter_table("universe_health_snapshots") as batch_op:
        batch_op.drop_column("observation_status")
