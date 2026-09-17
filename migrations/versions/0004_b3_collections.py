"""Add B3 collection requests, requirements, events and idempotency.

Revision ID: 0004_b3_collections
Revises: 0003_f3_bank_accounts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0004_b3_collections"
down_revision: str | None = "0003_f3_bank_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "collection_requests",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("scope_note", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=False),
        sa.Column("assignee_id", sa.Uuid(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("approved_by", sa.Uuid()),
        sa.Column("approved_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT', 'OPEN', 'IN_REVIEW', 'CHANGES_REQUESTED', "
            "'READY_FOR_BOOKKEEPING', 'CLOSED', 'CANCELLED')"
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.ForeignKeyConstraint(["approved_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        sa.ForeignKeyConstraint(
            ["firm_id", "assignee_id"],
            ["firm_members.firm_id", "firm_members.user_id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "client_id", "period"),
        sa.UniqueConstraint("firm_id", "id"),
    )
    op.create_index(
        "ix_collection_requests_dashboard", "collection_requests",
        ["firm_id", "status", "due_at"],
    )
    op.create_table(
        "requirements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("required", sa.Boolean(), nullable=False),
        sa.Column(
            "criteria", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("issue_code", sa.String(32)),
        sa.Column("client_message", sa.Text()),
        sa.Column("internal_note", sa.Text()),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("reviewed_by", sa.Uuid()),
        sa.Column("reviewed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("origin IN ('INITIAL', 'FOLLOW_UP')"),
        sa.CheckConstraint(
            "status IN ('PENDING', 'RECEIVED', 'NEEDS_ACTION', 'SATISFIED', 'WAIVED')"
        ),
        sa.CheckConstraint(
            "issue_code IS NULL OR issue_code IN ('MISSING', 'WRONG_PERIOD', "
            "'ENTITY_MISMATCH', 'UNREADABLE', 'INCOMPLETE', 'OTHER')"
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["reviewed_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "id"),
    )
    op.create_index("ix_requirements_request", "requirements", ["request_id"])
    op.create_table(
        "workflow_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column(
            "payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "request_id"],
            ["collection_requests.firm_id", "collection_requests.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_workflow_events_request_created", "workflow_events",
        ["request_id", "created_at"],
    )
    op.create_table(
        "idempotency_records",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("method", sa.String(8), nullable=False),
        sa.Column("path", sa.String(300), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("status_code", sa.Integer()),
        sa.Column(
            "response_body", postgresql.JSONB(astext_type=sa.Text())
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.CheckConstraint("status IN ('PROCESSING', 'COMPLETED')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "actor_id", "key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("workflow_events")
    op.drop_table("requirements")
    op.drop_table("collection_requests")
