"""Persist optional availability schedules on model group members."""

import sqlalchemy as sa
from alembic import op

revision = "0020_model_schedules"
down_revision = "0019_planning_native"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("model_group_members", sa.Column("schedule_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("model_group_members", "schedule_json")
