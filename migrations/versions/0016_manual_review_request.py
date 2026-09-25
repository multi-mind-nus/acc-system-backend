"""Allow a client to route an AI-disputed resubmission to an accountant."""

from alembic import op
import sqlalchemy as sa

revision = "0016_manual_review_request"
down_revision = "0015_review_preference"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "submissions",
        sa.Column(
            "manual_review_requested",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade():
    op.drop_column("submissions", "manual_review_requested")
