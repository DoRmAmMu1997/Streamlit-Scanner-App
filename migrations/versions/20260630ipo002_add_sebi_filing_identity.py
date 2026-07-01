"""Add SEBI filing identity fields for IPO-002 ingestion.

Revision ID: 20260630ipo002
Revises: 20260629ipo001
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260630ipo002"
down_revision = "20260629ipo001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add nullable ingestion identities while preserving all IPO-001 rows.

    Nullable columns keep manual/legacy records valid. Unique indexes turn the
    normalized company key and filing hash into idempotency guards, while the
    filing-date index supports the incremental scan watermark.
    """
    with op.batch_alter_table("ipo_issues") as batch_op:
        batch_op.add_column(sa.Column("sebi_company_key", sa.String(length=255), nullable=True))
        batch_op.drop_constraint("ck_ipo_issues_issue_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_issues_issue_type",
            "issue_type IN ('mainboard', 'sme', 'unknown')",
        )
        batch_op.create_index(
            "ux_ipo_issues_sebi_company_key",
            ["sebi_company_key"],
            unique=True,
        )

    with op.batch_alter_table("ipo_documents") as batch_op:
        batch_op.add_column(sa.Column("filing_date", sa.Date(), nullable=True))
        batch_op.add_column(sa.Column("record_hash", sa.String(length=64), nullable=True))
        batch_op.create_check_constraint(
            "ck_ipo_documents_record_hash_length",
            "record_hash IS NULL OR length(record_hash) = 64",
        )
        batch_op.create_index("ix_ipo_documents_filing_date", ["filing_date"], unique=False)
        batch_op.create_index("ux_ipo_documents_record_hash", ["record_hash"], unique=True)


def downgrade() -> None:
    """Remove IPO-002 fields only when no identity or ``unknown`` type is lost."""
    # IPO-001 cannot represent ``unknown`` or retain SEBI identity metadata.
    # Refuse the downgrade before any DDL instead of silently reclassifying an
    # issue or dropping its ingestion receipt. Operators may explicitly export
    # or remove IPO-002 records before retrying the downgrade.
    protected_rows = op.get_bind().execute(
        sa.text(
            "SELECT "
            "(SELECT COUNT(*) FROM ipo_issues "
            " WHERE sebi_company_key IS NOT NULL OR issue_type = 'unknown') + "
            "(SELECT COUNT(*) FROM ipo_documents "
            " WHERE filing_date IS NOT NULL OR record_hash IS NOT NULL)"
        )
    ).scalar_one()
    if protected_rows:
        raise RuntimeError(
            "Refusing to discard IPO-002 filing identities during downgrade."
        )

    with op.batch_alter_table("ipo_documents") as batch_op:
        batch_op.drop_index("ux_ipo_documents_record_hash")
        batch_op.drop_index("ix_ipo_documents_filing_date")
        batch_op.drop_constraint("ck_ipo_documents_record_hash_length", type_="check")
        batch_op.drop_column("record_hash")
        batch_op.drop_column("filing_date")

    with op.batch_alter_table("ipo_issues") as batch_op:
        batch_op.drop_index("ux_ipo_issues_sebi_company_key")
        batch_op.drop_constraint("ck_ipo_issues_issue_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_issues_issue_type",
            "issue_type IN ('mainboard', 'sme')",
        )
        batch_op.drop_column("sebi_company_key")
