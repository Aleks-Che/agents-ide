"""SQLAlchemy ORM models for the Agents IDE domain tables (migration 0002_domain)."""

from __future__ import annotations

import datetime as _dt

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class GeneralSettings(Base):
    __tablename__ = "general_settings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    revision: Mapped[int] = mapped_column(Integer, default=0)


class PlanningCouncilDefaults(Base):
    __tablename__ = "planning_council_defaults"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    participants_json: Mapped[str] = mapped_column(Text, default="[]")
    revision: Mapped[int] = mapped_column(Integer, default=1)


def _utcnow() -> float:
    return _dt.datetime.now(_dt.UTC).timestamp()


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint(
            "workspace_identity_dev",
            "workspace_identity_ino",
            name="uq_projects_identity",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    workspace_entered_path: Mapped[str] = mapped_column(String(1024))
    workspace_normalized_path: Mapped[str] = mapped_column(String(1024))
    workspace_identity_dev: Mapped[int] = mapped_column(BigInteger)
    workspace_identity_ino: Mapped[int] = mapped_column(BigInteger)
    git_root_path: Mapped[str | None] = mapped_column(String(1024))
    git_head_sha: Mapped[str | None] = mapped_column(String(64))
    git_remote_url: Mapped[str | None] = mapped_column(String(512))
    git_default_branch: Mapped[str | None] = mapped_column(String(256))
    git_dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    chats: Mapped[list[Chat]] = relationship(back_populates="project", cascade="all")


class Chat(Base):
    __tablename__ = "chats"
    __table_args__ = (UniqueConstraint("project_id", "title", name="uq_chats_project_title"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    title: Mapped[str] = mapped_column(String(256))
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    project: Mapped[Project] = relationship(back_populates="chats")
    messages: Mapped[list[Message]] = relationship(back_populates="chat", cascade="all")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system','note')", name="ck_messages_role"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id"))
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    chat: Mapped[Chat] = relationship(back_populates="messages")


class PipelineTemplate(Base):
    __tablename__ = "pipeline_templates"
    __table_args__ = (CheckConstraint("kind IN ('user','system')", name="ck_templates_kind"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16), default="user")
    schema_version: Mapped[str] = mapped_column(String(32))
    draft_json: Mapped[str] = mapped_column(Text, default="{}")
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    versions: Mapped[list[PipelineVersion]] = relationship(back_populates="template", cascade="all")


class PipelineVersion(Base):
    __tablename__ = "pipeline_versions"
    __table_args__ = (
        UniqueConstraint("template_id", "version_number", name="uq_versions_template_number"),
        UniqueConstraint("template_id", "execution_hash", name="uq_versions_template_hash"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    template_id: Mapped[str] = mapped_column(ForeignKey("pipeline_templates.id"))
    version_number: Mapped[int] = mapped_column(Integer)
    schema_version: Mapped[str] = mapped_column(String(32))
    required_features_json: Mapped[str] = mapped_column(Text, default="[]")
    graph_json: Mapped[str] = mapped_column(Text)
    execution_hash: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    inputs_json: Mapped[str] = mapped_column(Text, default="{}")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    origin: Mapped[str] = mapped_column(String(16), default="local")
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    template: Mapped[PipelineTemplate] = relationship(back_populates="versions")
    bindings: Mapped[list[PipelineBinding]] = relationship(
        back_populates="pipeline_version", cascade="all"
    )


class PipelineBinding(Base):
    __tablename__ = "pipeline_bindings"
    __table_args__ = (
        UniqueConstraint("project_id", "name", name="uq_bindings_project_name"),
        CheckConstraint("branch_policy IN ('run_branch','current')", name="ck_bindings_branch"),
        CheckConstraint("dirty_policy IN ('strict','allow_nonoverlap')", name="ck_bindings_dirty"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    version_id: Mapped[str] = mapped_column(ForeignKey("pipeline_versions.id"))
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    name: Mapped[str] = mapped_column(String(120))
    role_assignments_json: Mapped[str] = mapped_column(Text, default="{}")
    model_overrides_json: Mapped[str] = mapped_column(Text, default="{}")
    model_selections_json: Mapped[str] = mapped_column(Text, default="{}")
    limit_overrides_json: Mapped[str] = mapped_column(Text, default="{}")
    command_filter_json: Mapped[str] = mapped_column(Text, default="[]")
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    branch_policy: Mapped[str] = mapped_column(String(16), default="run_branch")
    dirty_policy: Mapped[str] = mapped_column(String(16), default="strict")
    archived_at: Mapped[float | None] = mapped_column(Float)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    pipeline_version: Mapped[PipelineVersion] = relationship(back_populates="bindings")


class ProviderConnection(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        CheckConstraint("protocol IN ('http','https')", name="ck_providers_protocol"),
        CheckConstraint(
            "provider_kind IN ('openai_compatible','loopback')", name="ck_providers_kind"
        ),
        UniqueConstraint("name", name="uq_provider_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    provider_kind: Mapped[str] = mapped_column(String(32))
    base_url: Mapped[str] = mapped_column(String(512))
    protocol: Mapped[str] = mapped_column(String(8))
    secret_reference: Mapped[str | None] = mapped_column(String(64))
    manual_models_json: Mapped[str] = mapped_column(Text, default="[]")
    catalog_models_json: Mapped[str] = mapped_column(Text, default="[]")
    catalog_fetched_at: Mapped[float | None] = mapped_column(Float)
    catalog_ttl_seconds: Mapped[int] = mapped_column(Integer, default=900)
    last_test_status: Mapped[str | None] = mapped_column(String(16))
    last_test_at: Mapped[float | None] = mapped_column(Float)
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class HarnessProfile(Base):
    __tablename__ = "harness_profiles"
    __table_args__ = (
        CheckConstraint("harness_kind IN ('codex','opencode')", name="ck_harness_kind"),
        UniqueConstraint("name", name="uq_harness_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    harness_kind: Mapped[str] = mapped_column(String(32))
    executable_path: Mapped[str | None] = mapped_column(String(512))
    settings_json: Mapped[str] = mapped_column(Text, default="{}")
    archived_at: Mapped[float | None] = mapped_column(Float)
    catalog_models_json: Mapped[str] = mapped_column(Text, default="[]")
    catalog_metadata_json: Mapped[str] = mapped_column(Text, default="{}", server_default="{}")
    catalog_fingerprint: Mapped[str | None] = mapped_column(String(64))
    catalog_fetched_at: Mapped[float | None] = mapped_column(Float)
    catalog_ttl_seconds: Mapped[int] = mapped_column(Integer, default=900)
    last_test_status: Mapped[str | None] = mapped_column(String(16))
    last_test_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class Run(Base):
    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_runs_idempotency"),
        CheckConstraint(
            "state IN ('queued','running','pause_requested','paused','stop_requested',"
            "'stopped','retry_wait','waiting_input','recovering','completed','failed','cancelled')",
            name="ck_runs_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    request_json: Mapped[str | None] = mapped_column(Text)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    chat_id: Mapped[str | None] = mapped_column(ForeignKey("chats.id"))
    pipeline_version_id: Mapped[str] = mapped_column(ForeignKey("pipeline_versions.id"))
    binding_id: Mapped[str] = mapped_column(ForeignKey("pipeline_bindings.id"))
    state: Mapped[str] = mapped_column(String(20))
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    schema_version: Mapped[str] = mapped_column(String(32))
    execution_hash: Mapped[str] = mapped_column(String(64))
    policy_hash: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[str] = mapped_column(Text)
    resolved_settings_json: Mapped[str] = mapped_column(Text)
    current_node_id: Mapped[str | None] = mapped_column(String(64))
    current_execution_id: Mapped[str | None] = mapped_column(String(32))
    current_attempt_id: Mapped[str | None] = mapped_column(String(32))
    current_cycle_id: Mapped[int | None] = mapped_column(Integer)
    worker_id: Mapped[str | None] = mapped_column(String(32))
    worker_generation: Mapped[int] = mapped_column(Integer, default=0)
    waiting_reason_json: Mapped[str | None] = mapped_column(Text)
    active_intervals_json: Mapped[str] = mapped_column(Text, default="[]")
    runtime_json: Mapped[str] = mapped_column(Text, default="{}")
    resume_target_json: Mapped[str | None] = mapped_column(Text)
    stop_goal: Mapped[str | None] = mapped_column(String(16))
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    pinned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    retention_sequence: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    detailed_event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    artifact_bytes: Mapped[int] = mapped_column(BigInteger, default=0, server_default="0")


class StepExecution(Base):
    __tablename__ = "step_executions"
    __table_args__ = (
        UniqueConstraint(
            "run_id",
            "node_id",
            "visit_index",
            "cycle_id",
            name="uq_executions_run_node_visit_cycle",
        ),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','skipped',"
            "'retry_wait','waiting_input','interrupted')",
            name="ck_executions_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    node_id: Mapped[str] = mapped_column(String(64))
    visit_index: Mapped[int] = mapped_column(Integer)
    cycle_id: Mapped[int] = mapped_column(Integer)
    scope: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    finished_at: Mapped[float | None] = mapped_column(Float)
    raw_result_ref: Mapped[str | None] = mapped_column(String(64))
    validated_result_json: Mapped[str | None] = mapped_column(Text)
    decision: Mapped[str | None] = mapped_column(String(8))
    evidence_manifest_id: Mapped[str | None] = mapped_column(String(32))
    plan_item_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)


class StepAttempt(Base):
    __tablename__ = "step_attempts"
    __table_args__ = (
        UniqueConstraint("execution_id", "attempt_index", name="uq_attempts_execution_index"),
        CheckConstraint(
            "status IN ('prepared','running','succeeded','failed','interrupted',"
            "'unknown','waiting_input')",
            name="ck_attempts_status",
        ),
        CheckConstraint("retry_safety IN ('safe','unsafe','unknown')", name="ck_attempts_retry"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    execution_id: Mapped[str] = mapped_column(ForeignKey("step_executions.id"))
    attempt_index: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16))
    operation_id: Mapped[str | None] = mapped_column(String(64))
    retry_safety: Mapped[str] = mapped_column(String(16), default="unknown")
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    finished_at: Mapped[float | None] = mapped_column(Float)
    external_outcome: Mapped[str | None] = mapped_column(String(16))
    error_code: Mapped[str | None] = mapped_column(String(64))
    error_details_json: Mapped[str | None] = mapped_column(Text)
    tokens_used: Mapped[int | None] = mapped_column(Integer)
    cost_estimated: Mapped[float | None] = mapped_column(Float)
    budget_quality: Mapped[str | None] = mapped_column(String(16))
    selection_json: Mapped[str] = mapped_column(Text, default="{}")
    request_artifact_id: Mapped[str | None] = mapped_column(String(32))
    result_artifact_id: Mapped[str | None] = mapped_column(String(32))
    heartbeat_at: Mapped[float | None] = mapped_column(Float, default=_utcnow)


class AgentSession(Base):
    __tablename__ = "agent_sessions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    attempt_id: Mapped[str] = mapped_column(ForeignKey("step_attempts.id"))
    harness_kind: Mapped[str] = mapped_column(String(32))
    capabilities_json: Mapped[str] = mapped_column(Text, default="{}")
    role: Mapped[str] = mapped_column(String(64))
    external_session_id: Mapped[str | None] = mapped_column(String(128))
    external_turn_id: Mapped[str | None] = mapped_column(String(128))
    cwd_identity_dev: Mapped[int | None] = mapped_column(BigInteger)
    cwd_identity_ino: Mapped[int | None] = mapped_column(BigInteger)
    process_id: Mapped[str | None] = mapped_column(String(32))
    resume_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    finished_at: Mapped[float | None] = mapped_column(Float)
    last_external_event_at: Mapped[float | None] = mapped_column(Float)
    last_message_preview: Mapped[str | None] = mapped_column(Text)


class RunEvent(Base):
    __tablename__ = "run_events"
    __table_args__ = (UniqueConstraint("run_id", "sequence", name="uq_run_events_sequence"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    sequence: Mapped[int] = mapped_column(Integer)
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    persisted_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    node_id: Mapped[str | None] = mapped_column(String(64))
    step_execution_id: Mapped[str | None] = mapped_column(String(32))
    step_attempt_id: Mapped[str | None] = mapped_column(String(32))
    agent_session_id: Mapped[str | None] = mapped_column(String(32))
    command_id: Mapped[str | None] = mapped_column(String(64))
    worker_generation: Mapped[int] = mapped_column(Integer, default=0)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


class ArtifactManifest(Base):
    __tablename__ = "artifact_manifests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    body_json: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    schema_type: Mapped[str] = mapped_column(String(64))
    byte_length: Mapped[int] = mapped_column(BigInteger)
    content_hash: Mapped[str] = mapped_column(String(64))
    source_kind: Mapped[str] = mapped_column(String(32))
    source_ref: Mapped[str | None] = mapped_column(String(256))
    step_execution_id: Mapped[str | None] = mapped_column(String(32))
    step_attempt_id: Mapped[str | None] = mapped_column(String(32))
    cycle_id: Mapped[int | None] = mapped_column(Integer)
    plan_item_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    files_json: Mapped[str] = mapped_column(Text, default="[]")
    base_head_sha: Mapped[str | None] = mapped_column(String(64))
    current_head_sha: Mapped[str | None] = mapped_column(String(64))
    redaction_json: Mapped[str | None] = mapped_column(Text)
    truncation_json: Mapped[str | None] = mapped_column(Text)
    omissions_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    tombstoned_at: Mapped[float | None] = mapped_column(Float)
    purged_at: Mapped[float | None] = mapped_column(Float)


class QueueJob(Base):
    __tablename__ = "queue_jobs"
    __table_args__ = (UniqueConstraint("run_id", name="uq_queue_jobs_run"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    available_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    claimed_by: Mapped[str | None] = mapped_column(String(32))
    owner_pid: Mapped[int | None] = mapped_column(Integer)
    owner_create_time: Mapped[float | None] = mapped_column(Float)
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class CommandJournal(Base):
    __tablename__ = "command_journal"
    __table_args__ = (UniqueConstraint("run_id", "command_id", name="uq_journal_run_command"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    command_id: Mapped[str] = mapped_column(String(64))
    command_type: Mapped[str] = mapped_column(String(32))
    expected_state_version: Mapped[int] = mapped_column(Integer)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str | None] = mapped_column(Text)
    sequence: Mapped[int] = mapped_column(Integer)
    initiator: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(16))
    response_json: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    applied_at: Mapped[float | None] = mapped_column(Float)


class WorkspaceReservation(Base):
    __tablename__ = "workspace_reservations"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    workspace_identity_dev: Mapped[int] = mapped_column(BigInteger)
    workspace_identity_ino: Mapped[int] = mapped_column(BigInteger)
    workspace_json: Mapped[str | None] = mapped_column(Text)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    owner_generation: Mapped[int] = mapped_column(Integer)
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    released_at: Mapped[float | None] = mapped_column(Float)


class PlanItem(Base):
    __tablename__ = "plan_items"
    __table_args__ = (
        UniqueConstraint("run_id", "item_id", name="uq_plan_items_run_item"),
        CheckConstraint(
            "status IN ('pending','in_progress','done','failed')", name="ck_plan_items_status"
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    item_id: Mapped[str] = mapped_column(String(64))
    scope: Mapped[str] = mapped_column(String(64), default="__default__")
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(Text)
    acceptance_criteria_json: Mapped[str] = mapped_column(Text, default="[]")
    status: Mapped[str] = mapped_column(String(16))
    evidence_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    commit_shas_json: Mapped[str] = mapped_column(Text, default="[]")
    related_execution_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class RunPolicyRevision(Base):
    __tablename__ = "run_policy_revisions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    limit_name: Mapped[str] = mapped_column(String(64))
    old_value_json: Mapped[str] = mapped_column(Text)
    new_value_json: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    author: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class ModelGroup(Base):
    __tablename__ = "model_groups"
    __table_args__ = (
        CheckConstraint("kind IN ('agent','llm')", name="ck_model_groups_kind"),
        UniqueConstraint("kind", "name", name="uq_model_groups_kind_name"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    description: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16))
    revision: Mapped[int] = mapped_column(Integer, default=1)
    archived_at: Mapped[float | None] = mapped_column(Float)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    members: Mapped[list[ModelGroupMember]] = relationship(back_populates="group", cascade="all")


class ModelGroupMember(Base):
    __tablename__ = "model_group_members"
    __table_args__ = (UniqueConstraint("group_id", "member_index", name="uq_members_group_index"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    group_id: Mapped[str] = mapped_column(ForeignKey("model_groups.id"))
    member_index: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    harness_profile_id: Mapped[str | None] = mapped_column(ForeignKey("harness_profiles.id"))
    provider_connection_id: Mapped[str | None] = mapped_column(
        ForeignKey("provider_connections.id")
    )
    model_id: Mapped[str] = mapped_column(String(256))
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    schedule_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)

    group: Mapped[ModelGroup] = relationship(back_populates="members")


class ProcessSupervision(Base):
    __tablename__ = "process_supervision"
    __table_args__ = (
        CheckConstraint(
            "state IN ('started','interrupt_requested','killed','finished','unknown')",
            name="ck_process_state",
        ),
        CheckConstraint(
            "kind IN ('harness','llm','command','git','support')",
            name="ck_process_kind",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id"))
    step_attempt_id: Mapped[str | None] = mapped_column(ForeignKey("step_attempts.id"))
    role: Mapped[str] = mapped_column(String(32))
    owner_generation: Mapped[int] = mapped_column(Integer)
    pid: Mapped[int] = mapped_column(Integer)
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    create_time: Mapped[float] = mapped_column(Float, default=_utcnow)
    parent_pid: Mapped[int | None] = mapped_column(Integer)
    executable: Mapped[str | None] = mapped_column(String(512))
    kind: Mapped[str] = mapped_column(String(32))
    state: Mapped[str] = mapped_column(String(16))
    interrupt_requested_at: Mapped[float | None] = mapped_column(Float)
    killed_at: Mapped[float | None] = mapped_column(Float)
    last_external_event_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)
    tree_json: Mapped[str | None] = mapped_column(Text)
    last_health_ok: Mapped[float | None] = mapped_column(Float)
    transport: Mapped[str | None] = mapped_column(String(32))
    port: Mapped[int | None] = mapped_column(Integer)
    workspace_json: Mapped[str | None] = mapped_column(Text)


class PlanningJob(Base):
    __tablename__ = "planning_jobs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_planning_jobs_idempotency"),
        CheckConstraint(
            "state IN ('drafting','merging','needs_answers','ready_for_confirmation',"
            "'confirmed','cancelled','failed')",
            name="ck_planning_jobs_state",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(64))
    request_hash: Mapped[str | None] = mapped_column(String(64))
    lease_owner: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[float | None] = mapped_column(Float)
    owner_pid: Mapped[int | None] = mapped_column(Integer)
    owner_create_time: Mapped[float | None] = mapped_column(Float)
    generation: Mapped[int] = mapped_column(Integer, default=0)
    project_id: Mapped[str] = mapped_column(ForeignKey("projects.id"))
    chat_id: Mapped[str | None] = mapped_column(ForeignKey("chats.id"))
    initiator: Mapped[str] = mapped_column(String(64), default="ui")
    state: Mapped[str] = mapped_column(String(32))
    state_version: Mapped[int] = mapped_column(Integer, default=0)
    read_manifest_hash: Mapped[str] = mapped_column(String(64), default="")
    read_workspace_json: Mapped[str] = mapped_column(Text, default="{}")
    task_text: Mapped[str] = mapped_column(Text)
    context_snapshot_json: Mapped[str] = mapped_column(Text, default="{}")
    budget_json: Mapped[str] = mapped_column(Text, default="{}")
    usage_json: Mapped[str] = mapped_column(Text, default="{}")
    n_participants_requested: Mapped[int] = mapped_column(Integer)
    n_participants_actual: Mapped[int] = mapped_column(Integer, default=0)
    degraded: Mapped[bool] = mapped_column(Boolean, default=False)
    concurrency: Mapped[int] = mapped_column(Integer, default=2)
    merged_by_member_id: Mapped[str | None] = mapped_column(String(32))
    last_error_json: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    updated_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    finished_at: Mapped[float | None] = mapped_column(Float)


class PlanningMember(Base):
    __tablename__ = "planning_members"
    __table_args__ = (
        UniqueConstraint("job_id", "slot_index", name="uq_planning_members_slot"),
        CheckConstraint("role IN ('participant','merger')", name="ck_planning_members_role"),
        CheckConstraint(
            "status IN ('pending','running','succeeded','failed','unknown','skipped')",
            name="ck_planning_members_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("planning_jobs.id"))
    slot_index: Mapped[int] = mapped_column(Integer)
    role: Mapped[str] = mapped_column(String(16))
    selection_json: Mapped[str] = mapped_column(Text)
    selection_kind: Mapped[str] = mapped_column(String(16))
    group_id: Mapped[str | None] = mapped_column(String(32))
    harness_profile_id: Mapped[str | None] = mapped_column(String(32))
    provider_connection_id: Mapped[str | None] = mapped_column(String(32))
    model_id: Mapped[str] = mapped_column(String(256), default="")
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    candidates_json: Mapped[str] = mapped_column(Text, default="[]")
    access_overrides_json: Mapped[str] = mapped_column(Text, default="{}")
    candidate_index: Mapped[int] = mapped_column(Integer, default=0)
    attempt_external_id: Mapped[str | None] = mapped_column(String(128))
    draft_revision: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16))
    error_json: Mapped[str | None] = mapped_column(Text)
    actual_member_id: Mapped[str | None] = mapped_column(String(32))
    started_at: Mapped[float | None] = mapped_column(Float)
    finished_at: Mapped[float | None] = mapped_column(Float)


class PlanningDraft(Base):
    __tablename__ = "planning_drafts"
    __table_args__ = (
        UniqueConstraint("member_id", name="uq_planning_drafts_member"),
        CheckConstraint(
            "parse_status IN ('unparsed','found','none_found','invalid_format')",
            name="ck_planning_drafts_parse",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("planning_jobs.id"))
    member_id: Mapped[str] = mapped_column(ForeignKey("planning_members.id"))
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    parse_status: Mapped[str] = mapped_column(String(16), default="unparsed")
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    byte_length: Mapped[int] = mapped_column(BigInteger, default=0)
    content_hash: Mapped[str] = mapped_column(String(64), default="")
    omissions_json: Mapped[str] = mapped_column(Text, default="[]")
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class PlanningRevision(Base):
    __tablename__ = "planning_revisions"
    __table_args__ = (
        UniqueConstraint("job_id", "revision_number", name="uq_planning_revisions_number"),
        CheckConstraint(
            "author IN ('merger','user','single_member')",
            name="ck_planning_revisions_author",
        ),
        CheckConstraint(
            "readiness IN ('ready','needs_answers','unverified','invalid_format')",
            name="ck_planning_revisions_readiness",
        ),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("planning_jobs.id"))
    revision_number: Mapped[int] = mapped_column(Integer)
    author: Mapped[str] = mapped_column(String(16))
    body_text: Mapped[str] = mapped_column(Text, default="")
    body_truncated: Mapped[bool] = mapped_column(Boolean, default=False)
    parse_status: Mapped[str] = mapped_column(String(16), default="unparsed")
    questions_json: Mapped[str] = mapped_column(Text, default="[]")
    plan_json: Mapped[str] = mapped_column(Text, default="{}")
    answers_json: Mapped[str] = mapped_column(Text, default="[]")
    questions_hash: Mapped[str] = mapped_column(String(64), default="")
    readiness: Mapped[str] = mapped_column(String(16), default="unverified")
    confirmation_hash: Mapped[str | None] = mapped_column(String(64))
    answered_hash: Mapped[str | None] = mapped_column(String(64))
    confirmed_at: Mapped[float | None] = mapped_column(Float)
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class PlanningAttempt(Base):
    __tablename__ = "planning_attempts"
    __table_args__ = (UniqueConstraint("member_id", "attempt_index"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("planning_jobs.id"))
    member_id: Mapped[str] = mapped_column(ForeignKey("planning_members.id"))
    attempt_index: Mapped[int] = mapped_column(Integer)
    generation: Mapped[int] = mapped_column(Integer)
    candidate_json: Mapped[str] = mapped_column(Text)
    runtime_json: Mapped[str] = mapped_column(Text, default="{}")
    outcome: Mapped[str] = mapped_column(String(32), default="running")
    body_text: Mapped[str] = mapped_column(Text, default="")
    error_code: Mapped[str | None] = mapped_column(String(64))
    started_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    finished_at: Mapped[float | None] = mapped_column(Float)


class PlanningAnswer(Base):
    __tablename__ = "planning_answers"
    __table_args__ = (
        UniqueConstraint(
            "revision_id", "question_id", name="uq_planning_answers_revision_question"
        ),
        CheckConstraint("kind IN ('single','multi','text')", name="ck_planning_answers_kind"),
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    revision_id: Mapped[str] = mapped_column(ForeignKey("planning_revisions.id"))
    question_id: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))
    selected_option_ids_json: Mapped[str] = mapped_column(Text, default="[]")
    free_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[float] = mapped_column(Float, default=_utcnow)


class PlanningEvent(Base):
    __tablename__ = "planning_events"
    __table_args__ = (UniqueConstraint("job_id", "sequence", name="uq_planning_events_sequence"),)

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    job_id: Mapped[str] = mapped_column(ForeignKey("planning_jobs.id"))
    member_id: Mapped[str | None] = mapped_column(String(32))
    sequence: Mapped[int] = mapped_column(Integer)
    event_version: Mapped[int] = mapped_column(Integer, default=1)
    type: Mapped[str] = mapped_column(String(64))
    occurred_at: Mapped[float] = mapped_column(Float, default=_utcnow)
    payload_json: Mapped[str] = mapped_column(Text, default="{}")


__all__ = [
    "Base",
    "Project",
    "Chat",
    "Message",
    "PipelineTemplate",
    "PipelineVersion",
    "PipelineBinding",
    "ProviderConnection",
    "HarnessProfile",
    "Run",
    "StepExecution",
    "StepAttempt",
    "AgentSession",
    "RunEvent",
    "ArtifactManifest",
    "QueueJob",
    "CommandJournal",
    "WorkspaceReservation",
    "PlanItem",
    "RunPolicyRevision",
    "ModelGroup",
    "ModelGroupMember",
    "ProcessSupervision",
    "PlanningJob",
    "PlanningMember",
    "PlanningDraft",
    "PlanningRevision",
    "PlanningAnswer",
    "PlanningEvent",
]
