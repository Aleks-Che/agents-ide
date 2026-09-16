"""Council ownership, pinned candidates, attempts and complete revisions."""

import sqlalchemy as sa
from alembic import op

revision = "0014_council_safety"
down_revision = "0013_planning_council"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for column in (
        sa.Column("request_hash", sa.String(64)),
        sa.Column("lease_owner", sa.String(64)),
        sa.Column("lease_expires_at", sa.Float()),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="0"),
    ):
        op.add_column("planning_jobs", column)
    op.add_column(
        "planning_members",
        sa.Column("candidates_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.add_column(
        "planning_members",
        sa.Column("candidate_index", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "planning_revisions", sa.Column("plan_json", sa.Text(), nullable=False, server_default="{}")
    )
    op.add_column(
        "planning_revisions",
        sa.Column("answers_json", sa.Text(), nullable=False, server_default="[]"),
    )
    op.create_table(
        "planning_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("planning_jobs.id"), nullable=False),
        sa.Column("member_id", sa.String(32), sa.ForeignKey("planning_members.id"), nullable=False),
        sa.Column("attempt_index", sa.Integer(), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("candidate_json", sa.Text(), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("error_code", sa.String(64)),
        sa.Column("started_at", sa.Float(), nullable=False),
        sa.Column("finished_at", sa.Float()),
        sa.UniqueConstraint("member_id", "attempt_index"),
    )
    for action in ("UPDATE", "DELETE"):
        op.execute(f"""
            CREATE TRIGGER planning_confirmed_no_{action.lower()}
            BEFORE {action} ON planning_revisions WHEN OLD.confirmed_at IS NOT NULL
            BEGIN SELECT RAISE(ABORT, 'confirmed_planning_revision_immutable'); END
        """)


def downgrade() -> None:
    op.execute("DROP TRIGGER planning_confirmed_no_update")
    op.execute("DROP TRIGGER planning_confirmed_no_delete")
    op.drop_table("planning_attempts")
    for table, columns in (
        ("planning_revisions", ("plan_json", "answers_json")),
        ("planning_members", ("candidates_json", "candidate_index")),
        ("planning_jobs", ("request_hash", "lease_owner", "lease_expires_at", "generation")),
    ):
        for column in columns:
            op.drop_column(table, column)
