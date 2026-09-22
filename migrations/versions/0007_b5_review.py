"""Add B5 review decisions and evidence relations.

Revision ID: 0007_b5_review
Revises: 0006_cancelled_period_reuse
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_b5_review"
down_revision: str | None = "0006_cancelled_period_reuse"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "requirement_documents",
        sa.Column("relation", sa.String(16), server_default="SUPPORTS", nullable=False),
    )
    op.create_check_constraint(
        "ck_requirement_documents_relation",
        "requirement_documents",
        "relation IN ('SUPPORTS', 'CONTRADICTS', 'REFERENCE')",
    )
    op.create_table(
        "review_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("issue_code", sa.String(32)),
        sa.Column("client_message", sa.Text()),
        sa.Column("internal_note", sa.Text()),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("decision IN ('SATISFY', 'REQUEST_ACTION', 'WAIVE')"),
        sa.CheckConstraint(
            "issue_code IS NULL OR issue_code IN ('MISSING', 'WRONG_PERIOD', "
            "'ENTITY_MISMATCH', 'UNREADABLE', 'INCOMPLETE', 'OTHER')"
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"]),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_review_decisions_requirement",
        "review_decisions",
        ["requirement_id", "created_at"],
    )
    op.create_table(
        "review_decision_documents",
        sa.Column("decision_id", sa.Uuid(), nullable=False),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(16), nullable=False),
        sa.CheckConstraint("relation IN ('SUPPORTS', 'CONTRADICTS', 'REFERENCE')"),
        sa.ForeignKeyConstraint(["decision_id"], ["review_decisions.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.PrimaryKeyConstraint("decision_id", "document_id"),
    )


def downgrade() -> None:
    op.drop_table("review_decision_documents")
    op.drop_index("ix_review_decisions_requirement", table_name="review_decisions")
    op.drop_table("review_decisions")
    op.drop_constraint(
        "ck_requirement_documents_relation",
        "requirement_documents",
        type_="check",
    )
    op.drop_column("requirement_documents", "relation")
