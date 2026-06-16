"""Add scan-run candle data-quality receipts for DATA-001.

Revision ID: 20260616data001
Revises: 20260613prov003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260616data001"
down_revision = "20260613prov003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "scan_runs",
        sa.Column("data_quality_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("scan_runs", "data_quality_json")
