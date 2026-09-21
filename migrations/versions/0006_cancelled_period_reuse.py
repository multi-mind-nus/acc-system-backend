"""Allow recreating a collection request after cancellation.

Revision ID: 0006_cancelled_period_reuse
Revises: 0005_b4_portal_documents
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_cancelled_period_reuse"
down_revision: str | None = "0005_b4_portal_documents"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "collection_requests_firm_id_client_id_period_key",
        "collection_requests",
        type_="unique",
    )
    op.create_index(
        "uq_collection_requests_active_period",
        "collection_requests",
        ["firm_id", "client_id", "period"],
        unique=True,
        postgresql_where=sa.text("status <> 'CANCELLED'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_collection_requests_active_period",
        table_name="collection_requests",
    )
    op.create_unique_constraint(
        "collection_requests_firm_id_client_id_period_key",
        "collection_requests",
        ["firm_id", "client_id", "period"],
    )
