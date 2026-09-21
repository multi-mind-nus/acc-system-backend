"""Add client submissions and isolated documents.

Revision ID: 0005_b4_portal_documents
Revises: 0004_b3_collections
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_b4_portal_documents"
down_revision: str | None = "0004_b3_collections"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("round_no", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('DRAFT', 'SUBMITTED')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("request_id", "round_no"),
    )
    op.create_index("ix_submissions_request_status", "submissions", ["request_id", "status"])
    op.create_index(
        "uq_submissions_one_draft",
        "submissions",
        ["request_id"],
        unique=True,
        postgresql_where=sa.text("status = 'DRAFT'"),
    )

    op.create_table(
        "documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("uploader_id", sa.Uuid(), nullable=False),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("content_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("storage_key", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("failure_code", sa.String(64)),
        sa.Column("locked_by", sa.String(100)),
        sa.Column("locked_until", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("status IN ('QUARANTINED', 'AVAILABLE', 'FAILED')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["uploader_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["firm_id", "client_id"], ["clients.firm_id", "clients.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_documents_scan_queue", "documents", ["status", "next_attempt_at"])
    op.create_index("ix_documents_client_sha", "documents", ["firm_id", "client_id", "sha256"])

    op.create_table(
        "requirement_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("requirement_id", sa.Uuid()),
        sa.Column("document_id", sa.Uuid(), nullable=False),
        sa.Column("document_type", sa.String(64), nullable=False),
        sa.Column("excluded_at", sa.DateTime(timezone=True)),
        sa.Column("excluded_by", sa.Uuid()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"]),
        sa.ForeignKeyConstraint(["requirement_id"], ["requirements.id"]),
        sa.ForeignKeyConstraint(["document_id"], ["documents.id"]),
        sa.ForeignKeyConstraint(["excluded_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_requirement_documents_submission", "requirement_documents", ["submission_id"])
    op.create_index("ix_requirement_documents_document", "requirement_documents", ["document_id"])


def downgrade() -> None:
    op.drop_table("requirement_documents")
    op.drop_table("documents")
    op.drop_table("submissions")
