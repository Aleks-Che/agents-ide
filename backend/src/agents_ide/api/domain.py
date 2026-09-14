"""API router wiring domain services to HTTP endpoints."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import ConfigDict, Field
from sqlalchemy.orm import Session

from agents_ide.api.deps import get_secret_store, get_session, get_settings
from agents_ide.config import Settings
from agents_ide.domain.graph_schema import node_schemas as _node_schemas_payload
from agents_ide.domain.graph_validation import (
    ValidationReport,
    validate_version,
)
from agents_ide.domain.graph_validation import (
    export_payload as export_graph_payload,
)
from agents_ide.domain.graph_validation import (
    import_payload as import_graph_payload,
)
from agents_ide.domain.graph_validation import (
    preflight as preflight_binding,
)
from agents_ide.domain.schemas import (
    Chat,
    ChatArchive,
    ChatCreate,
    ChatUpdate,
    CommandAccepted,
    DraftPublish,
    HarnessProfile,
    HarnessProfileCreate,
    HarnessProfileUpdate,
    Message,
    MessageCreate,
    MessageUpdate,
    ModelGroup,
    ModelGroupAgentCreate,
    ModelGroupAgentMembersReplace,
    ModelGroupAgentUpdate,
    ModelGroupCopy,
    ModelGroupExport,
    ModelGroupImport,
    ModelGroupLLMCreate,
    ModelGroupLLMMembersReplace,
    ModelGroupLLMUpdate,
    ModelGroupMemberDelete,
    PipelineBinding,
    PipelineBindingCreate,
    PipelineBindingUpdate,
    PipelineDraftUpdate,
    PipelineTemplate,
    PipelineTemplateCreate,
    PipelineTemplateUpdate,
    PipelineVersion,
    PipelineVersionCreate,
    Project,
    ProjectArchive,
    ProjectCreate,
    ProjectUpdate,
    ProviderConnection,
    ProviderConnectionCreate,
    ProviderConnectionUpdate,
    ProviderTest,
    ResolvedSettings,
    Run,
    RunCommand,
    RunStart,
    SettingsOverrides,
)
from agents_ide.errors import AppError
from agents_ide.security.secrets import SecretStore
from agents_ide.services import (
    chats,
    connections,
    groups,
    harness,
    projects,
    runs,
    templates,
)

router = APIRouter(prefix="/api", tags=["domain"])


SessionDep = Annotated[Session, Depends(get_session, scope="function")]
SecretDep = Annotated[SecretStore, Depends(get_secret_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


# ----------------------------------------------------------------------------- Workspace probe


@router.post("/workspace/probe")
def workspace_probe(payload: dict[str, str]) -> dict[str, Any]:
    path = payload.get("path", "")
    if not path:
        raise AppError("path_invalid", "Не указан путь", 400)
    from agents_ide.domain.workspace import touch_workspace

    return touch_workspace(path)


# ----------------------------------------------------------------------------- Projects


@router.get("/projects", response_model=list[Project])
def list_projects_endpoint(
    session: SessionDep,
    include_archived: bool = Query(default=False),
) -> list[Project]:
    return projects.list_projects(session, include_archived=include_archived)


@router.post("/projects", response_model=Project, status_code=201)
def create_project_endpoint(session: SessionDep, payload: ProjectCreate) -> Project:
    return projects.create_project(session, payload)


@router.get("/projects/{project_id}", response_model=Project)
def get_project_endpoint(session: SessionDep, project_id: str) -> Project:
    return projects.get_project(session, project_id)


@router.patch("/projects/{project_id}", response_model=Project)
def update_project_endpoint(
    session: SessionDep, project_id: str, payload: ProjectUpdate
) -> Project:
    return projects.update_project(session, project_id, payload)


@router.post("/projects/{project_id}/archive", response_model=Project)
def archive_project_endpoint(
    session: SessionDep, project_id: str, payload: ProjectArchive
) -> Project:
    return projects.archive_project(session, project_id, payload)


# ----------------------------------------------------------------------------- Chats and messages


@router.get("/projects/{project_id}/chats", response_model=list[Chat])
def list_chats_endpoint(
    session: SessionDep,
    project_id: str,
    include_archived: bool = Query(default=False),
) -> list[Chat]:
    return chats.list_chats(session, project_id, include_archived=include_archived)


@router.post("/projects/{project_id}/chats", response_model=Chat, status_code=201)
def create_chat_endpoint(session: SessionDep, project_id: str, payload: ChatCreate) -> Chat:
    return chats.create_chat(session, project_id, payload)


@router.get("/chats/{chat_id}", response_model=Chat)
def get_chat_endpoint(session: SessionDep, chat_id: str) -> Chat:
    return chats.get_chat(session, chat_id)


@router.post("/chats/{chat_id}/archive", response_model=Chat)
def archive_chat_endpoint(session: SessionDep, chat_id: str, payload: ChatArchive) -> Chat:
    return chats.archive_chat(session, chat_id, payload)


@router.patch("/chats/{chat_id}", response_model=Chat)
def update_chat_endpoint(session: SessionDep, chat_id: str, payload: ChatUpdate) -> Chat:
    return chats.update_chat(session, chat_id, payload)


@router.get("/messages/{message_id}", response_model=Message)
def get_message_endpoint(session: SessionDep, message_id: str) -> Message:
    return chats.get_message(session, message_id)


@router.patch("/messages/{message_id}", response_model=Message)
def update_message_endpoint(
    session: SessionDep,
    message_id: str,
    payload: MessageUpdate,
) -> Message:
    return chats.update_message(session, message_id, payload)


@router.post("/messages/{message_id}/archive", response_model=Message)
def archive_message_endpoint(
    session: SessionDep,
    message_id: str,
    payload: ChatArchive,
) -> Message:
    return chats.archive_message(session, message_id, payload)


@router.get("/chats/{chat_id}/messages", response_model=list[Message])
def list_messages_endpoint(
    session: SessionDep, chat_id: str, limit: int = Query(default=200, ge=1, le=1000)
) -> list[Message]:
    return chats.list_messages(session, chat_id, limit=limit)


@router.post("/chats/{chat_id}/messages", response_model=Message, status_code=201)
def add_message_endpoint(session: SessionDep, chat_id: str, payload: MessageCreate) -> Message:
    return chats.add_message(session, chat_id, payload)


# ----------------------------------------------------------------------------- Pipeline templates


@router.get("/templates", response_model=list[PipelineTemplate])
def list_templates_endpoint(
    session: SessionDep, include_archived: bool = Query(default=False)
) -> list[PipelineTemplate]:
    return templates.list_templates(session, include_archived=include_archived)


@router.post("/templates", response_model=PipelineTemplate, status_code=201)
def create_template_endpoint(
    session: SessionDep, payload: PipelineTemplateCreate
) -> PipelineTemplate:
    return templates.create_template(session, payload)


@router.get("/templates/{template_id}", response_model=PipelineTemplate)
def get_template_endpoint(session: SessionDep, template_id: str) -> PipelineTemplate:
    return templates.get_template(session, template_id)


@router.patch("/templates/{template_id}", response_model=PipelineTemplate)
def update_template_endpoint(
    session: SessionDep, template_id: str, payload: PipelineTemplateUpdate
) -> PipelineTemplate:
    return templates.update_template(session, template_id, payload)


@router.post(
    "/templates/{template_id}/archive",
    response_model=PipelineTemplate,
)
def archive_template_endpoint(
    session: SessionDep,
    template_id: str,
    expected_version: int = Query(ge=1),
) -> PipelineTemplate:
    return templates.archive_template(session, template_id, expected_version)


@router.get("/templates/{template_id}/versions", response_model=list[PipelineVersion])
def list_versions_endpoint(session: SessionDep, template_id: str) -> list[PipelineVersion]:
    return templates.list_versions(session, template_id)


@router.put("/templates/{template_id}/draft", response_model=PipelineTemplate)
def update_draft_endpoint(
    session: SessionDep,
    template_id: str,
    payload: PipelineDraftUpdate,
) -> PipelineTemplate:
    return templates.update_draft(session, template_id, payload)


@router.post("/templates/{template_id}/publish", response_model=PipelineVersion, status_code=201)
def publish_draft_endpoint(
    session: SessionDep,
    template_id: str,
    payload: DraftPublish,
) -> PipelineVersion:
    return templates.publish_draft(session, template_id, payload.expected_version)


@router.post(
    "/templates/{template_id}/versions",
    response_model=PipelineVersion,
    status_code=201,
)
def create_version_endpoint(
    session: SessionDep, template_id: str, payload: PipelineVersionCreate
) -> PipelineVersion:
    return templates.create_version(session, template_id, payload)


@router.get("/versions/{version_id}", response_model=PipelineVersion)
def get_version_endpoint(session: SessionDep, version_id: str) -> PipelineVersion:
    return templates.get_version(session, version_id)


# ----------------------------------------------------------------------------- Bindings


@router.get("/bindings", response_model=list[PipelineBinding])
def list_bindings_endpoint(
    session: SessionDep,
    project_id: str | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> list[PipelineBinding]:
    return templates.list_bindings(session, project_id, include_archived=include_archived)


@router.post(
    "/versions/{version_id}/bindings",
    response_model=PipelineBinding,
    status_code=201,
)
def create_binding_endpoint(
    session: SessionDep, version_id: str, payload: PipelineBindingCreate
) -> PipelineBinding:
    return templates.create_binding(session, version_id, payload)


@router.get("/bindings/{binding_id}", response_model=PipelineBinding)
def get_binding_endpoint(session: SessionDep, binding_id: str) -> PipelineBinding:
    return templates.get_binding(session, binding_id)


@router.patch("/bindings/{binding_id}", response_model=PipelineBinding)
def update_binding_endpoint(
    session: SessionDep, binding_id: str, payload: PipelineBindingUpdate
) -> PipelineBinding:
    return templates.update_binding(session, binding_id, payload)


@router.post("/bindings/{binding_id}/archive", response_model=PipelineBinding)
def archive_binding_endpoint(
    session: SessionDep,
    binding_id: str,
    expected_version: int = Query(ge=1),
) -> PipelineBinding:
    return templates.archive_binding(session, binding_id, expected_version)


@router.get("/bindings/{binding_id}/resolved", response_model=ResolvedSettings)
def resolve_settings_endpoint(session: SessionDep, binding_id: str) -> ResolvedSettings:
    return templates.resolve_settings(session, binding_id)


@router.post("/bindings/{binding_id}/resolved", response_model=ResolvedSettings)
def resolve_run_settings_endpoint(
    session: SessionDep,
    binding_id: str,
    payload: SettingsOverrides,
) -> ResolvedSettings:
    return templates.resolve_settings(session, binding_id, payload)


# ----------------------------------------------------------------------------- Provider connections


@router.get("/connections", response_model=list[ProviderConnection])
def list_connections_endpoint(
    session: SessionDep, include_archived: bool = Query(default=False)
) -> list[ProviderConnection]:
    return connections.list_connections(session, include_archived=include_archived)


@router.post("/connections", response_model=ProviderConnection, status_code=201)
def create_connection_endpoint(
    session: SessionDep,
    secrets: SecretDep,
    payload: ProviderConnectionCreate,
) -> ProviderConnection:
    return connections.create_connection(session, secrets, payload)


@router.get("/connections/{connection_id}", response_model=ProviderConnection)
def get_connection_endpoint(session: SessionDep, connection_id: str) -> ProviderConnection:
    return connections.get_connection(session, connection_id)


@router.patch("/connections/{connection_id}", response_model=ProviderConnection)
def update_connection_endpoint(
    session: SessionDep,
    secrets: SecretDep,
    connection_id: str,
    payload: ProviderConnectionUpdate,
) -> ProviderConnection:
    return connections.update_connection(session, secrets, connection_id, payload)


@router.post("/connections/{connection_id}/archive", response_model=ProviderConnection)
def archive_connection_endpoint(
    session: SessionDep,
    connection_id: str,
    expected_version: int = Query(ge=1),
) -> ProviderConnection:
    return connections.archive_connection(session, connection_id, expected_version)


@router.post("/connections/{connection_id}/test", response_model=ProviderTest)
def test_connection_endpoint(
    session: SessionDep,
    connection_id: str,
    settings: SettingsDep,
) -> ProviderTest:
    """Run a smoke probe against the provider. Stage 7 expands it to real HTTP."""

    _ = settings
    from agents_ide.persistence.models import ProviderConnection as ProviderConnectionModel
    from agents_ide.services.mapping import get_or_404

    model = get_or_404(session, ProviderConnectionModel, connection_id)
    if model.archived_at is not None:
        raise AppError("connection_archived", "Архивное подключение недоступно", 409)
    raise AppError("not_implemented", "Проверка провайдера будет доступна на этапе 7", 501)


@router.get("/connections/{connection_id}/models")
def model_catalog_endpoint(session: SessionDep, connection_id: str) -> dict[str, Any]:
    return connections.model_catalog(session, connection_id)


# ----------------------------------------------------------------------------- Harness profiles


@router.get("/harness_profiles", response_model=list[HarnessProfile])
def list_harnesses_endpoint(
    session: SessionDep, include_archived: bool = Query(default=False)
) -> list[HarnessProfile]:
    return harness.list_harnesses(session, include_archived=include_archived)


@router.post("/harness_profiles", response_model=HarnessProfile, status_code=201)
def create_harness_endpoint(session: SessionDep, payload: HarnessProfileCreate) -> HarnessProfile:
    return harness.create_harness(session, payload)


@router.get("/harness_profiles/{harness_id}", response_model=HarnessProfile)
def get_harness_endpoint(session: SessionDep, harness_id: str) -> HarnessProfile:
    return harness.get_harness(session, harness_id)


@router.patch("/harness_profiles/{harness_id}", response_model=HarnessProfile)
def update_harness_endpoint(
    session: SessionDep, harness_id: str, payload: HarnessProfileUpdate
) -> HarnessProfile:
    return harness.update_harness(session, harness_id, payload)


@router.post("/harness_profiles/{harness_id}/archive", response_model=HarnessProfile)
def archive_harness_endpoint(
    session: SessionDep,
    harness_id: str,
    expected_version: int = Query(ge=1),
) -> HarnessProfile:
    return harness.archive_harness(session, harness_id, expected_version)


# ----------------------------------------------------------------------------- Runs


@router.get("/runs", response_model=list[Run])
def list_runs_endpoint(
    session: SessionDep,
    project_id: str | None = Query(default=None),
) -> list[Run]:
    return runs.list_runs(session, project_id=project_id)


@router.post("/runs", response_model=Run, status_code=201)
def start_run_endpoint(session: SessionDep, payload: RunStart) -> Run:
    return runs.start_run(session, payload)


@router.get("/runs/{run_id}", response_model=Run)
def get_run_endpoint(session: SessionDep, run_id: str) -> Run:
    return runs.get_run(session, run_id)


@router.post("/runs/{run_id}/commands", response_model=CommandAccepted)
def submit_command_endpoint(
    session: SessionDep, run_id: str, payload: RunCommand
) -> CommandAccepted:
    return runs.submit_command(session, run_id, payload)


@router.get("/runs/{run_id}/commands", response_model=list[CommandAccepted])
def list_command_journal_endpoint(session: SessionDep, run_id: str) -> list[CommandAccepted]:
    return runs.list_command_journal(session, run_id)


# ----------------------------------------------------------------------------- Model groups


@router.get("/model_groups", response_model=list[ModelGroup])
def list_model_groups_endpoint(
    session: SessionDep,
    kind: Literal["agent", "llm"] | None = Query(default=None),
    include_archived: bool = Query(default=False),
) -> list[ModelGroup]:
    return groups.list_groups(session, kind=kind, include_archived=include_archived)


@router.post("/model_groups/agent", response_model=ModelGroup, status_code=201)
def create_agent_group_endpoint(session: SessionDep, payload: ModelGroupAgentCreate) -> ModelGroup:
    return groups.create_group(session, payload)


@router.post("/model_groups/llm", response_model=ModelGroup, status_code=201)
def create_llm_group_endpoint(session: SessionDep, payload: ModelGroupLLMCreate) -> ModelGroup:
    return groups.create_group(session, payload)


@router.post("/model_groups/import", response_model=ModelGroup, status_code=201)
def import_model_group_endpoint(session: SessionDep, payload: ModelGroupImport) -> ModelGroup:
    return groups.import_group(session, payload)


@router.get("/model_groups/{group_id}/export", response_model=ModelGroupExport)
def export_model_group_endpoint(session: SessionDep, group_id: str) -> ModelGroupExport:
    return groups.export_group(session, group_id)


@router.get("/model_groups/{group_id}", response_model=ModelGroup)
def get_model_group_endpoint(session: SessionDep, group_id: str) -> ModelGroup:
    return groups.get_group(session, group_id)


@router.patch("/model_groups/{group_id}/agent", response_model=ModelGroup)
def update_agent_group_endpoint(
    session: SessionDep,
    group_id: str,
    payload: ModelGroupAgentUpdate,
) -> ModelGroup:
    return groups.update_group(session, group_id, payload)


@router.patch("/model_groups/{group_id}/llm", response_model=ModelGroup)
def update_llm_group_endpoint(
    session: SessionDep,
    group_id: str,
    payload: ModelGroupLLMUpdate,
) -> ModelGroup:
    return groups.update_group(session, group_id, payload)


@router.put("/model_groups/{group_id}/agent/members", response_model=ModelGroup)
def replace_agent_members_endpoint(
    session: SessionDep,
    group_id: str,
    payload: ModelGroupAgentMembersReplace,
) -> ModelGroup:
    return groups.replace_members(session, group_id, payload)


@router.put("/model_groups/{group_id}/llm/members", response_model=ModelGroup)
def replace_llm_members_endpoint(
    session: SessionDep,
    group_id: str,
    payload: ModelGroupLLMMembersReplace,
) -> ModelGroup:
    return groups.replace_members(session, group_id, payload)


@router.delete("/model_groups/{group_id}/members/{member_id}", response_model=ModelGroup)
def delete_member_endpoint(
    session: SessionDep,
    group_id: str,
    member_id: str,
    expected_revision: int = Query(ge=1),
) -> ModelGroup:
    payload = ModelGroupMemberDelete(expected_revision=expected_revision)
    return groups.delete_member(session, group_id, member_id, payload)


@router.post("/model_groups/{group_id}/archive", response_model=ModelGroup)
def archive_model_group_endpoint(
    session: SessionDep,
    group_id: str,
    expected_revision: int = Query(ge=1),
) -> ModelGroup:
    return groups.archive_group(session, group_id, expected_revision)


@router.post("/model_groups/{group_id}/copy", response_model=ModelGroup, status_code=201)
def copy_model_group_endpoint(
    session: SessionDep,
    group_id: str,
    payload: ModelGroupCopy,
) -> ModelGroup:
    return groups.copy_group(session, group_id, payload)


# ----- Node schemas for stage 3+


@router.get("/schema/nodes")
def node_schemas() -> dict[str, Any]:
    return _node_schemas_payload()


# ----- Graph validation / preflight

from agents_ide.domain.schemas import ApiModel  # noqa: E402


class GraphValidationRequest(ApiModel):
    graph: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    schema_version: str = "1.0.0"
    required_features: list[str] = Field(default_factory=list)
    settings: SettingsOverrides = Field(default_factory=SettingsOverrides)


class GraphImportRequest(ApiModel):
    """Permissive import body; ``trusted`` and ``execution_hash`` are ignored."""

    model_config = ConfigDict(extra="allow")
    graph: dict[str, Any] = Field(default_factory=dict)
    inputs: dict[str, Any] = Field(default_factory=dict)
    required_features: list[str] = Field(default_factory=list)
    schema_version: str = "1.0.0"
    settings: dict[str, Any] = Field(default_factory=dict)


class GraphImportResponse(ApiModel):
    ok: bool
    errors: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    graph_hash: str | None = None
    execution_hash: str | None = None
    features: list[str] = Field(default_factory=list)
    body: dict[str, Any] | None = None


@router.post("/graphs/validate")
def validate_graph_endpoint(
    session: SessionDep,
    payload: GraphValidationRequest,
) -> dict[str, Any]:
    """Validate a graph without persisting anything."""

    from agents_ide.domain.graph_validation import validate_with_session

    report: ValidationReport = validate_with_session(
        session, payload.graph, inputs=payload.inputs, settings=payload.settings
    )
    from agents_ide.domain.graph_validation import check_version_features

    check_version_features(payload.schema_version, payload.required_features, report)
    return report.to_dict()


@router.post("/graphs/export")
def export_graph_endpoint(payload: GraphValidationRequest) -> dict[str, Any]:
    """Return a portable export with a freshly computed execution_hash."""

    return export_graph_payload(
        payload.graph,
        inputs=payload.inputs,
        settings=payload.settings.model_dump(mode="json", exclude_none=True),
        schema_version=payload.schema_version,
        required_features=payload.required_features,
    )


@router.post("/graphs/import", response_model=GraphImportResponse)
def import_graph_endpoint(payload: GraphImportRequest) -> GraphImportResponse:
    """Validate an imported graph payload, ignoring client-supplied trust markers."""

    body, report = import_graph_payload(payload.model_dump())
    return GraphImportResponse(
        ok=report.ok,
        errors=[issue.to_dict() for issue in report.errors],
        warnings=[issue.to_dict() for issue in report.warnings],
        graph_hash=report.graph_hash,
        execution_hash=report.execution_hash,
        features=report.features,
        body=body if report.ok else None,
    )


@router.get("/versions/{version_id}/validate")
def validate_version_endpoint(session: SessionDep, version_id: str) -> dict[str, Any]:
    """Re-validate an immutable :class:`PipelineVersion` graph."""

    return validate_version(session, version_id).to_dict()


class PreflightRequest(ApiModel):
    inputs: dict[str, Any] = Field(default_factory=dict)
    overrides: SettingsOverrides = Field(default_factory=SettingsOverrides)


@router.post("/bindings/{binding_id}/preflight")
def preflight_endpoint(
    session: SessionDep, binding_id: str, payload: PreflightRequest | None = None
) -> dict[str, Any]:
    """Return the validation report and resolved capabilities for ``binding_id``."""

    from agents_ide.persistence.models import PipelineBinding as PipelineBindingModel
    from agents_ide.services.mapping import get_or_404

    binding_model = get_or_404(session, PipelineBindingModel, binding_id)
    report = preflight_binding(
        session,
        binding_model,
        inputs=payload.inputs if payload else None,
        overrides=payload.overrides if payload else None,
    )
    return report.to_dict()


@router.get("/schema/events")
def event_schemas() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "events": [
            "run.created",
            "run.state_changed",
            "run.waiting_input",
            "control.accepted",
            "control.applied",
            "control.rejected",
            "step.started",
            "step.completed",
            "step.failed",
            "step.attempt_started",
            "step.attempt_finished",
            "agent.message",
            "agent.tool_call",
            "command.started",
            "command.finished",
            "artifact.created",
            "context.collected",
            "condition.evaluated",
            "transition.selected",
            "plan.item_changed",
            "git.commit_intent_saved",
            "git.commit_created",
            "git.no_changes",
            "error.technical",
            "recovery.result",
            "budget.updated",
            "budget.exceeded",
            "model_group.candidate_selected",
            "model_group.candidate_skipped",
            "model_group.candidate_switched",
            "model_group.exhausted",
            "stream.gap",
        ],
    }


@router.get("/capabilities")
def capabilities(settings: SettingsDep) -> dict[str, Any]:
    from agents_ide import __version__
    from agents_ide.domain.graph_schema import GRAPH_LIMITS, SUPPORTED_FEATURES

    return {
        "engine_version": __version__,
        "supported_schema": ["1.0.0"],
        "features": [
            "projects",
            "chats",
            "messages",
            "templates",
            "bindings",
            "provider_connections",
            "model_groups",
        ],
        "limits": {"max_projects": 1024, "max_chats_per_project": 256},
        "graph_limits": GRAPH_LIMITS,
        "supported_graph_features": sorted(SUPPORTED_FEATURES),
        "runtime_execution": "unimplemented",
        "adapter_capabilities": "unverified",
        "frontend_origin": settings.allowed_origins,
    }
