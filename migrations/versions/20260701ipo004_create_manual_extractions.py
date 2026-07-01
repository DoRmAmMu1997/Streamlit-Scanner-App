"""Create immutable, sourced manual IPO extraction revisions for IPO-004.

Revision ID: 20260701ipo004
Revises: 20260630ipo003

Beginner note:
Alembic upgrades the header before its children because foreign keys can only
reference a table that already exists. Downgrade performs the reverse order and
refuses to discard populated manual history; an operator must explicitly archive
or remove that evidence before choosing a pre-IPO-004 schema.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260701ipo004"
down_revision = "20260630ipo003"
branch_labels = None
depends_on = None


def _big_int_primary_key() -> sa.TypeEngine:
    """Use BIGINT in Postgres while preserving SQLite auto-increment behavior."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the revision header, three-period children, and peer rows."""
    op.create_table(
        "ipo_manual_extractions",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("source_document_id", _big_int_primary_key(), nullable=True),
        sa.Column("source_document_url", sa.Text(), nullable=False),
        sa.Column("source_record_hash", sa.String(length=64), nullable=True),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("financial_amount_unit", sa.String(length=24), nullable=False),
        sa.Column("issue_amount_unit", sa.String(length=24), nullable=False),
        sa.Column("equity_share_unit", sa.String(length=24), nullable=False),
        sa.Column("net_worth", sa.Numeric(24, 4), nullable=False),
        sa.Column("net_worth_page", sa.Integer(), nullable=False),
        sa.Column("total_debt", sa.Numeric(24, 4), nullable=False),
        sa.Column("total_debt_page", sa.Integer(), nullable=False),
        sa.Column("cash", sa.Numeric(24, 4), nullable=False),
        sa.Column("cash_page", sa.Integer(), nullable=False),
        sa.Column("cash_flow_from_operations", sa.Numeric(24, 4), nullable=False),
        sa.Column("cash_flow_from_operations_page", sa.Integer(), nullable=False),
        sa.Column("equity_shares", sa.Numeric(24, 4), nullable=False),
        sa.Column("equity_shares_page", sa.Integer(), nullable=False),
        sa.Column("eps", sa.Numeric(24, 4), nullable=False),
        sa.Column("eps_page", sa.Integer(), nullable=False),
        sa.Column("nav_book_value", sa.Numeric(24, 4), nullable=False),
        sa.Column("nav_book_value_page", sa.Integer(), nullable=False),
        sa.Column("objects_of_issue", sa.Text(), nullable=False),
        sa.Column("objects_of_issue_page", sa.Integer(), nullable=False),
        sa.Column("fresh_issue_amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("fresh_issue_amount_page", sa.Integer(), nullable=False),
        sa.Column("ofs_amount", sa.Numeric(24, 4), nullable=False),
        sa.Column("ofs_amount_page", sa.Integer(), nullable=False),
        sa.Column("promoter_holding_pre_issue", sa.Numeric(7, 4), nullable=False),
        sa.Column("promoter_holding_pre_issue_page", sa.Integer(), nullable=False),
        sa.Column("promoter_holding_post_issue", sa.Numeric(7, 4), nullable=False),
        sa.Column("promoter_holding_post_issue_page", sa.Integer(), nullable=False),
        sa.Column("entered_by_email", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "financial_amount_unit IN ('inr', 'thousand_inr', 'lakh_inr', "
            "'million_inr', 'crore_inr')",
            name="ck_ipo_manual_extractions_financial_unit",
        ),
        sa.CheckConstraint(
            "issue_amount_unit IN ('inr', 'thousand_inr', 'lakh_inr', "
            "'million_inr', 'crore_inr')",
            name="ck_ipo_manual_extractions_issue_unit",
        ),
        sa.CheckConstraint(
            "equity_share_unit IN ('shares', 'thousand_shares', 'lakh_shares', "
            "'million_shares', 'crore_shares')",
            name="ck_ipo_manual_extractions_share_unit",
        ),
        sa.CheckConstraint(
            "length(source_content_sha256) = 64 AND "
            "source_content_sha256 = lower(source_content_sha256) AND "
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "replace(replace(replace(replace(replace(replace(replace(replace("
            "source_content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
            "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
            "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''",
            name="ck_ipo_manual_extractions_content_hash",
        ),
        sa.CheckConstraint(
            "source_record_hash IS NULL OR length(source_record_hash) = 64",
            name="ck_ipo_manual_extractions_record_hash",
        ),
        sa.CheckConstraint(
            "total_debt >= 0 AND cash >= 0 AND equity_shares > 0 AND "
            "fresh_issue_amount >= 0 AND ofs_amount >= 0",
            name="ck_ipo_manual_extractions_nonnegative",
        ),
        sa.CheckConstraint(
            "promoter_holding_pre_issue >= 0 AND promoter_holding_pre_issue <= 100 AND "
            "promoter_holding_post_issue >= 0 AND promoter_holding_post_issue <= 100",
            name="ck_ipo_manual_extractions_promoter_range",
        ),
        sa.CheckConstraint(
            "net_worth_page > 0 AND total_debt_page > 0 AND cash_page > 0 AND "
            "cash_flow_from_operations_page > 0 AND equity_shares_page > 0 AND "
            "eps_page > 0 AND nav_book_value_page > 0 AND objects_of_issue_page > 0 AND "
            "fresh_issue_amount_page > 0 AND ofs_amount_page > 0 AND "
            "promoter_holding_pre_issue_page > 0 AND promoter_holding_post_issue_page > 0",
            name="ck_ipo_manual_extractions_pages",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["ipo_documents.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ipo_manual_extractions_source_document_id",
        "ipo_manual_extractions",
        ["source_document_id"],
    )
    op.create_index(
        "ix_ipo_manual_extractions_issue_submitted",
        "ipo_manual_extractions",
        ["issue_id", "submitted_at", "id"],
    )

    op.create_table(
        "ipo_manual_financial_periods",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("extraction_id", _big_int_primary_key(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("revenue", sa.Numeric(24, 4), nullable=False),
        sa.Column("revenue_page", sa.Integer(), nullable=False),
        sa.Column("ebitda", sa.Numeric(24, 4), nullable=False),
        sa.Column("ebitda_page", sa.Integer(), nullable=False),
        sa.Column("pat", sa.Numeric(24, 4), nullable=False),
        sa.Column("pat_page", sa.Integer(), nullable=False),
        sa.CheckConstraint(
            "ordinal >= 1 AND ordinal <= 3", name="ck_ipo_manual_periods_ordinal"
        ),
        sa.CheckConstraint("revenue >= 0", name="ck_ipo_manual_periods_revenue"),
        sa.CheckConstraint(
            "revenue_page > 0 AND ebitda_page > 0 AND pat_page > 0",
            name="ck_ipo_manual_periods_pages",
        ),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["ipo_manual_extractions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_id", "ordinal", name="uq_ipo_manual_periods_ordinal"
        ),
        sa.UniqueConstraint(
            "extraction_id", "period_end", name="uq_ipo_manual_periods_date"
        ),
    )
    op.create_index(
        "ix_ipo_manual_financial_periods_extraction_id",
        "ipo_manual_financial_periods",
        ["extraction_id"],
    )

    op.create_table(
        "ipo_manual_peer_valuations",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("extraction_id", _big_int_primary_key(), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("company_key", sa.String(length=255), nullable=False),
        sa.Column("source_page", sa.Integer(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.CheckConstraint("source_page > 0", name="ck_ipo_manual_peers_page"),
        sa.ForeignKeyConstraint(
            ["extraction_id"], ["ipo_manual_extractions.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "extraction_id", "company_key", name="uq_ipo_manual_peers_company"
        ),
    )
    op.create_index(
        "ix_ipo_manual_peer_valuations_extraction_id",
        "ipo_manual_peer_valuations",
        ["extraction_id"],
    )


def downgrade() -> None:
    """Drop empty IPO-004 tables, refusing silent loss of manual evidence."""
    # Alembic cannot reverse an immutable evidence ledger losslessly once it
    # contains rows. Refusing before any DDL leaves the database fully usable and
    # gives the operator a chance to archive or explicitly remove the records.
    connection = op.get_bind()
    count = connection.scalar(sa.text("SELECT COUNT(*) FROM ipo_manual_extractions"))
    if count:
        raise RuntimeError(
            "Downgrade would discard IPO-004 manual extraction revisions; "
            "archive or delete them explicitly first."
        )
    op.drop_table("ipo_manual_peer_valuations")
    op.drop_table("ipo_manual_financial_periods")
    op.drop_table("ipo_manual_extractions")
