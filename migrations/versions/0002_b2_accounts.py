"""Create B2 account, tenant, and authentication tables.

Revision ID: 0002_b2_accounts
Revises: 0001_b1_baseline
Create Date: 2026-09-17
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_b2_accounts"
down_revision: str | None = "0001_b1_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "firms",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("password_hash", sa.String(512), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "id"),
    )
    op.create_index(
        "uq_users_email_lower", "users", [sa.text("lower(email)")], unique=True
    )
    op.create_table(
        "firm_members",
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('FIRM_ADMIN', 'ACCOUNTANT')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "user_id"], ["users.firm_id", "users.id"]
        ),
        sa.PrimaryKeyConstraint("firm_id", "user_id"),
        sa.UniqueConstraint("user_id"),
    )
    op.create_table(
        "clients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("code", sa.String(50), nullable=False),
        sa.Column("legal_name", sa.String(200), nullable=False),
        sa.Column("base_currency", sa.String(3), nullable=False),
        sa.Column(
            "features",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("firm_id", "code"),
        sa.UniqueConstraint("firm_id", "id"),
    )
    op.create_table(
        "client_members",
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("role IN ('CLIENT_ADMIN', 'CLIENT_SUBMITTER')"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        sa.ForeignKeyConstraint(
            ["firm_id", "user_id"], ["users.firm_id", "users.id"]
        ),
        sa.PrimaryKeyConstraint("firm_id", "client_id", "user_id"),
    )
    op.create_table(
        "client_assignments",
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        sa.ForeignKeyConstraint(
            ["firm_id", "user_id"],
            ["firm_members.firm_id", "firm_members.user_id"],
        ),
        sa.PrimaryKeyConstraint("firm_id", "client_id", "user_id"),
    )
    op.create_table(
        "user_invites",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid()),
        sa.Column("email", sa.String(320), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("invited_by", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("accepted_at", sa.DateTime(timezone=True)),
        sa.Column("revoked_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint("scope IN ('FIRM', 'CLIENT')"),
        sa.CheckConstraint(
            "role IN ('FIRM_ADMIN', 'ACCOUNTANT', 'CLIENT_ADMIN', 'CLIENT_SUBMITTER')"
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(["invited_by"], ["users.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "password_reset_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid()),
        sa.Column("actor_id", sa.Uuid()),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("target_type", sa.String(64)),
        sa.Column("target_id", sa.Uuid()),
        sa.Column(
            "details",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.PrimaryKeyConstraint("id"),
    )


def downgrade() -> None:
    op.drop_table("audit_events")
    op.drop_table("password_reset_tokens")
    op.drop_table("user_invites")
    op.drop_table("client_assignments")
    op.drop_table("client_members")
    op.drop_table("clients")
    op.drop_table("firm_members")
    op.drop_index("uq_users_email_lower", table_name="users")
    op.drop_table("users")
    op.drop_table("firms")
