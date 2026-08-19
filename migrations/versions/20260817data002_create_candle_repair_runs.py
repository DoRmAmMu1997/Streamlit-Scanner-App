"""Create the candle-cache repair audit table for DATA-002.

Adds ``candle_repair_runs``: one row per repair pass over the daily candle cache
(see ``backend/data_quality/cache_repair.py`` and
``backend/jobs/repair_candle_cache.py``).

Why a new table rather than another column on ``scan_runs``: a repair pass is not
a scan. It runs during the ``python app.py`` prefetch, before any screener
executes, and it has its own counts (symbols repaired, rows removed, re-downloads
spent). Hanging it off a scan row would mean inventing a synthetic scan for every
morning's cleanup.

``receipt_json`` is nullable so a pass that dies mid-run still leaves its header
row — a run with ``finished_at IS NULL`` is itself useful evidence. The drift test
in ``tests/test_scan_storage_migrations.py`` keeps this in sync with the ORM model.

Revision ID: 20260817data002
Revises: 20260718ipo010
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# Alembic reads these module globals to order migrations: ``revision`` is this
# script's id and ``down_revision`` is the one it must run after.
revision = "20260817data002"
down_revision = "20260718ipo010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "candle_repair_runs",
        # BigInteger with a SQLite Integer variant, matching every other
        # surrogate key in this schema so autoincrement behaves the same on both.
        sa.Column(
            "id",
            sa.BigInteger().with_variant(sa.Integer, "sqlite"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("trigger", sa.String(length=32), nullable=False),
        sa.Column("symbols_checked", sa.Integer(), nullable=False),
        sa.Column("symbols_repaired", sa.Integer(), nullable=False),
        sa.Column("symbols_unrepairable", sa.Integer(), nullable=False),
        sa.Column("rows_removed", sa.Integer(), nullable=False),
        sa.Column("refetch_count", sa.Integer(), nullable=False),
        # sa.JSON maps to JSON on Postgres and JSON-encoded TEXT on SQLite, so the
        # same code path serves tests and production.
        sa.Column("receipt_json", sa.JSON(), nullable=True),
    )
    # Every reader ("what did the last repair do?") sorts newest-first.
    op.create_index(
        "ix_candle_repair_runs_started_at",
        "candle_repair_runs",
        ["started_at"],
    )


def downgrade() -> None:
    # The exact inverse, so ``alembic downgrade`` leaves a clean schema.
    op.drop_index("ix_candle_repair_runs_started_at", table_name="candle_repair_runs")
    op.drop_table("candle_repair_runs")
