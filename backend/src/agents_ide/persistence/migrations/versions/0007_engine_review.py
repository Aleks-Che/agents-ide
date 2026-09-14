"""Durable engine checkpoints, candidate identity and artifact content."""

import sqlalchemy as sa
from alembic import op

revision = "0007_engine_review"
down_revision = "0006_graph_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("runtime_json", sa.Text(), nullable=False, server_default="{}"))
    op.add_column(
        "step_attempts", sa.Column("selection_json", sa.Text(), nullable=False, server_default="{}")
    )
    op.add_column("step_attempts", sa.Column("request_artifact_id", sa.String(32)))
    op.add_column("step_attempts", sa.Column("result_artifact_id", sa.String(32)))
    op.add_column("artifact_manifests", sa.Column("body_json", sa.Text()))


def downgrade() -> None:
    op.drop_column("artifact_manifests", "body_json")
    for name in ("result_artifact_id", "request_artifact_id", "selection_json"):
        op.drop_column("step_attempts", name)
    op.drop_column("runs", "runtime_json")
