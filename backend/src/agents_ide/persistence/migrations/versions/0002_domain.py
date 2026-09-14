"""Project, chat, message, template, version, binding, connections and run tables."""

import sqlalchemy as sa
from alembic import op

revision = "0002_domain"
down_revision = "0001_foundation"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ----- Projects ---------------------------------------------------------
    op.create_table(
        "projects",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("workspace_entered_path", sa.String(1024), nullable=False),
        sa.Column("workspace_normalized_path", sa.String(1024), nullable=False),
        sa.Column("workspace_identity_dev", sa.BigInteger, nullable=False),
        sa.Column("workspace_identity_ino", sa.BigInteger, nullable=False),
        sa.Column("git_root_path", sa.String(1024)),
        sa.Column("git_head_sha", sa.String(64)),
        sa.Column("git_remote_url", sa.String(512)),
        sa.Column("git_default_branch", sa.String(256)),
        sa.Column("git_dirty", sa.Boolean, nullable=False, server_default=sa.text("0")),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.UniqueConstraint(
            "workspace_identity_dev", "workspace_identity_ino", name="uq_projects_identity"
        ),
    )
    op.create_index("ix_projects_archived_at", "projects", ["archived_at"])

    # ----- Chats and messages -----------------------------------------------
    op.create_table(
        "chats",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("title", sa.String(256), nullable=False),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "title", name="uq_chats_project_title"),
    )
    op.create_index("ix_chats_project_archived", "chats", ["project_id", "archived_at"])

    op.create_table(
        "messages",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("chat_id", sa.String(32), nullable=False),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["chat_id"], ["chats.id"], ondelete="CASCADE"),
        sa.CheckConstraint("role IN ('user', 'assistant', 'system', 'note')"),
    )
    op.create_index("ix_messages_chat_created", "messages", ["chat_id", "created_at"])

    # ----- Templates, versions, bindings ------------------------------------
    op.create_table(
        "pipeline_templates",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("description", sa.Text, nullable=False, server_default=""),
        sa.Column("kind", sa.String(16), nullable=False, server_default="user"),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("kind IN ('user', 'system')"),
    )
    op.create_index("ix_templates_archived", "pipeline_templates", ["archived_at"])

    op.create_table(
        "pipeline_versions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("template_id", sa.String(32), nullable=False),
        sa.Column("version_number", sa.Integer, nullable=False),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("required_features_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("graph_json", sa.Text, nullable=False),
        sa.Column("execution_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("inputs_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["template_id"], ["pipeline_templates.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("template_id", "version_number", name="uq_versions_template_number"),
        sa.UniqueConstraint("template_id", "execution_hash", name="uq_versions_template_hash"),
    )

    op.create_table(
        "pipeline_bindings",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("version_id", sa.String(32), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("role_assignments_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("model_overrides_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("limit_overrides_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("command_filter_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("branch_policy", sa.String(16), nullable=False, server_default="run_branch"),
        sa.Column("dirty_policy", sa.String(16), nullable=False, server_default="strict"),
        sa.Column("archived_at", sa.Float),
        sa.Column("revision", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["version_id"], ["pipeline_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("project_id", "name", name="uq_bindings_project_name"),
        sa.CheckConstraint("branch_policy IN ('run_branch', 'current')"),
        sa.CheckConstraint("dirty_policy IN ('strict', 'allow_nonoverlap')"),
    )
    op.create_index("ix_bindings_archived", "pipeline_bindings", ["archived_at"])

    # ----- Provider connections ---------------------------------------------
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("provider_kind", sa.String(32), nullable=False),
        sa.Column("base_url", sa.String(512), nullable=False),
        sa.Column("protocol", sa.String(8), nullable=False),
        sa.Column("secret_reference", sa.String(64)),
        sa.Column("manual_models_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("catalog_models_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("catalog_fetched_at", sa.Float),
        sa.Column("catalog_ttl_seconds", sa.Integer, nullable=False, server_default="900"),
        sa.Column("last_test_status", sa.String(16)),
        sa.Column("last_test_at", sa.Float),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("protocol IN ('http', 'https')"),
        sa.CheckConstraint("provider_kind IN ('openai_compatible', 'loopback')"),
        sa.UniqueConstraint("name", name="uq_provider_name"),
    )
    op.create_index("ix_providers_archived", "provider_connections", ["archived_at"])

    # ----- Harness profiles -------------------------------------------------
    op.create_table(
        "harness_profiles",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("harness_kind", sa.String(32), nullable=False),
        sa.Column("executable_path", sa.String(512)),
        sa.Column("settings_json", sa.Text, nullable=False, server_default="{}"),
        sa.Column("archived_at", sa.Float),
        sa.Column("version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.CheckConstraint("harness_kind IN ('codex', 'opencode')"),
        sa.UniqueConstraint("name", name="uq_harness_name"),
    )
    op.create_index("ix_harness_archived", "harness_profiles", ["archived_at"])

    # ----- Runs -------------------------------------------------------------
    op.create_table(
        "runs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("idempotency_key", sa.String(64), nullable=False),
        sa.Column("project_id", sa.String(32), nullable=False),
        sa.Column("chat_id", sa.String(32)),
        sa.Column("pipeline_version_id", sa.String(32), nullable=False),
        sa.Column("binding_id", sa.String(32), nullable=False),
        sa.Column("state", sa.String(20), nullable=False),
        sa.Column("state_version", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("schema_version", sa.String(32), nullable=False),
        sa.Column("execution_hash", sa.String(64), nullable=False),
        sa.Column("policy_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_json", sa.Text, nullable=False),
        sa.Column("resolved_settings_json", sa.Text, nullable=False),
        sa.Column("current_node_id", sa.String(64)),
        sa.Column("current_execution_id", sa.String(32)),
        sa.Column("current_attempt_id", sa.String(32)),
        sa.Column("current_cycle_id", sa.Integer),
        sa.Column("worker_id", sa.String(32)),
        sa.Column("worker_generation", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("waiting_reason_json", sa.Text),
        sa.Column("resume_target_json", sa.Text),
        sa.Column("stop_goal", sa.String(16)),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("updated_at", sa.Float, nullable=False),
        sa.Column("started_at", sa.Float),
        sa.Column("finished_at", sa.Float),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["pipeline_version_id"], ["pipeline_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["binding_id"], ["pipeline_bindings.id"], ondelete="RESTRICT"),
        sa.UniqueConstraint("idempotency_key", name="uq_runs_idempotency"),
        sa.CheckConstraint(
            "state IN ('queued','running','pause_requested','paused','stop_requested',"
            "'stopped','retry_wait','waiting_input','recovering','completed','failed','cancelled')"
        ),
    )
    op.create_index("ix_runs_project_created", "runs", ["project_id", "created_at"])
    op.create_index("ix_runs_state", "runs", ["state"])

    # ----- Step executions and attempts -------------------------------------
    op.create_table(
        "step_executions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("node_id", sa.String(64), nullable=False),
        sa.Column("visit_index", sa.Integer, nullable=False),
        sa.Column("cycle_id", sa.Integer, nullable=False),
        sa.Column("scope", sa.String(64)),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float),
        sa.Column("raw_result_ref", sa.String(64)),
        sa.Column("validated_result_json", sa.Text),
        sa.Column("decision", sa.String(8)),
        sa.Column("evidence_manifest_id", sa.String(32)),
        sa.Column("plan_item_ids_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("attempt_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint(
            "run_id",
            "node_id",
            "visit_index",
            "cycle_id",
            name="uq_executions_run_node_visit_cycle",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped',"
            "'retry_wait','waiting_input','interrupted')"
        ),
    )
    op.create_index("ix_executions_run_status", "step_executions", ["run_id", "status"])

    op.create_table(
        "step_attempts",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("execution_id", sa.String(32), nullable=False),
        sa.Column("attempt_index", sa.Integer, nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("operation_id", sa.String(64)),
        sa.Column("retry_safety", sa.String(16), nullable=False, server_default="unknown"),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float),
        sa.Column("external_outcome", sa.String(16)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("error_details_json", sa.Text),
        sa.Column("tokens_used", sa.Integer),
        sa.Column("cost_estimated", sa.Float),
        sa.Column("budget_quality", sa.String(16)),
        sa.ForeignKeyConstraint(["execution_id"], ["step_executions.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("execution_id", "attempt_index", name="uq_attempts_execution_index"),
        sa.CheckConstraint(
            "status IN ('prepared','running','succeeded','failed','interrupted',"
            "'unknown','waiting_input')"
        ),
        sa.CheckConstraint("retry_safety IN ('safe','unsafe','unknown')"),
    )

    # ----- Agent sessions ---------------------------------------------------
    op.create_table(
        "agent_sessions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("attempt_id", sa.String(32), nullable=False),
        sa.Column("harness_kind", sa.String(32), nullable=False),
        sa.Column("role", sa.String(64), nullable=False),
        sa.Column("external_session_id", sa.String(128)),
        sa.Column("external_turn_id", sa.String(128)),
        sa.Column("cwd_identity_dev", sa.BigInteger),
        sa.Column("cwd_identity_ino", sa.BigInteger),
        sa.Column("process_id", sa.String(32)),
        sa.Column("resume_count", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("started_at", sa.Float, nullable=False),
        sa.Column("finished_at", sa.Float),
        sa.Column("last_external_event_at", sa.Float),
        sa.Column("last_message_preview", sa.Text),
        sa.ForeignKeyConstraint(["attempt_id"], ["step_attempts.id"], ondelete="CASCADE"),
    )

    # ----- Run events -------------------------------------------------------
    op.create_table(
        "run_events",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("event_version", sa.Integer, nullable=False, server_default=sa.text("1")),
        sa.Column("type", sa.String(64), nullable=False),
        sa.Column("occurred_at", sa.Float, nullable=False),
        sa.Column("persisted_at", sa.Float, nullable=False),
        sa.Column("node_id", sa.String(64)),
        sa.Column("step_execution_id", sa.String(32)),
        sa.Column("step_attempt_id", sa.String(32)),
        sa.Column("agent_session_id", sa.String(32)),
        sa.Column("command_id", sa.String(64)),
        sa.Column("worker_generation", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("payload_json", sa.Text, nullable=False, server_default="{}"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),
    )
    op.create_index("ix_run_events_run_sequence", "run_events", ["run_id", "sequence"])
    op.create_index("ix_run_events_type", "run_events", ["type"])

    # ----- Artifact manifests ----------------------------------------------
    op.create_table(
        "artifact_manifests",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("schema_type", sa.String(64), nullable=False),
        sa.Column("byte_length", sa.BigInteger, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("source_kind", sa.String(32), nullable=False),
        sa.Column("source_ref", sa.String(256)),
        sa.Column("step_execution_id", sa.String(32)),
        sa.Column("step_attempt_id", sa.String(32)),
        sa.Column("cycle_id", sa.Integer),
        sa.Column("plan_item_ids_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("files_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("base_head_sha", sa.String(64)),
        sa.Column("current_head_sha", sa.String(64)),
        sa.Column("redaction_json", sa.Text),
        sa.Column("truncation_json", sa.Text),
        sa.Column("omissions_json", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
    )

    # ----- Queue jobs -------------------------------------------------------
    op.create_table(
        "queue_jobs",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("available_at", sa.Float, nullable=False),
        sa.Column("claimed_by", sa.String(32)),
        sa.Column("lease_expires_at", sa.Float),
        sa.Column("generation", sa.Integer, nullable=False, server_default=sa.text("0")),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", name="uq_queue_jobs_run"),
    )
    op.create_index("ix_queue_jobs_available", "queue_jobs", ["available_at", "claimed_by"])

    # ----- Command journal --------------------------------------------------
    op.create_table(
        "command_journal",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("command_id", sa.String(64), nullable=False),
        sa.Column("command_type", sa.String(32), nullable=False),
        sa.Column("expected_state_version", sa.Integer, nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("sequence", sa.Integer, nullable=False),
        sa.Column("initiator", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("response_json", sa.Text),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("applied_at", sa.Float),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "command_id", name="uq_journal_run_command"),
    )
    op.create_index("ix_command_journal_run", "command_journal", ["run_id", "sequence"])

    # ----- Workspace reservations -------------------------------------------
    op.create_table(
        "workspace_reservations",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("workspace_identity_dev", sa.BigInteger, nullable=False),
        sa.Column("workspace_identity_ino", sa.BigInteger, nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("owner_generation", sa.Integer, nullable=False),
        sa.Column("lease_expires_at", sa.Float),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.Column("released_at", sa.Float),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
    )
    op.create_index(
        "ix_reservations_identity_active",
        "workspace_reservations",
        ["workspace_identity_dev", "workspace_identity_ino", "released_at"],
    )

    # ----- Plan items -------------------------------------------------------
    op.create_table(
        "plan_items",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("item_id", sa.String(64), nullable=False),
        sa.Column("order_index", sa.Integer, nullable=False),
        sa.Column("title", sa.Text, nullable=False),
        sa.Column("acceptance_criteria_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("evidence_ids_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("commit_shas_json", sa.Text, nullable=False, server_default="[]"),
        sa.Column("related_execution_ids_json", sa.Text, nullable=False, server_default="[]"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.UniqueConstraint("run_id", "item_id", name="uq_plan_items_run_item"),
        sa.CheckConstraint("status IN ('pending','in_progress','done','failed')"),
    )

    # ----- Policy revisions -------------------------------------------------
    op.create_table(
        "run_policy_revisions",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("limit_name", sa.String(64), nullable=False),
        sa.Column("old_value_json", sa.Text, nullable=False),
        sa.Column("new_value_json", sa.Text, nullable=False),
        sa.Column("reason", sa.Text, nullable=False),
        sa.Column("author", sa.String(64), nullable=False),
        sa.Column("created_at", sa.Float, nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("run_policy_revisions")
    op.drop_table("plan_items")
    op.drop_table("workspace_reservations")
    op.drop_table("command_journal")
    op.drop_table("queue_jobs")
    op.drop_table("artifact_manifests")
    op.drop_table("run_events")
    op.drop_table("agent_sessions")
    op.drop_table("step_attempts")
    op.drop_table("step_executions")
    op.drop_table("runs")
    op.drop_table("harness_profiles")
    op.drop_table("provider_connections")
    op.drop_table("pipeline_bindings")
    op.drop_table("pipeline_versions")
    op.drop_table("pipeline_templates")
    op.drop_table("messages")
    op.drop_table("chats")
    op.drop_table("projects")
