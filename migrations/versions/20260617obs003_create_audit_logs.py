"""Create the OBS-003 audit log and runtime-config tables.

Beginner note:
This migration adds two tables:

- ``audit_logs`` — an append-only record of important user actions (logins,
  manual scans, config changes, CSV exports, admin-page access). Each row stores
  the actor's email, a UTC timestamp, and a small JSON metadata blob that the
  recorder has already passed through the app's secret redactor. System actions
  that run before sign-in (the startup data refresh) store ``user_email = NULL``.
- ``app_config`` — a tiny key/value store of runtime setting overrides for the
  admin config form (currently ``LOG_LEVEL``/``LOG_FORMAT``). The key is the env
  var name and is the primary key, so there is one override row per setting.

Both are additive, so existing rows in other tables are untouched. Keep the
style boring and explicit, like the SCAN-002/PROV-003 migrations.

Revision ID: 20260617obs003
Revises: 20260616data001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260617obs003"
down_revision = "20260616data001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create ``audit_logs`` and ``app_config`` with their indexes."""
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.BigInteger().with_variant(sa.Integer(), "sqlite"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("event", sa.String(length=50), nullable=False),
        sa.Column("user_email", sa.String(length=320), nullable=True),
        sa.Column("metadata_json", sa.JSON(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_audit_logs_created_at",
        "audit_logs",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_event",
        "audit_logs",
        ["event"],
        unique=False,
    )
    op.create_index(
        "ix_audit_logs_user_email",
        "audit_logs",
        ["user_email"],
        unique=False,
    )

    op.create_table(
        "app_config",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_by", sa.String(length=320), nullable=True),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Drop the OBS-003 tables again (newest objects first)."""
    op.drop_table("app_config")
    op.drop_index("ix_audit_logs_user_email", table_name="audit_logs")
    op.drop_index("ix_audit_logs_event", table_name="audit_logs")
    op.drop_index("ix_audit_logs_created_at", table_name="audit_logs")
    op.drop_table("audit_logs")
