"""Preserve import provenance; old versions and snapshots are unchanged."""

import sqlalchemy as sa
from alembic import op

revision = "0006_graph_review"
down_revision = "0005_model_group_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "pipeline_versions",
        sa.Column("origin", sa.String(16), nullable=False, server_default="local"),
    )


def downgrade() -> None:
    op.drop_column("pipeline_versions", "origin")
