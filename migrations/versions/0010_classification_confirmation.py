"""Track classification confirmation independently from analysis status."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010_classification_confirmation"
down_revision = "0009_b6_ai_baseline"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("ai_runs", sa.Column("confirmed_at", sa.DateTime(timezone=True)))
    op.add_column("ai_runs", sa.Column("confirmation", JSONB()))


def downgrade():
    op.drop_column("ai_runs", "confirmation")
    op.drop_column("ai_runs", "confirmed_at")
