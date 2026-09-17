"""Persist the default Council composition shared by all projects."""

import sqlalchemy as sa
from alembic import op

revision = "0021_council_defaults"
down_revision = "0020_model_schedules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_council_defaults",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("participants_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("planning_council_defaults")
