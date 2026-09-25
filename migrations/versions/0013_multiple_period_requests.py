"""Allow independent collection requests for the same client and period."""
from alembic import op
import sqlalchemy as sa

revision = "0013_multiple_period_requests"
down_revision = "0012_client_industry"
branch_labels = None
depends_on = None


def upgrade():
    op.drop_index("uq_collection_requests_active_period", table_name="collection_requests")
    op.create_index("ix_collection_requests_client_period", "collection_requests", ["firm_id", "client_id", "period"])


def downgrade():
    # Duplicate requests must be resolved explicitly before restoring uniqueness.
    op.create_index("uq_collection_requests_active_period", "collection_requests", ["firm_id", "client_id", "period"], unique=True, postgresql_where=sa.text("status <> 'CANCELLED'"))
    op.drop_index("ix_collection_requests_client_period", table_name="collection_requests")
