"""Add sourced raw facts required by the IPO-005 ratio engine.

Revision ID: 20260703ipo005
Revises: 20260701ipo004

Beginner note:
The columns are nullable for one compatibility reason: an IPO-004 revision was
valid before PBT, finance cost, total assets, current liabilities, and post-issue
shares existed. Grouped CHECK constraints allow either that all-null legacy shape
or a fully sourced IPO-005 shape; partial evidence is never valid.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260703ipo005"
down_revision = "20260701ipo004"
branch_labels = None
depends_on = None

_HEADER_RATIO_INPUTS_CHECK = (
    "(total_assets IS NULL AND total_assets_page IS NULL AND "
    "current_liabilities IS NULL AND current_liabilities_page IS NULL AND "
    "post_issue_equity_shares IS NULL AND post_issue_equity_shares_page IS NULL) OR "
    "(total_assets IS NOT NULL AND total_assets_page IS NOT NULL AND "
    "current_liabilities IS NOT NULL AND current_liabilities_page IS NOT NULL AND "
    "post_issue_equity_shares IS NOT NULL AND post_issue_equity_shares_page IS NOT NULL AND "
    "total_assets >= 0 AND current_liabilities >= 0 AND "
    "post_issue_equity_shares > 0 AND total_assets_page > 0 AND "
    "current_liabilities_page > 0 AND post_issue_equity_shares_page > 0)"
)

_PERIOD_RATIO_INPUTS_CHECK = (
    "(profit_before_tax IS NULL AND profit_before_tax_page IS NULL AND "
    "finance_cost IS NULL AND finance_cost_page IS NULL) OR "
    "(profit_before_tax IS NOT NULL AND profit_before_tax_page IS NOT NULL AND "
    "finance_cost IS NOT NULL AND finance_cost_page IS NOT NULL AND "
    "finance_cost >= 0 AND profit_before_tax_page > 0 AND finance_cost_page > 0)"
)


def upgrade() -> None:
    """Add legacy-compatible, source-paired ratio inputs to manual revisions.

    Beginner note:
    Alembic batch operations rebuild tables on SQLite and issue normal ALTERs on
    PostgreSQL. Using the same construct keeps local tests and production schema
    behavior aligned instead of maintaining dialect-specific migration branches.
    """
    # Header facts occur once for the selected balance-sheet/share snapshot.
    with op.batch_alter_table("ipo_manual_extractions") as batch_op:
        batch_op.add_column(sa.Column("total_assets", sa.Numeric(24, 4), nullable=True))
        batch_op.add_column(sa.Column("total_assets_page", sa.Integer(), nullable=True))
        batch_op.add_column(
            sa.Column("current_liabilities", sa.Numeric(24, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column("current_liabilities_page", sa.Integer(), nullable=True)
        )
        batch_op.add_column(
            sa.Column("post_issue_equity_shares", sa.Numeric(24, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column("post_issue_equity_shares_page", sa.Integer(), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_ipo_manual_extractions_ratio_inputs",
            _HEADER_RATIO_INPUTS_CHECK,
        )

    # PBT and finance cost belong to each annual period, just like revenue,
    # EBITDA, and PAT. Each keeps its own prospectus page citation.
    with op.batch_alter_table("ipo_manual_financial_periods") as batch_op:
        batch_op.add_column(
            sa.Column("profit_before_tax", sa.Numeric(24, 4), nullable=True)
        )
        batch_op.add_column(
            sa.Column("profit_before_tax_page", sa.Integer(), nullable=True)
        )
        batch_op.add_column(sa.Column("finance_cost", sa.Numeric(24, 4), nullable=True))
        batch_op.add_column(sa.Column("finance_cost_page", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(
            "ck_ipo_manual_periods_ratio_inputs",
            _PERIOD_RATIO_INPUTS_CHECK,
        )


def downgrade() -> None:
    """Remove IPO-005 columns only when doing so cannot discard entered facts."""
    connection = op.get_bind()
    header_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_manual_extractions WHERE "
            "total_assets IS NOT NULL OR total_assets_page IS NOT NULL OR "
            "current_liabilities IS NOT NULL OR current_liabilities_page IS NOT NULL OR "
            "post_issue_equity_shares IS NOT NULL OR post_issue_equity_shares_page IS NOT NULL"
        )
    ).scalar_one()
    period_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_manual_financial_periods WHERE "
            "profit_before_tax IS NOT NULL OR profit_before_tax_page IS NOT NULL OR "
            "finance_cost IS NOT NULL OR finance_cost_page IS NOT NULL"
        )
    ).scalar_one()
    if header_rows or period_rows:
        raise RuntimeError(
            "Refusing to discard IPO-005 manual ratio inputs during downgrade."
        )

    # Children first mirrors the upgrade's dependency direction and keeps the
    # SQLite table rebuilds straightforward.
    with op.batch_alter_table("ipo_manual_financial_periods") as batch_op:
        batch_op.drop_constraint(
            "ck_ipo_manual_periods_ratio_inputs", type_="check"
        )
        batch_op.drop_column("finance_cost_page")
        batch_op.drop_column("finance_cost")
        batch_op.drop_column("profit_before_tax_page")
        batch_op.drop_column("profit_before_tax")

    with op.batch_alter_table("ipo_manual_extractions") as batch_op:
        batch_op.drop_constraint(
            "ck_ipo_manual_extractions_ratio_inputs", type_="check"
        )
        batch_op.drop_column("post_issue_equity_shares_page")
        batch_op.drop_column("post_issue_equity_shares")
        batch_op.drop_column("current_liabilities_page")
        batch_op.drop_column("current_liabilities")
        batch_op.drop_column("total_assets_page")
        batch_op.drop_column("total_assets")
