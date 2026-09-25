"""Add per-user in-app notifications."""

from alembic import op
import sqlalchemy as sa


revision = "0011_in_app_notifications"
down_revision = "0010_classification_confirmation"
branch_labels = None
depends_on = None


def upgrade():
    op.create_unique_constraint(
        "uq_workflow_events_firm_id_id", "workflow_events", ["firm_id", "id"]
    )
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("firm_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["firm_id"], ["firms.id"]),
        sa.ForeignKeyConstraint(
            ["firm_id", "user_id"], ["users.firm_id", "users.id"]
        ),
        sa.ForeignKeyConstraint(
            ["firm_id", "event_id"],
            ["workflow_events.firm_id", "workflow_events.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "event_id", name="uq_notifications_user_event"
        ),
    )
    op.create_index(
        "ix_notifications_user_created",
        "notifications",
        ["user_id", "created_at"],
    )


def downgrade():
    op.drop_index("ix_notifications_user_created", table_name="notifications")
    op.drop_table("notifications")
    op.drop_constraint(
        "uq_workflow_events_firm_id_id", "workflow_events", type_="unique"
    )
