"""Add content-addressed PDF cache metadata for IPO-003.

Revision ID: 20260630ipo003
Revises: 20260630ipo002

Beginner note:
The four nullable provenance columns move as one unit. A row either has a
verified downloaded file and ``pending`` status, or it has no file metadata at
all. Database checks preserve that rule even when code outside the typed IPO
repository writes directly through SQLAlchemy.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260630ipo003"
down_revision = "20260630ipo002"
branch_labels = None
depends_on = None

# Validate content_sha256 as a 64-char lowercase hex digest without a regex
# (SQLite has none built in): strip every hex digit with nested replace() calls
# and require the remainder to be empty, so only hex characters could remain.
# Runs identically on SQLite and PostgreSQL. Kept byte-identical to the ORM
# CheckConstraint in backend/storage/models.py for the migration-parity test.
_SHA256_CHECK = (
    "content_sha256 IS NULL OR (length(content_sha256) = 64 "
    "AND content_sha256 = lower(content_sha256) "
    "AND replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
    "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = '')"
)

_METADATA_CHECK = (
    "(parse_status = 'pending' AND content_sha256 IS NOT NULL "
    "AND downloaded_at IS NOT NULL AND file_path IS NOT NULL "
    "AND page_count IS NULL) OR "
    "(parse_status IN ('not_downloaded', 'download_failed') "
    "AND content_sha256 IS NULL AND downloaded_at IS NULL "
    "AND file_path IS NULL AND page_count IS NULL)"
)


def upgrade() -> None:
    """Add constrained cache provenance while preserving metadata-only rows.

    No content-hash index is created because cache lookup addresses the filesystem
    directly by digest. The grouped CHECK ensures callers cannot persist a path,
    hash, or timestamp separately and accidentally make partial bytes look valid.
    """
    with op.batch_alter_table("ipo_documents") as batch_op:
        batch_op.add_column(sa.Column("content_sha256", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("downloaded_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("file_path", sa.Text(), nullable=True))
        batch_op.add_column(sa.Column("page_count", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "parse_status",
                sa.String(length=20),
                nullable=False,
                server_default="not_downloaded",
            )
        )
        batch_op.create_check_constraint("ck_ipo_documents_content_sha256", _SHA256_CHECK)
        batch_op.create_check_constraint(
            "ck_ipo_documents_parse_status",
            "parse_status IN ('not_downloaded', 'pending', 'download_failed')",
        )
        batch_op.create_check_constraint(
            "ck_ipo_documents_page_count", "page_count IS NULL OR page_count > 0"
        )
        batch_op.create_check_constraint(
            "ck_ipo_documents_download_metadata", _METADATA_CHECK
        )


def downgrade() -> None:
    """Refuse to remove IPO-003 columns while any cache receipt would be lost."""
    protected_rows = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_documents WHERE "
            "content_sha256 IS NOT NULL OR downloaded_at IS NOT NULL OR "
            "file_path IS NOT NULL OR page_count IS NOT NULL OR "
            "parse_status <> 'not_downloaded'"
        )
    ).scalar_one()
    if protected_rows:
        raise RuntimeError("Refusing to discard IPO-003 document-cache metadata during downgrade.")

    with op.batch_alter_table("ipo_documents") as batch_op:
        batch_op.drop_constraint("ck_ipo_documents_download_metadata", type_="check")
        batch_op.drop_constraint("ck_ipo_documents_page_count", type_="check")
        batch_op.drop_constraint("ck_ipo_documents_parse_status", type_="check")
        batch_op.drop_constraint("ck_ipo_documents_content_sha256", type_="check")
        batch_op.drop_column("parse_status")
        batch_op.drop_column("page_count")
        batch_op.drop_column("file_path")
        batch_op.drop_column("downloaded_at")
        batch_op.drop_column("content_sha256")
