"""Merge retired CLOSED requests into confirmed requests; preserve audit history."""
from alembic import op

revision = "0014_retire_closed"
down_revision = "0013_multiple_period_requests"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("UPDATE collection_requests SET status = 'READY_FOR_BOOKKEEPING' WHERE status = 'CLOSED'")


def downgrade():
    # The merged business states cannot be distinguished safely after migration.
    pass
