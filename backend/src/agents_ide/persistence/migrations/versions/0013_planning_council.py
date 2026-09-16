"""Council: read-only plan preparation by multiple models."""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0013_planning_council"
down_revision = "0012_stage6a_harness_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "planning_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(32), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("chat_id", sa.String(32), sa.ForeignKey("chats.id"), nullable=True),
        sa.Column("initiator", sa.String(64), nullable=False, server_default="ui"),
        sa.Column(
            "state",
            sa.String(32),
            nullable=False,
        ),
        sa.Column("state_version", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("read_manifest_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("read_workspace_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("task_text", sa.Text(), nullable=False),
        sa.Column("context_snapshot_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("budget_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("usage_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("n_participants_requested", sa.Integer(), nullable=False),
        sa.Column("n_participants_actual", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("degraded", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("concurrency", sa.Integer(), nullable=False, server_default="2"),
        sa.Column("merged_by_member_id", sa.String(32), nullable=True),
        sa.Column("last_error_json", sa.Text(), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("updated_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_planning_jobs_idempotency"),
    )
    op.create_index("ix_planning_jobs_project", "planning_jobs", ["project_id"])
    op.create_index("ix_planning_jobs_state", "planning_jobs", ["state"])

    op.create_table(
        "planning_members",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("planning_jobs.id"), nullable=False),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("selection_json", sa.Text(), nullable=False),
        sa.Column("selection_kind", sa.String(16), nullable=False),
        sa.Column("group_id", sa.String(32), nullable=True),
        sa.Column("harness_profile_id", sa.String(32), nullable=True),
        sa.Column("provider_connection_id", sa.String(32), nullable=True),
        sa.Column("model_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("params_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("attempt_external_id", sa.String(128), nullable=True),
        sa.Column("draft_revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("error_json", sa.Text(), nullable=True),
        sa.Column("actual_member_id", sa.String(32), nullable=True),
        sa.Column("started_at", sa.Float(), nullable=True),
        sa.Column("finished_at", sa.Float(), nullable=True),
        sa.UniqueConstraint("job_id", "slot_index", name="uq_planning_members_slot"),
        sa.CheckConstraint("role IN ('participant','merger')", name="ck_planning_members_role"),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','unknown','skipped')",
            name="ck_planning_members_status",
        ),
    )
    op.create_index("ix_planning_members_job", "planning_members", ["job_id"])

    op.create_table(
        "planning_drafts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("planning_jobs.id"), nullable=False),
        sa.Column("member_id", sa.String(32), sa.ForeignKey("planning_members.id"), nullable=False),
        sa.Column("accepted", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("parse_status", sa.String(16), nullable=False, server_default="unparsed"),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("byte_length", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("content_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("omissions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("created_at", sa.Float(), nullable=False, server_default="0"),
        sa.UniqueConstraint("member_id", name="uq_planning_drafts_member"),
        sa.CheckConstraint(
            "parse_status IN ('unparsed','found','none_found','invalid_format')",
            name="ck_planning_drafts_parse",
        ),
    )

    op.create_table(
        "planning_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("planning_jobs.id"), nullable=False),
        sa.Column("revision_number", sa.Integer(), nullable=False),
        sa.Column("author", sa.String(16), nullable=False),
        sa.Column("body_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("body_truncated", sa.Boolean(), nullable=False, server_default=sa.text("0")),
        sa.Column("parse_status", sa.String(16), nullable=False, server_default="unparsed"),
        sa.Column("questions_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("questions_hash", sa.String(64), nullable=False, server_default=""),
        sa.Column("readiness", sa.String(16), nullable=False, server_default="unverified"),
        sa.Column("confirmation_hash", sa.String(64), nullable=True),
        sa.Column("answered_hash", sa.String(64), nullable=True),
        sa.Column("confirmed_at", sa.Float(), nullable=True),
        sa.Column("created_at", sa.Float(), nullable=False, server_default="0"),
        sa.UniqueConstraint("job_id", "revision_number", name="uq_planning_revisions_number"),
        sa.CheckConstraint("author IN ('merger','user')", name="ck_planning_revisions_author"),
        sa.CheckConstraint(
            "readiness IN ('ready','needs_answers','unverified','invalid_format')",
            name="ck_planning_revisions_readiness",
        ),
    )
    op.create_index("ix_planning_revisions_job", "planning_revisions", ["job_id"])

    op.create_table(
        "planning_answers",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column(
            "revision_id",
            sa.String(32),
            sa.ForeignKey("planning_revisions.id"),
            nullable=False,
        ),
        sa.Column("question_id", sa.String(64), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("selected_option_ids_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("free_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.Float(), nullable=False, server_default="0"),
        sa.UniqueConstraint(
            "revision_id", "question_id", name="uq_planning_answers_revision_question"
        ),
        sa.CheckConstraint("kind IN ('single','multi','text')", name="ck_planning_answers_kind"),
    )

    op.create_table(
        "planning_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("job_id", sa.String(32), sa.ForeignKey("planning_jobs.id"), nullable=False),
        sa.Column("member_id", sa.String(32), nullable=True),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.Float(), nullable=False, server_default="0"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.UniqueConstraint("job_id", "sequence", name="uq_planning_events_sequence"),
    )
    op.create_index("ix_planning_events_job", "planning_events", ["job_id"])


def downgrade() -> None:
    op.drop_index("ix_planning_events_job", table_name="planning_events")
    op.drop_table("planning_events")
    op.drop_table("planning_answers")
    op.drop_index("ix_planning_revisions_job", table_name="planning_revisions")
    op.drop_table("planning_revisions")
    op.drop_table("planning_drafts")
    op.drop_index("ix_planning_members_job", table_name="planning_members")
    op.drop_table("planning_members")
    op.drop_index("ix_planning_jobs_state", table_name="planning_jobs")
    op.drop_index("ix_planning_jobs_project", table_name="planning_jobs")
    op.drop_table("planning_jobs")
