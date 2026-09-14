"""API router wiring domain services to HTTP endpoints."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from agents_ide.api.deps import get_secret_store, get_session, get_settings
from agents_ide.config import Settings
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


# ----- Node schemas for stage 3+


_NODE_SCHEMAS: dict[str, Any] = {
    "Start": {
        "type": "object",
        "required": ["id", "type"],
        "properties": {"id": {"type": "string"}, "type": {"const": "Start"}},
        "additionalProperties": False,
    },
    "End": {
        "type": "object",
        "required": ["id", "type"],
        "properties": {"id": {"type": "string"}, "type": {"const": "End"}},
        "additionalProperties": False,
    },
    "AgentTask": {
        "type": "object",
        "required": ["id", "type", "config"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "AgentTask"},
            "config": {
                "type": "object",
                "required": ["role", "prompt"],
                "properties": {
                    "role": {"type": "string"},
                    "prompt": {"type": "string", "maxLength": 65536},
                },
            },
        },
    },
    "LLMRequest": {
        "type": "object",
        "required": ["id", "type", "config"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "LLMRequest"},
            "config": {
                "type": "object",
                "required": ["connection_id", "model", "prompt"],
                "properties": {
                    "connection_id": {"type": "string"},
                    "model": {"type": "string"},
                    "prompt": {"type": "string"},
                },
            },
        },
    },
    "Command": {
        "type": "object",
        "required": ["id", "type", "config"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "Command"},
            "config": {
                "type": "object",
                "required": ["commands"],
                "properties": {
                    "commands": {
                        "type": "array",
                        "maxItems": 50,
                        "items": {
                            "type": "object",
                            "required": ["program", "args"],
                            "properties": {
                                "id": {"type": "string"},
                                "program": {"type": "string"},
                                "args": {
                                    "type": "array",
                                    "items": {"type": "string"},
                                },
                                "cwd": {"type": "string"},
                                "env": {"type": "object"},
                                "required": {"type": "boolean"},
                                "success_exit_codes": {
                                    "type": "array",
                                    "items": {"type": "integer"},
                                },
                                "timeout_seconds": {"type": "integer"},
                                "max_output_bytes": {"type": "integer"},
                                "retry_safety": {
                                    "enum": ["safe", "unsafe"],
                                },
                            },
                        },
                    },
                    "failure_policy": {"enum": ["collect_all", "stop_on_failure"]},
                },
            },
        },
    },
    "CollectContext": {
        "type": "object",
        "required": ["id", "type", "config"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "CollectContext"},
            "config": {
                "type": "object",
                "properties": {
                    "mode": {"enum": ["collect", "resolve_requests"]},
                    "sources": {"type": "array"},
                    "context_paths": {"type": "array"},
                },
            },
        },
    },
    "GitCommit": {
        "type": "object",
        "required": ["id", "type"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "GitCommit"},
        },
    },
    "Condition": {
        "type": "object",
        "required": ["id", "type", "expression"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "Condition"},
            "expression": {"type": "object"},
        },
    },
    "PlanControl": {
        "type": "object",
        "required": ["id", "type", "operation"],
        "properties": {
            "id": {"type": "string"},
            "type": {"const": "PlanControl"},
            "operation": {
                "enum": ["select_next", "record_verified", "record_final_check", "attach_commit"],
            },
        },
    },
}


@router.get("/schema/nodes")
def node_schemas() -> dict[str, Any]:
    return {
        "schema_version": "1.0.0",
        "nodes": _NODE_SCHEMAS,
        "limits": {
            "max_nodes": 200,
            "max_edges": 400,
            "max_prompt_bytes": 65536,
            "max_graph_bytes": 1048576,
        },
    }


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
            "stream.gap",
        ],
    }


@router.get("/capabilities")
def capabilities(settings: SettingsDep) -> dict[str, Any]:
    from agents_ide import __version__

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
        ],
        "limits": {"max_projects": 1024, "max_chats_per_project": 256},
        "frontend_origin": settings.allowed_origins,
    }
