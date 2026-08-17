"""Allow the IPO-011 web-sourced subscription-demand enrichment signal.

Revision ID: 20260720ipo011
Revises: 20260718ipo010

Beginner note:
The only schema change the one-button screener needs is one more allowed
value in the enrichment signal-type vocabulary. Everything else it stores
(cited issue terms, auto-approval attribution) rides in existing JSON and
text columns.

The new ``subscription_demand`` value is the single numeric topic the web
collector may observe. Its blast radius is contained in code, not here: a
low-confidence snapshot may feed the optional QIB factor but can never fire
the hard ``weak_qib_demand_near_close`` caution.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260720ipo011"
down_revision = "20260718ipo010"
branch_labels = None
depends_on = None

# Keep both strings byte-identical to backend/storage/models.py so the
# ORM/Alembic parity test can compare reflected schemas without drift.
_SIGNAL_TYPE_CHECK_WIDE = (
    "signal_type IN ('gmp', 'news', 'promoter_reputation', 'litigation_red_flag', "
    "'anchor_commentary', 'brokerage_review', 'peer_discovery', "
    "'subscription_demand')"
)
_SIGNAL_TYPE_CHECK_LEGACY = (
    "signal_type IN ('gmp', 'news', 'promoter_reputation', 'litigation_red_flag', "
    "'anchor_commentary', 'brokerage_review', 'peer_discovery')"
)


def upgrade() -> None:
    """Widen the enrichment signal-type CHECK to accept subscription demand.

    Beginner note:
    Alembic batch operations rebuild the table on SQLite and issue a normal
    ALTER on PostgreSQL, so one construct keeps local tests and production
    aligned. Widening a CHECK is backward compatible: every row that was
    valid before is still valid after.
    """
    with op.batch_alter_table("ipo_enrichment_signals") as batch_op:
        batch_op.drop_constraint("ck_ipo_enrichment_signals_signal_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_enrichment_signals_signal_type", _SIGNAL_TYPE_CHECK_WIDE
        )


def downgrade() -> None:
    """Restore the narrow vocabulary only when no such signal exists.

    Beginner note:
        Narrowing a CHECK would make existing ``subscription_demand`` rows
        illegal, and SQLite's table rebuild would fail confusingly mid-flight.
        Counting first turns that into one clear, actionable error.
    """
    connection = op.get_bind()
    observed = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_enrichment_signals "
            "WHERE signal_type = 'subscription_demand'"
        )
    ).scalar_one()
    if observed:
        raise RuntimeError(
            "Refusing to discard IPO-011 subscription-demand signals during downgrade."
        )

    with op.batch_alter_table("ipo_enrichment_signals") as batch_op:
        batch_op.drop_constraint("ck_ipo_enrichment_signals_signal_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_enrichment_signals_signal_type", _SIGNAL_TYPE_CHECK_LEGACY
        )
