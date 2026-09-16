"""Explicit Council credential recovery without changing candidate snapshots."""

import sqlalchemy as sa
from alembic import op

revision = "0015_council_retry"
down_revision = "0014_council_safety"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planning_members",
        sa.Column("access_overrides_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("planning_members", "access_overrides_json")
