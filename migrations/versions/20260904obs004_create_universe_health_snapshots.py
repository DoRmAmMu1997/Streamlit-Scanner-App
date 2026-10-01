"""Create the universe mapping-health baseline table for OBS-004.

Adds ``universe_health_snapshots``: one row per universe per health check (see
``backend/data_quality/universe_health.py`` and ``backend/jobs/run_daily_scan.py``).

Why a table and not a file: the check has to answer *"is this worse than last
time?"*, which needs a previous number to compare against. The Render daily-scan
cron deliberately runs on an **ephemeral filesystem with no disk**, so anything
written next to the universe CSVs is gone before the next run. The shared
Postgres is the only state that survives between runs.

Why append-only rather than one upserted row per universe: the question that
always follows "GUJGASLTD dropped out" is "when?". Keeping the history answers it
for free, and the read path only ever wants the newest row per universe - which
``ix_universe_health_snapshots_key_captured`` serves directly.

``unmapped_symbols_json`` is nullable so a universe whose CSV could not be read
still leaves its header row behind as evidence. The drift test in
``tests/test_scan_storage_migrations.py`` keeps this in sync with the ORM model.

Revision ID: 20260904obs004
Revises: 20260820ipo011
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic reads these module globals to order migrations: ``revision`` is this
# script's id and ``down_revision`` is the one it must run after.
revision = "20260904obs004"
down_revision = "20260820ipo011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "universe_health_snapshots",
        # BigInteger with a SQLite Integer variant, matching every other
        # surrogate key in this schema so autoincrement behaves the same on both.
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("universe_key", sa.String(length=100), nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("mapped_rows", sa.Integer(), nullable=False),
        sa.Column("unmapped_rows", sa.Integer(), nullable=False),
        # sa.JSON maps to JSON on Postgres and JSON-encoded TEXT on SQLite, so the
        # same code path serves tests and production.
        sa.Column("unmapped_symbols_json", sa.JSON(), nullable=True),
    )
    # Single-column indexes mirror the ORM's ``index=True`` columns.
    op.create_index(
        "ix_universe_health_snapshots_captured_at",
        "universe_health_snapshots",
        ["captured_at"],
    )
    op.create_index(
        "ix_universe_health_snapshots_universe_key",
        "universe_health_snapshots",
        ["universe_key"],
    )
    # The composite index the comparison actually uses: newest row per universe.
    op.create_index(
        "ix_universe_health_snapshots_key_captured",
        "universe_health_snapshots",
        ["universe_key", "captured_at"],
    )


def downgrade() -> None:
    """Drop the indexes before the table so the schema unwinds cleanly."""
    op.drop_index(
        "ix_universe_health_snapshots_key_captured",
        table_name="universe_health_snapshots",
    )
    op.drop_index(
        "ix_universe_health_snapshots_universe_key",
        table_name="universe_health_snapshots",
    )
    op.drop_index(
        "ix_universe_health_snapshots_captured_at",
        table_name="universe_health_snapshots",
    )
    op.drop_table("universe_health_snapshots")
