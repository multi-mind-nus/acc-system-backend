"""Add tenant-scoped client bank accounts for F3.

Revision ID: 0003_f3_bank_accounts
Revises: 0002_b2_accounts
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_f3_bank_accounts"
down_revision: str | None = "0002_b2_accounts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "client_bank_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("client_id", sa.Uuid(), nullable=False),
        sa.Column("bank", sa.String(100), nullable=False),
        sa.Column("account_last4", sa.String(4), nullable=False),
        sa.Column("currency", sa.String(3), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "client_id"], ["clients.firm_id", "clients.id"]
        ),
        sa.CheckConstraint("account_last4 ~ '^[0-9]{4}$'"),
        sa.CheckConstraint("currency ~ '^[A-Z]{3}$'"),
        sa.CheckConstraint("status IN ('ACTIVE', 'DISABLED')"),
    )
    op.create_index(
        "ix_client_bank_accounts_firm_client", "client_bank_accounts",
        ["firm_id", "client_id"],
    )


def downgrade() -> None:
    op.drop_table("client_bank_accounts")
