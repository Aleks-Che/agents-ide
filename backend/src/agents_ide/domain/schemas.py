"""Strict API contracts; incomplete pipeline drafts are stored separately."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from agents_ide.domain.contracts import RunState

NonEmptyStr = Annotated[str, StringConstraints(min_length=1, max_length=120)]
ShortStr = Annotated[str, StringConstraints(min_length=1, max_length=64)]
LongStr = Annotated[str, StringConstraints(min_length=1, max_length=256)]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @model_validator(mode="after")
    def _json_values(self) -> ApiModel:
        json.dumps(self.model_dump(mode="json"), allow_nan=False)
        return self


class ApiOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


# ----------------------------------------------------------------------------- Path


class WorkspaceInfo(ApiOutput):
    entered_path: str
    normalized_path: str
    identity_dev: int
    identity_ino: int
    git_root_path: str | None
    git_head_sha: str | None
    git_remote_url: str | None
    git_default_branch: str | None
    git_dirty: bool


class ProjectCreate(ApiModel):
    name: NonEmptyStr
    workspace_path: str = Field(min_length=1, max_length=1024)


class ProjectUpdate(ApiModel):
    name: NonEmptyStr | None = None
    expected_version: int = Field(ge=1)


class ProjectArchive(ApiModel):
    expected_version: int = Field(ge=1)
    archive: bool = True


class Project(ApiOutput):
    id: str
    name: str
    workspace: WorkspaceInfo
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


# ----------------------------------------------------------------------------- Chat


class ChatCreate(ApiModel):
    title: NonEmptyStr


class ChatUpdate(ApiModel):
    title: NonEmptyStr
    expected_version: int = Field(ge=1)


class ChatArchive(ApiModel):
    expected_version: int = Field(ge=1)
    archive: bool = True


class Chat(ApiOutput):
    id: str
    project_id: str
    title: str
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class MessageCreate(ApiModel):
    role: Literal["user", "assistant", "system", "note"]
    content: str = Field(min_length=1, max_length=64 * 1024)


class Message(ApiOutput):
    id: str
    chat_id: str
    role: str
    content: str
    created_at: datetime
    archived: bool
    version: int


class MessageUpdate(ApiModel):
    content: str = Field(min_length=1, max_length=64 * 1024)
    expected_version: int = Field(ge=1)


class SettingsOverrides(ApiModel):
    role_assignments: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    limit_overrides: dict[str, float] | None = None
    command_filter: list[str] | None = None
    branch_policy: Literal["run_branch", "current"] | None = None
    dirty_policy: Literal["strict", "allow_nonoverlap"] | None = None

    @field_validator("limit_overrides")
    @classmethod
    def _positive_limits(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        import math

        if value and any(not math.isfinite(item) or item <= 0 for item in value.values()):
            raise ValueError("Limits must be positive finite numbers")
        return value


class PipelineDraft(ApiModel):
    graph: dict[str, Any] = Field(default_factory=dict)
    required_features: list[str] = Field(default_factory=list, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)
    settings: SettingsOverrides = Field(default_factory=SettingsOverrides)

    @model_validator(mode="after")
    def _size(self) -> PipelineDraft:
        if len(json.dumps(self.model_dump(), ensure_ascii=False).encode("utf-8")) > 1048576:
            raise ValueError("Pipeline exceeds 1 MiB")
        return self


class PipelineDraftUpdate(PipelineDraft):
    expected_version: int = Field(ge=1)


class DraftPublish(ApiModel):
    expected_version: int = Field(ge=1)


# ----------------------------------------------------------------------------- Pipeline templates


class PipelineTemplateCreate(ApiModel):
    name: NonEmptyStr
    description: str = Field(default="", max_length=4096)
    schema_version: str = Field(default="1.0.0", max_length=32)


class PipelineTemplateUpdate(ApiModel):
    name: NonEmptyStr | None = None
    description: str | None = Field(default=None, max_length=4096)
    expected_version: int = Field(ge=1)


class PipelineTemplate(ApiOutput):
    id: str
    name: str
    description: str
    kind: Literal["user", "system"]
    schema_version: str
    draft: PipelineDraft
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class PipelineVersionCreate(PipelineDraft):
    graph: dict[str, Any] = Field(min_length=2)
    required_features: list[str] = Field(default_factory=list, max_length=64)
    inputs: dict[str, Any] = Field(default_factory=dict)

    @field_validator("graph")
    @classmethod
    def _graph_size(cls, value: dict[str, Any]) -> dict[str, Any]:
        nodes = value.get("nodes")
        edges = value.get("edges")
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("graph.nodes and graph.edges must be lists")
        if len(nodes) > 200 or len(edges) > 400:
            raise ValueError("graph exceeds 200 nodes / 400 edges limit")
        return value


class PipelineVersion(ApiOutput):
    id: str
    template_id: str
    version_number: int
    schema_version: str
    execution_hash: str
    policy_hash: str
    graph: dict[str, Any]
    required_features: list[str]
    inputs: dict[str, Any]
    settings: SettingsOverrides
    created_at: datetime
    immutable: Literal[True] = True


class PipelineBindingCreate(SettingsOverrides):
    project_id: str
    name: NonEmptyStr
    role_assignments: dict[str, str] = Field(default_factory=dict)
    model_overrides: dict[str, str] = Field(default_factory=dict)
    limit_overrides: dict[str, float] = Field(default_factory=dict)
    command_filter: list[str] = Field(default_factory=list)
    branch_policy: Literal["run_branch", "current"] = "run_branch"
    dirty_policy: Literal["strict", "allow_nonoverlap"] = "strict"


class PipelineBindingUpdate(SettingsOverrides):
    name: NonEmptyStr | None = None
    role_assignments: dict[str, str] | None = None
    model_overrides: dict[str, str] | None = None
    limit_overrides: dict[str, float] | None = None
    command_filter: list[str] | None = None
    branch_policy: Literal["run_branch", "current"] | None = None
    dirty_policy: Literal["strict", "allow_nonoverlap"] | None = None
    expected_version: int = Field(ge=1)


class PipelineBinding(ApiOutput):
    id: str
    version_id: str
    project_id: str
    name: str
    role_assignments: dict[str, str]
    model_overrides: dict[str, str]
    limit_overrides: dict[str, Any]
    command_filter: list[str]
    branch_policy: Literal["run_branch", "current"]
    dirty_policy: Literal["strict", "allow_nonoverlap"]
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


# ----------------------------------------------------------------------------- Provider connections


class ProviderConnectionCreate(ApiModel):
    name: NonEmptyStr
    provider_kind: Literal["openai_compatible", "loopback"] = "openai_compatible"
    base_url: str = Field(min_length=1, max_length=512)
    secret: str | None = Field(default=None, min_length=1, max_length=4096)
    manual_models: list[str] = Field(default_factory=list)
    catalog_ttl_seconds: int = Field(default=900, ge=60, le=86400)

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not re.match(r"^https?://", value):
            raise ValueError("base_url must start with http:// or https://")
        if "@" in value.split("?", 1)[0]:
            raise ValueError("base_url must not include userinfo")
        return value.rstrip("/")


class ProviderConnectionUpdate(ApiModel):
    name: NonEmptyStr | None = None
    base_url: str | None = Field(default=None, min_length=1, max_length=512)
    secret: str | None = Field(default=None, min_length=1, max_length=4096)
    manual_models: list[str] | None = None
    catalog_ttl_seconds: int | None = Field(default=None, ge=60, le=86400)
    expected_version: int = Field(ge=1)

    @field_validator("base_url")
    @classmethod
    def _validate_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if not re.match(r"^https?://", value):
            raise ValueError("base_url must start with http:// or https://")
        if "@" in value.split("?", 1)[0]:
            raise ValueError("base_url must not include userinfo")
        return value.rstrip("/")


class ProviderConnection(ApiOutput):
    id: str
    name: str
    provider_kind: Literal["openai_compatible", "loopback"]
    base_url: str
    protocol: Literal["http", "https"]
    has_secret: bool
    manual_models: list[str]
    catalog_models: list[str]
    catalog_fetched_at: datetime | None
    catalog_ttl_seconds: int
    last_test_status: str | None
    last_test_at: datetime | None
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ProviderTest(ApiOutput):
    status: Literal["ok", "failed"]
    models: list[str]
    detail: str | None = None
    tested_at: datetime


# ----------------------------------------------------------------------------- Harness profiles


class HarnessProfileCreate(ApiModel):
    name: NonEmptyStr
    harness_kind: Literal["codex", "opencode"]
    executable_path: str | None = Field(default=None, max_length=512)
    settings: dict[str, Any] = Field(default_factory=dict)

    @field_validator("settings")
    @classmethod
    def _no_credentials(cls, value: dict[str, Any]) -> dict[str, Any]:
        _reject_credentials(value)
        return value


class HarnessProfileUpdate(ApiModel):
    name: NonEmptyStr | None = None
    executable_path: str | None = Field(default=None, max_length=512)
    settings: dict[str, Any] | None = None
    expected_version: int = Field(ge=1)

    @field_validator("settings")
    @classmethod
    def _no_credentials(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        _reject_credentials(value)
        return value


def _reject_credentials(value: Any) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if re.search(r"secret|password|api[_-]?key|authorization|access[_-]?token", key, re.I):
                raise ValueError("Credentials must use SecretStore or harness authentication")
            _reject_credentials(item)
    elif isinstance(value, list):
        for item in value:
            _reject_credentials(item)


class HarnessProfile(ApiOutput):
    id: str
    name: str
    harness_kind: Literal["codex", "opencode"]
    executable_path: str | None
    settings: dict[str, Any]
    archived: bool
    version: int
    created_at: datetime
    updated_at: datetime


# ----------------------------------------------------------------------------- Runs


class RunStart(ApiModel):
    project_id: str
    chat_id: str | None = None
    binding_id: str
    message: str | None = Field(default=None, max_length=64 * 1024)
    idempotency_key: ShortStr
    initiator: ShortStr = "ui"
    inputs: dict[str, Any] = Field(default_factory=dict)
    overrides: SettingsOverrides = Field(default_factory=SettingsOverrides)

    @model_validator(mode="after")
    def _one_of(self) -> RunStart:
        if self.chat_id is None and not self.message:
            raise ValueError("Either chat_id or message must be provided")
        return self


class ResolvedSettingSource(ApiOutput):
    name: str
    value: Any
    source: Literal["run", "binding", "template", "node", "default"]
    locked: bool


class ResolvedSettings(ApiOutput):
    settings: list[ResolvedSettingSource]
    execution_hash: str
    policy_hash: str
    schema_version: str


class WaitingReason(ApiModel):
    code: Literal[
        "missing_data",
        "permission_required",
        "limit_exceeded",
        "auth_required",
        "secret_unavailable",
        "model_unavailable",
        "external_change_detected",
        "unknown_external_result",
        "invalid_response_format",
        "workspace_conflict",
        "process_not_responding",
        "port_unavailable",
        "path_violation",
        "import_trust_required",
        "schema_unsupported",
        "configuration_invalid",
        "storage_unavailable",
        "signing_required",
        "no_progress",
    ]
    details: dict[str, Any] = Field(default_factory=dict)
    allowed_actions: list[Literal["resolve", "resume", "pause", "stop", "cancel"]]
    blocked_operation_id: str | None = None
    resolution_schema: dict[str, Any] = Field(default_factory=dict)


class ActiveInterval(ApiModel):
    state: Literal["running", "retry_wait", "recovering", "pause_requested", "stop_requested"]
    started_at: AwareDatetime
    ended_at: AwareDatetime | None = None
    quality: Literal["observed", "estimated", "unknown"]

    @model_validator(mode="after")
    def _order(self) -> ActiveInterval:
        if self.ended_at is not None and self.ended_at < self.started_at:
            raise ValueError("Interval ends before it starts")
        return self


class Run(ApiOutput):
    id: str
    idempotency_key: str
    project_id: str
    chat_id: str | None
    binding_id: str
    pipeline_version_id: str
    state: RunState
    state_version: int
    schema_version: str
    execution_hash: str
    policy_hash: str
    snapshot_hash: str
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime
    updated_at: datetime
    worker_id: str | None
    worker_generation: int
    waiting_reason: WaitingReason | None = None
    active_intervals: list[ActiveInterval] = Field(default_factory=list)


class RunCommand(ApiModel):
    command_id: ShortStr
    command_type: Literal["pause", "stop", "cancel", "resume", "resolve"]
    expected_state_version: int = Field(ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class CommandAccepted(ApiOutput):
    command_id: str
    sequence: int
    status: Literal["accepted", "applied", "rejected", "superseded"]
    response: dict[str, Any] | None = None
    applied_at: datetime | None = None
