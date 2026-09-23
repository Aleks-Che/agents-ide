"""Seek attempt processes without scanning every prior process at each stream flush."""

from alembic import op

revision = "0027_process_attempt_index"
down_revision = "0026_independent_workspaces"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This additive index can also be installed online before a service restart.
    # IF NOT EXISTS lets the next normal migration adopt that repair safely.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_process_attempt ON process_supervision (step_attempt_id)"
    )


def downgrade() -> None:
    op.drop_index("ix_process_attempt", table_name="process_supervision")
