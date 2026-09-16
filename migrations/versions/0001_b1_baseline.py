"""Create the B1 deployment baseline.

Revision ID: 0001_b1_baseline
Revises:
Create Date: 2026-09-17
"""

from collections.abc import Sequence

revision: str = "0001_b1_baseline"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
