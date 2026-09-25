"""Add client industry; existing clients default to Other."""

from alembic import op
import sqlalchemy as sa

revision = "0012_client_industry"
down_revision = "0011_in_app_notifications"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("clients", sa.Column("industry", sa.String(40), nullable=False, server_default="OTHER"))


def downgrade():
    op.drop_column("clients", "industry")
