"""Add scan_runs.symbols_scanned for the SCAN-004 history page.

The history page shows "number of symbols scanned" per run. That fact (the
universe size handed to the screener) was never persisted before, so this
migration adds one nullable integer column. Old rows keep NULL and render as
"—" in the UI; no backfill is possible because the information was not stored.

Beginner note:
Adding a nullable column is the safest kind of schema change — existing rows
stay valid and no data is rewritten. Keep it boring and explicit, like the
initial SCAN-002 migration.
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "20260610scan004"
down_revision = "20260604scan002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable symbols_scanned column to scan_runs."""
    op.add_column(
        "scan_runs",
        sa.Column("symbols_scanned", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    """Drop symbols_scanned again.

    batch_alter_table is required for SQLite: older SQLite builds cannot
    ALTER TABLE ... DROP COLUMN directly, so Alembic rebuilds the table copy
    behind the scenes. On Postgres this degrades to a plain DROP COLUMN.
    """
    with op.batch_alter_table("scan_runs") as batch_op:
        batch_op.drop_column("symbols_scanned")
