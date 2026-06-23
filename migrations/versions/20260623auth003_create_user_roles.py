"""Create the AUTH-003 user_roles table.

Beginner note:
This migration adds one table, ``user_roles``: a durable role assignment per
user (``viewer``/``analyst``/``admin``). ``email`` is the primary key — one role
per user — mirroring how ``app_config`` keys on the setting name. The allowed
role names are pinned by a CHECK constraint so a bad write can never store an
unknown role.

The table is additive, so existing rows in other tables are untouched. Keep the
style boring and explicit, like the SCAN-002/OBS-003 migrations, so the
``test_migration_matches_orm_metadata`` drift guard stays green against
``backend/storage/models.py``.

Revision ID: 20260623auth003
Revises: 20260618valid001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260623auth003"
down_revision = "20260618valid001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Create ``user_roles`` with its role CHECK constraint."""
    op.create_table(
        "user_roles",
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("assigned_by", sa.String(length=320), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "role IN ('viewer', 'analyst', 'admin')", name="ck_user_roles_role"
        ),
        sa.PrimaryKeyConstraint("email"),
    )


def downgrade() -> None:
    """Drop the AUTH-003 table again."""
    op.drop_table("user_roles")
