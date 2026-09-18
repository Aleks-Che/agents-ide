"""Keep live Council ownership despite delayed heartbeat writes."""

import sqlalchemy as sa
from alembic import op

revision = "0024_planning_owner"
down_revision = "0023_worktree_reservations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("planning_jobs", sa.Column("owner_pid", sa.Integer(), nullable=True))
    op.add_column("planning_jobs", sa.Column("owner_create_time", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("planning_jobs", "owner_create_time")
    op.drop_column("planning_jobs", "owner_pid")
