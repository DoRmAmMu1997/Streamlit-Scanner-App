"""Add scan-run candle data-quality receipts for DATA-001.

Adds a single nullable ``data_quality_json`` column to ``scan_runs`` that holds
the per-run candle-quality receipt (see ``backend/scanning/service.py``).

Nullable on purpose: existing rows and any non-scan bootstrap have no receipt, so
they simply stay NULL rather than needing a synthetic empty object. This is the
fourth migration, chained after ``20260613prov003`` (see ``down_revision``); the
drift test in ``tests/test_scan_storage_migrations.py`` keeps it in sync with the
ORM model.

Revision ID: 20260616data001
Revises: 20260613prov003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic reads these module globals to order migrations: ``revision`` is this
# script's id and ``down_revision`` is the one it must run after.
revision = "20260616data001"
down_revision = "20260613prov003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Add the nullable receipt column. ``sa.JSON`` maps to JSON on Postgres and a
    # JSON-encoded TEXT on SQLite, so the same code works in tests and production.
    op.add_column(
        "scan_runs",
        sa.Column("data_quality_json", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    # The exact inverse, so ``alembic downgrade`` leaves a clean schema.
    op.drop_column("scan_runs", "data_quality_json")
