"""Create the IPO-001 domain, evidence, score, and recommendation tables.

Revision ID: 20260629ipo001
Revises: 20260623auth003
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260629ipo001"
down_revision = "20260623auth003"
branch_labels = None
depends_on = None


def _big_int_primary_key() -> sa.BigInteger:
    """Use SQLite INTEGER rowids while retaining BIGINT on Postgres."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def _source_confidence_constraint(table: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(
        "source_confidence IN ('low', 'medium', 'high')",
        name=f"ck_{table}_source_confidence",
    )


def upgrade() -> None:
    """Create all six additive IPO-001 tables and their lookup indexes."""
    op.create_table(
        "ipo_issues",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("company_name", sa.String(length=255), nullable=False),
        sa.Column("issue_type", sa.String(length=16), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("open_date", sa.Date(), nullable=True),
        sa.Column("close_date", sa.Date(), nullable=True),
        sa.Column("price_band_low", sa.Numeric(18, 2), nullable=True),
        sa.Column("price_band_high", sa.Numeric(18, 2), nullable=True),
        sa.Column("lot_size", sa.Integer(), nullable=True),
        sa.Column("fresh_issue_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("ofs_amount", sa.Numeric(20, 2), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_confidence", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("issue_type IN ('mainboard', 'sme')", name="ck_ipo_issues_issue_type"),
        sa.CheckConstraint(
            "status IN ('drhp_filed', 'rhp_filed', 'open', 'closed', 'listed')",
            name="ck_ipo_issues_status",
        ),
        sa.CheckConstraint(
            "open_date IS NULL OR close_date IS NULL OR close_date >= open_date",
            name="ck_ipo_issues_date_order",
        ),
        sa.CheckConstraint(
            "(price_band_low IS NULL OR price_band_low >= 0) AND "
            "(price_band_high IS NULL OR price_band_high >= 0) AND "
            "(price_band_low IS NULL OR price_band_high IS NULL OR price_band_high >= price_band_low)",
            name="ck_ipo_issues_price_band",
        ),
        sa.CheckConstraint("lot_size IS NULL OR lot_size > 0", name="ck_ipo_issues_lot_size"),
        sa.CheckConstraint(
            "fresh_issue_amount IS NULL OR fresh_issue_amount >= 0",
            name="ck_ipo_issues_fresh_amount",
        ),
        sa.CheckConstraint("ofs_amount IS NULL OR ofs_amount >= 0", name="ck_ipo_issues_ofs_amount"),
        _source_confidence_constraint("ipo_issues"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ipo_issues_company_name", "ipo_issues", ["company_name"])
    op.create_index("ix_ipo_issues_status", "ipo_issues", ["status"])
    op.create_index("ix_ipo_issues_status_open_date", "ipo_issues", ["status", "open_date"])

    op.create_table(
        "ipo_documents",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("document_type", sa.String(length=50), nullable=False),
        sa.Column("document_url", sa.Text(), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_confidence", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        _source_confidence_constraint("ipo_documents"),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "document_type", "document_url", name="uq_ipo_documents_issue_type_url"
        ),
    )
    op.create_index("ix_ipo_documents_issue_id", "ipo_documents", ["issue_id"])

    op.create_table(
        "ipo_financials",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("period_end", sa.Date(), nullable=False),
        sa.Column("period_type", sa.String(length=16), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("source_document_id", _big_int_primary_key(), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_confidence", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "period_type IN ('annual', 'quarterly')", name="ck_ipo_financials_period_type"
        ),
        _source_confidence_constraint("ipo_financials"),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["source_document_id"], ["ipo_documents.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "period_end", "period_type", name="uq_ipo_financials_issue_period"
        ),
    )
    op.create_index("ix_ipo_financials_issue_id", "ipo_financials", ["issue_id"])
    op.create_index(
        "ix_ipo_financials_source_document_id", "ipo_financials", ["source_document_id"]
    )

    op.create_table(
        "ipo_subscriptions",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("qib_multiple", sa.Numeric(12, 2), nullable=True),
        sa.Column("nii_multiple", sa.Numeric(12, 2), nullable=True),
        sa.Column("retail_multiple", sa.Numeric(12, 2), nullable=True),
        sa.Column("total_multiple", sa.Numeric(12, 2), nullable=True),
        sa.Column("source_url", sa.Text(), nullable=True),
        sa.Column("source_confidence", sa.String(length=8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "(qib_multiple IS NULL OR qib_multiple >= 0) AND "
            "(nii_multiple IS NULL OR nii_multiple >= 0) AND "
            "(retail_multiple IS NULL OR retail_multiple >= 0) AND "
            "(total_multiple IS NULL OR total_multiple >= 0)",
            name="ck_ipo_subscriptions_nonnegative",
        ),
        _source_confidence_constraint("ipo_subscriptions"),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id", "captured_at", name="uq_ipo_subscriptions_issue_capture"
        ),
    )
    op.create_index("ix_ipo_subscriptions_captured_at", "ipo_subscriptions", ["captured_at"])
    op.create_index("ix_ipo_subscriptions_issue_id", "ipo_subscriptions", ["issue_id"])

    score_columns = [
        "business_quality",
        "financial_growth",
        "return_ratios",
        "valuation",
        "qib_subscription",
        "promoter_quality",
        "gmp_sentiment",
    ]
    op.create_table(
        "ipo_scores",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        *(sa.Column(name, sa.Numeric(5, 2), nullable=True) for name in score_columns),
        sa.Column("total_score", sa.Numeric(5, 2), nullable=False),
        sa.Column("contributions_json", sa.JSON(), nullable=False),
        sa.Column("missing_data_json", sa.JSON(), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.String(length=32), nullable=False),
        sa.Column("scored_at", sa.DateTime(timezone=True), nullable=False),
        *(
            sa.CheckConstraint(
                f"{name} IS NULL OR ({name} >= 0 AND {name} <= 100)",
                name=f"ck_ipo_scores_{name}_range",
            )
            for name in score_columns
        ),
        sa.CheckConstraint(
            "total_score >= 0 AND total_score <= 100", name="ck_ipo_scores_total_range"
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_ipo_scores_issue_id", "ipo_scores", ["issue_id"])
    op.create_index("ix_ipo_scores_issue_scored_at", "ipo_scores", ["issue_id", "scored_at"])

    op.create_table(
        "ipo_recommendations",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("score_id", _big_int_primary_key(), nullable=False),
        sa.Column("recommendation", sa.String(length=32), nullable=False),
        sa.Column("recommendation_type", sa.String(length=80), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("reasons_json", sa.JSON(), nullable=False),
        sa.Column("missing_data_json", sa.JSON(), nullable=False),
        sa.Column("source_documents_json", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "recommendation IN ('Recommended', 'Not Recommended')",
            name="ck_ipo_recommendations_binary",
        ),
        sa.CheckConstraint(
            "recommendation_type IN ('Apply confidently and consider holding if allotted', "
            "'Apply primarily for listing gains', 'Skip')",
            name="ck_ipo_recommendations_type",
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_recommendations_confidence",
        ),
        sa.ForeignKeyConstraint(["score_id"], ["ipo_scores.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ipo_recommendations_score_id",
        "ipo_recommendations",
        ["score_id"],
        unique=True,
    )


def downgrade() -> None:
    """Drop IPO tables in reverse foreign-key order."""
    op.drop_table("ipo_recommendations")
    op.drop_table("ipo_scores")
    op.drop_table("ipo_subscriptions")
    op.drop_table("ipo_financials")
    op.drop_table("ipo_documents")
    op.drop_table("ipo_issues")

