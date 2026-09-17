"""Persist native Council process ownership and session/event accounting."""

import sqlalchemy as sa
from alembic import op

revision = "0019_planning_native"
down_revision = "0018_native_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "planning_attempts",
        sa.Column("runtime_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("planning_attempts", "runtime_json")
