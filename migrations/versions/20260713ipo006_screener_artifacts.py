"""Add the IPO-006..010 screener artifacts: proposals, signals, verdict metadata.

Revision ID: 20260713ipo006
Revises: 20260703ipo005

Beginner note:
Two additive tables carry evidence that must never silently enter scoring:
``ipo_extraction_proposals`` holds AI-proposed extractions awaiting human
review, and ``ipo_enrichment_signals`` holds low-confidence web observations.
The existing evaluation pair gains three additions: a fourth
``recommendation_type`` for the fail-closed "Insufficient verified data"
verdict, a complete caution-flag report on each recommendation, and an
inputs fingerprint on each score so idempotent re-runs can skip unchanged
issues.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260713ipo006"
down_revision = "20260703ipo005"
branch_labels = None
depends_on = None

# Keep every CHECK expression byte-identical to backend/storage/models.py so
# the ORM/Alembic parity test can compare reflected schemas without drift.
_PROPOSAL_REVIEW_METADATA_CHECK = (
    "(status = 'pending' AND reviewed_by_email IS NULL AND reviewed_at IS NULL "
    "AND review_note IS NULL AND manual_extraction_id IS NULL) OR "
    "(status IN ('approved', 'rejected') AND reviewed_by_email IS NOT NULL "
    "AND reviewed_at IS NOT NULL)"
)
_PROPOSAL_CONTENT_HASH_CHECK = (
    "length(source_content_sha256) = 64 AND "
    "source_content_sha256 = lower(source_content_sha256) AND "
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "replace(replace(replace(replace(replace(replace(replace(replace("
    "source_content_sha256, '0', ''), '1', ''), '2', ''), '3', ''), '4', ''), "
    "'5', ''), '6', ''), '7', ''), '8', ''), '9', ''), 'a', ''), "
    "'b', ''), 'c', ''), 'd', ''), 'e', ''), 'f', '') = ''"
)
_ENRICHMENT_SIGNAL_TYPE_CHECK = (
    "signal_type IN ('gmp', 'news', 'promoter_reputation', 'litigation_red_flag', "
    "'anchor_commentary', 'brokerage_review', 'peer_discovery')"
)
_RECOMMENDATION_TYPE_CHECK_WIDE = (
    "recommendation_type IN ('Apply confidently and consider holding if allotted', "
    "'Apply primarily for listing gains', 'Skip', 'Insufficient verified data')"
)
_RECOMMENDATION_TYPE_CHECK_LEGACY = (
    "recommendation_type IN ('Apply confidently and consider holding if allotted', "
    "'Apply primarily for listing gains', 'Skip')"
)


def _big_int_primary_key() -> sa.BigInteger:
    """Use SQLite INTEGER rowids while retaining BIGINT on Postgres."""
    return sa.BigInteger().with_variant(sa.Integer(), "sqlite")


def upgrade() -> None:
    """Create the review-queue and enrichment tables, then extend evaluations.

    Beginner note:
    The new tables land first because they have no effect on existing rows.
    The evaluation-pair changes use Alembic batch operations, which rebuild
    tables on SQLite and issue normal ALTERs on PostgreSQL, so both dialects
    end at the same shape. ``caution_flags_json`` carries an empty-list server
    default because legacy evaluations were scored before flags existed.
    """
    op.create_table(
        "ipo_extraction_proposals",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("document_id", _big_int_primary_key(), nullable=False),
        sa.Column(
            "status", sa.String(length=16), nullable=False, server_default="pending"
        ),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("needs_review_reasons_json", sa.JSON(), nullable=False),
        sa.Column("model_version", sa.String(length=40), nullable=False),
        sa.Column("agent_model", sa.String(length=64), nullable=False),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("page_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_by_email", sa.Text(), nullable=True),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text(), nullable=True),
        sa.Column("manual_extraction_id", _big_int_primary_key(), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')",
            name="ck_ipo_extraction_proposals_status",
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_extraction_proposals_confidence",
        ),
        sa.CheckConstraint(
            _PROPOSAL_REVIEW_METADATA_CHECK,
            name="ck_ipo_extraction_proposals_review_metadata",
        ),
        sa.CheckConstraint(
            "status != 'approved' OR manual_extraction_id IS NOT NULL",
            name="ck_ipo_extraction_proposals_approval_link",
        ),
        sa.CheckConstraint(
            "page_count > 0", name="ck_ipo_extraction_proposals_page_count"
        ),
        sa.CheckConstraint(
            _PROPOSAL_CONTENT_HASH_CHECK,
            name="ck_ipo_extraction_proposals_content_hash",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["document_id"], ["ipo_documents.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["manual_extraction_id"], ["ipo_manual_extractions.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_ipo_extraction_proposals_issue_id", "ipo_extraction_proposals", ["issue_id"]
    )
    op.create_index(
        "ix_ipo_extraction_proposals_document_id",
        "ipo_extraction_proposals",
        ["document_id"],
    )
    op.create_index(
        "ix_ipo_extraction_proposals_manual_extraction_id",
        "ipo_extraction_proposals",
        ["manual_extraction_id"],
    )

    op.create_table(
        "ipo_enrichment_signals",
        sa.Column("id", _big_int_primary_key(), nullable=False),
        sa.Column("issue_id", _big_int_primary_key(), nullable=False),
        sa.Column("signal_type", sa.String(length=32), nullable=False),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("query_text", sa.String(length=255), nullable=False),
        sa.Column("payload_json", sa.JSON(), nullable=False),
        sa.Column("parsed_value", sa.Numeric(12, 2), nullable=True),
        sa.Column("quarantined", sa.Boolean(), nullable=False),
        sa.Column("confidence", sa.String(length=8), nullable=False),
        sa.Column("source_policy", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            _ENRICHMENT_SIGNAL_TYPE_CHECK,
            name="ck_ipo_enrichment_signals_signal_type",
        ),
        sa.CheckConstraint(
            "confidence IN ('low', 'medium', 'high')",
            name="ck_ipo_enrichment_signals_confidence",
        ),
        sa.ForeignKeyConstraint(["issue_id"], ["ipo_issues.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issue_id",
            "signal_type",
            "captured_at",
            name="uq_ipo_enrichment_signals_issue_type_capture",
        ),
    )
    op.create_index(
        "ix_ipo_enrichment_signals_issue_id", "ipo_enrichment_signals", ["issue_id"]
    )
    op.create_index(
        "ix_ipo_enrichment_signals_captured_at",
        "ipo_enrichment_signals",
        ["captured_at"],
    )

    with op.batch_alter_table("ipo_scores") as batch_op:
        batch_op.add_column(
            sa.Column("inputs_fingerprint", sa.String(length=64), nullable=True)
        )
        batch_op.create_check_constraint(
            "ck_ipo_scores_inputs_fingerprint_length",
            "inputs_fingerprint IS NULL OR length(inputs_fingerprint) = 64",
        )

    with op.batch_alter_table("ipo_recommendations") as batch_op:
        batch_op.add_column(
            sa.Column(
                "caution_flags_json", sa.JSON(), nullable=False, server_default="[]"
            )
        )
        batch_op.drop_constraint("ck_ipo_recommendations_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_recommendations_type", _RECOMMENDATION_TYPE_CHECK_WIDE
        )


def downgrade() -> None:
    """Remove the IPO-006..010 artifacts only when nothing would be lost.

    Beginner note:
        Every evaluation written by the ipo-006 scoring service carries an
        ``inputs_fingerprint``, so guarding on fingerprints, the new verdict
        type, and any row in the two new tables covers all artifacts the new
        code can produce. Counting first keeps the schema intact when the
        downgrade would silently delete history.
    """
    connection = op.get_bind()
    proposal_rows = connection.execute(
        sa.text("SELECT COUNT(*) FROM ipo_extraction_proposals")
    ).scalar_one()
    signal_rows = connection.execute(
        sa.text("SELECT COUNT(*) FROM ipo_enrichment_signals")
    ).scalar_one()
    fingerprint_rows = connection.execute(
        sa.text("SELECT COUNT(*) FROM ipo_scores WHERE inputs_fingerprint IS NOT NULL")
    ).scalar_one()
    insufficient_rows = connection.execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_recommendations "
            "WHERE recommendation_type = 'Insufficient verified data'"
        )
    ).scalar_one()
    if proposal_rows or signal_rows or fingerprint_rows or insufficient_rows:
        raise RuntimeError(
            "Refusing to discard IPO-006 screener artifacts during downgrade."
        )

    with op.batch_alter_table("ipo_recommendations") as batch_op:
        batch_op.drop_constraint("ck_ipo_recommendations_type", type_="check")
        batch_op.create_check_constraint(
            "ck_ipo_recommendations_type", _RECOMMENDATION_TYPE_CHECK_LEGACY
        )
        batch_op.drop_column("caution_flags_json")

    with op.batch_alter_table("ipo_scores") as batch_op:
        batch_op.drop_constraint(
            "ck_ipo_scores_inputs_fingerprint_length", type_="check"
        )
        batch_op.drop_column("inputs_fingerprint")

    op.drop_table("ipo_enrichment_signals")
    op.drop_table("ipo_extraction_proposals")
