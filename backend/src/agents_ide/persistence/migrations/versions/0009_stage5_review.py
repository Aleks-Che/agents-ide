"""Owner identity and process-tree evidence for safe recovery of stage 5 data."""

import sqlalchemy as sa
from alembic import op

revision = "0009_stage5_review"
down_revision = "0008_stage5_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("queue_jobs", sa.Column("owner_pid", sa.Integer()))
    op.add_column("queue_jobs", sa.Column("owner_create_time", sa.Float()))
    op.add_column("process_supervision", sa.Column("tree_json", sa.Text()))
    op.add_column("process_supervision", sa.Column("last_health_ok", sa.Float()))
    op.add_column("process_supervision", sa.Column("transport", sa.String(32)))
    op.add_column("process_supervision", sa.Column("port", sa.Integer()))
    op.add_column("process_supervision", sa.Column("workspace_json", sa.Text()))


def downgrade() -> None:
    for column in ("workspace_json", "port", "transport", "last_health_ok", "tree_json"):
        op.drop_column("process_supervision", column)
    op.drop_column("queue_jobs", "owner_create_time")
    op.drop_column("queue_jobs", "owner_pid")
