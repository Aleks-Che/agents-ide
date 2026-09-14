"""Stage 5 controls: process registry, attempt heartbeat, reconciliation.

The stage 5 review adds the building blocks required by recovery and
process supervision without rewriting earlier rows. The legacy schema
remains valid; new columns default to safe values so old attempts keep
their original semantics.

* ``step_attempts.heartbeat_at`` separates the adapter call heartbeat
  from the worker heartbeat so a slow adapter cannot mask a dead worker
  or vice versa.
* ``process_supervision`` records every owned child process: PID, OS
  start time, parent PID, owner generation, role and the Run/attempt
  it belongs to. Cleanup never touches foreign PIDs or generations.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0008_stage5_controls"
down_revision = "0007_engine_review"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "step_attempts",
        sa.Column("heartbeat_at", sa.Float()),
    )
    op.create_table(
        "process_supervision",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), sa.ForeignKey("runs.id"), nullable=False),
        sa.Column("step_attempt_id", sa.String(32), sa.ForeignKey("step_attempts.id")),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("owner_generation", sa.Integer(), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("create_time", sa.Float(), nullable=False),
        sa.Column("parent_pid", sa.Integer()),
        sa.Column("executable", sa.String(512)),
        sa.Column("kind", sa.String(32), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("interrupt_requested_at", sa.Float()),
        sa.Column("killed_at", sa.Float()),
        sa.Column("last_external_event_at", sa.Float()),
        sa.Column("finished_at", sa.Float()),
        sa.CheckConstraint(
            "state IN ('started','interrupt_requested','killed','finished','unknown')",
            name="ck_process_state",
        ),
        sa.CheckConstraint(
            "kind IN ('harness','llm','command','git','support')",
            name="ck_process_kind",
        ),
    )
    op.create_index("ix_process_run", "process_supervision", ["run_id", "state"])
    op.create_index("ix_process_owner", "process_supervision", ["owner_generation", "pid"])


def downgrade() -> None:
    op.drop_index("ix_process_owner", table_name="process_supervision")
    op.drop_index("ix_process_run", table_name="process_supervision")
    op.drop_table("process_supervision")
    op.drop_column("step_attempts", "heartbeat_at")
