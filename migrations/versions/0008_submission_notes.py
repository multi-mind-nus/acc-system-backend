"""Add optional submission notes.

Revision ID: 0008_submission_notes
Revises: 0007_b5_review
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_submission_notes"
down_revision: str | None = "0007_b5_review"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("submissions", sa.Column("note", sa.Text()))


def downgrade() -> None:
    op.drop_column("submissions", "note")
