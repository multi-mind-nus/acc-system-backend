"""Use a named review preference instead of model-reported confidence thresholds."""

from alembic import op
import sqlalchemy as sa

revision = "0015_review_preference"
down_revision = "0014_retire_closed"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("collection_requests", sa.Column("review_preference", sa.String(16), nullable=False, server_default="STANDARD"))
    op.execute("""UPDATE collection_requests SET review_preference = CASE
        WHEN ai_request_action_threshold >= 0.995 THEN 'CAUTIOUS'
        WHEN ai_request_action_threshold <= 0.950 THEN 'EFFICIENT'
        ELSE 'STANDARD' END""")
    op.create_check_constraint("ck_collection_review_preference", "collection_requests", "review_preference IN ('CAUTIOUS', 'STANDARD', 'EFFICIENT')")


def downgrade():
    op.drop_constraint("ck_collection_review_preference", "collection_requests", type_="check")
    op.drop_column("collection_requests", "review_preference")
