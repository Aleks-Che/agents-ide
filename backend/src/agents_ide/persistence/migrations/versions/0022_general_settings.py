"""Persist general application defaults."""

import sqlalchemy as sa
from alembic import op

revision = "0022_general_settings"
down_revision = "0021_council_defaults"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "general_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("settings_json", sa.Text(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("general_settings")
