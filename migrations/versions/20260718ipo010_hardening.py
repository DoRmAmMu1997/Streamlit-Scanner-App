"""Harden IPO extraction, enrichment, and evaluation identity boundaries.

Revision ID: 20260718ipo010
Revises: 20260713ipo006

Beginner note:
This migration turns application-level promises into database invariants. It
prevents two pending extraction proposals for one document, preserves reviewed
proposal provenance when mutable document metadata is deleted, deduplicates
semantic evidence/evaluations, and adds versioned typed-evidence/breakdown
storage for IPO-010 remediation.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "20260718ipo010"
down_revision = "20260713ipo006"
branch_labels = None
depends_on = None

_FK_NAMING = {
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
}


def _document_fk_name() -> str:
    """Return the reflected document FK name on SQLite or PostgreSQL."""
    foreign_keys = sa.inspect(op.get_bind()).get_foreign_keys(
        "ipo_extraction_proposals"
    )
    document_fk = next(
        fk for fk in foreign_keys if fk["referred_table"] == "ipo_documents"
    )
    return str(
        document_fk["name"]
        or "fk_ipo_extraction_proposals_document_id_ipo_documents"
    )


def upgrade() -> None:
    """Add versioned evidence, semantic identity, and retention constraints."""
    document_fk_name = _document_fk_name()
    with op.batch_alter_table(
        "ipo_extraction_proposals",
        naming_convention=_FK_NAMING,
    ) as batch_op:
        batch_op.add_column(
            sa.Column("document_url_snapshot", sa.Text(), nullable=True)
        )
        batch_op.add_column(
            sa.Column(
                "evidence_schema_version",
                sa.String(length=40),
                nullable=False,
                server_default="legacy-unbound/v0",
            )
        )
        batch_op.add_column(
            sa.Column("semantic_fingerprint", sa.String(length=64), nullable=True)
        )
        batch_op.drop_constraint(document_fk_name, type_="foreignkey")
        batch_op.alter_column(
            "document_id",
            existing_type=sa.BigInteger(),
            nullable=True,
        )
        batch_op.create_foreign_key(
            "fk_ipo_extraction_proposals_document_id_ipo_documents",
            "ipo_documents",
            ["document_id"],
            ["id"],
            ondelete="SET NULL",
        )

    op.execute(
        sa.text(
            "UPDATE ipo_extraction_proposals "
            "SET document_url_snapshot = ("
            "SELECT document_url FROM ipo_documents "
            "WHERE ipo_documents.id = ipo_extraction_proposals.document_id"
            ")"
        )
    )
    with op.batch_alter_table("ipo_extraction_proposals") as batch_op:
        batch_op.alter_column(
            "document_url_snapshot",
            existing_type=sa.Text(),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_ipo_extraction_proposals_pending_document",
            "status != 'pending' OR document_id IS NOT NULL",
        )
        batch_op.create_check_constraint(
            "ck_ipo_extraction_proposals_semantic_fingerprint",
            "semantic_fingerprint IS NULL OR length(semantic_fingerprint) = 64",
        )

    op.create_index(
        "ux_ipo_extraction_proposals_pending_document",
        "ipo_extraction_proposals",
        ["document_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ux_ipo_extraction_proposals_semantic",
        "ipo_extraction_proposals",
        ["document_id", "semantic_fingerprint"],
        unique=True,
        sqlite_where=sa.text(
            "document_id IS NOT NULL AND semantic_fingerprint IS NOT NULL"
        ),
        postgresql_where=sa.text(
            "document_id IS NOT NULL AND semantic_fingerprint IS NOT NULL"
        ),
    )

    with op.batch_alter_table("ipo_scores") as batch_op:
        batch_op.add_column(
            sa.Column(
                "breakdown_json",
                sa.JSON(),
                nullable=False,
                server_default="[]",
            )
        )
    op.create_index(
        "ux_ipo_scores_semantic_evaluation",
        "ipo_scores",
        ["issue_id", "model_version", "inputs_fingerprint"],
        unique=True,
        sqlite_where=sa.text("inputs_fingerprint IS NOT NULL"),
        postgresql_where=sa.text("inputs_fingerprint IS NOT NULL"),
    )

    with op.batch_alter_table("ipo_enrichment_signals") as batch_op:
        batch_op.add_column(
            sa.Column(
                "authority_level",
                sa.String(length=24),
                nullable=False,
                server_default="advisory",
            )
        )
        batch_op.add_column(
            sa.Column(
                "corroborated",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(
            sa.Column(
                "authority_policy_version",
                sa.String(length=48),
                nullable=False,
                server_default="ipo-enrichment-authority-v1",
            )
        )
        batch_op.add_column(
            sa.Column(
                "batch_usability",
                sa.String(length=20),
                nullable=False,
                server_default="partial",
            )
        )
        batch_op.add_column(
            sa.Column("semantic_hash", sa.String(length=64), nullable=True)
        )
        batch_op.add_column(
            sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=True)
        )
        batch_op.add_column(
            sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True)
        )
    op.execute(
        sa.text(
            "UPDATE ipo_enrichment_signals "
            "SET first_seen_at = captured_at, last_seen_at = captured_at"
        )
    )
    with op.batch_alter_table("ipo_enrichment_signals") as batch_op:
        batch_op.alter_column(
            "first_seen_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.alter_column(
            "last_seen_at",
            existing_type=sa.DateTime(timezone=True),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_ipo_enrichment_signals_authority",
            "authority_level IN ('advisory', 'official', 'approved_manual')",
        )
        batch_op.create_check_constraint(
            "ck_ipo_enrichment_signals_batch_usability",
            "batch_usability IN ('usable', 'partial', 'not_evaluable')",
        )
        batch_op.create_check_constraint(
            "ck_ipo_enrichment_signals_semantic_hash",
            "semantic_hash IS NULL OR length(semantic_hash) = 64",
        )
    op.create_index(
        "ux_ipo_enrichment_signals_semantic",
        "ipo_enrichment_signals",
        ["issue_id", "signal_type", "semantic_hash"],
        unique=True,
        sqlite_where=sa.text("semantic_hash IS NOT NULL"),
        postgresql_where=sa.text("semantic_hash IS NOT NULL"),
    )


def downgrade() -> None:
    """Remove hardening columns only when document retention can be restored."""
    null_document_rows = op.get_bind().execute(
        sa.text(
            "SELECT COUNT(*) FROM ipo_extraction_proposals "
            "WHERE document_id IS NULL"
        )
    ).scalar_one()
    if null_document_rows:
        raise RuntimeError(
            "Refusing to discard retained proposal provenance during downgrade."
        )

    op.drop_index(
        "ux_ipo_enrichment_signals_semantic",
        table_name="ipo_enrichment_signals",
    )
    with op.batch_alter_table("ipo_enrichment_signals") as batch_op:
        batch_op.drop_constraint(
            "ck_ipo_enrichment_signals_semantic_hash", type_="check"
        )
        batch_op.drop_constraint(
            "ck_ipo_enrichment_signals_batch_usability", type_="check"
        )
        batch_op.drop_constraint(
            "ck_ipo_enrichment_signals_authority", type_="check"
        )
        for column in (
            "last_seen_at",
            "first_seen_at",
            "semantic_hash",
            "batch_usability",
            "authority_policy_version",
            "corroborated",
            "authority_level",
        ):
            batch_op.drop_column(column)

    op.drop_index("ux_ipo_scores_semantic_evaluation", table_name="ipo_scores")
    with op.batch_alter_table("ipo_scores") as batch_op:
        batch_op.drop_column("breakdown_json")

    op.drop_index(
        "ux_ipo_extraction_proposals_semantic",
        table_name="ipo_extraction_proposals",
    )
    op.drop_index(
        "ux_ipo_extraction_proposals_pending_document",
        table_name="ipo_extraction_proposals",
    )
    document_fk_name = _document_fk_name()
    with op.batch_alter_table(
        "ipo_extraction_proposals",
        naming_convention=_FK_NAMING,
    ) as batch_op:
        batch_op.drop_constraint(
            "ck_ipo_extraction_proposals_semantic_fingerprint", type_="check"
        )
        batch_op.drop_constraint(
            "ck_ipo_extraction_proposals_pending_document", type_="check"
        )
        batch_op.drop_constraint(document_fk_name, type_="foreignkey")
        batch_op.alter_column(
            "document_id",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch_op.create_foreign_key(
            "fk_ipo_extraction_proposals_document_id_ipo_documents",
            "ipo_documents",
            ["document_id"],
            ["id"],
            ondelete="CASCADE",
        )
        batch_op.drop_column("semantic_fingerprint")
        batch_op.drop_column("evidence_schema_version")
        batch_op.drop_column("document_url_snapshot")
