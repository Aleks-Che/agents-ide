"""API router wiring domain services to HTTP endpoints."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from dataclasses import asdict
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import ConfigDict, Field
from sqlalchemy.orm import Session

from agents_ide.adapters.fake import FakeScenarioSpec
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
from agents_ide.domain.planning import (  # noqa: E402
    PlanningAnswersAccepted as PlanningAnswersAcceptedSchema,
)
from agents_ide.domain.planning import (
    PlanningAnswersSubmit,
    PlanningCancelRequest,
    PlanningConfirmRequest,
    PlanningJobCreate,
    PlanningJobView,
    PlanningPromoteSingle,
    PlanningPromoteSingleRequest,
    PlanningRetried,
    PlanningRetryRequest,
)
from agents_ide.domain.planning import (
    PlanningConfirmed as PlanningConfirmedSchema,
)
from agents_ide.domain.schemas import (
    ApiModel,
    Chat,
    ChatArchive,
    ChatCreate,
    ChatUpdate,
    CommandAccepted,
    DraftPublish,
    HarnessProbe,
    HarnessProfile,
    HarnessProfileCatalog,
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
    PlanningSource,
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
    SingleAgentSpec,
)
from agents_ide.engine.events import event_catalog
from agents_ide.engine.events_stream import (
    ArtifactContent,
    ArtifactView,
    EventBatchResponse,
    RunSnapshot,
    fetch_events_after,
    fetch_full_history,
    format_sse,
    is_terminal,
)
from agents_ide.errors import AppError
from agents_ide.security.auth import COOKIE_NAME
from agents_ide.security.secrets import SecretStore
from agents_ide.services import (
    chats,
    connections,
    groups,
    harness,
    planning,
    projects,
    runs,
    templates,
)
from agents_ide.services.run_observation import HistoryCategory, HistoryPage, read_history

router = APIRouter(prefix="/api", tags=["domain"])


SessionDep = Annotated[Session, Depends(get_session, scope="function")]
SecretDep = Annotated[SecretStore, Depends(get_secret_store)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


@router.get("/presets")
def list_presets_endpoint() -> list[dict[str, Any]]:
    from agents_ide.domain.common import content_hash
    from agents_ide.services.presets import list_builtin_presets

    return [
        {
            "id": p.preset_id,
            "name": p.name,
            "description": p.description,
            "template_id": content_hash({"builtin_preset": p.preset_id})[:32],
            "preset_version": p.body["preset_version"],
            "roles": p.graph["roles"],
        }
        for p in list_builtin_presets()
    ]


class PresetCopyRequest(ApiModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)


@router.post("/presets/{preset_id}/copy", response_model=PipelineTemplate, status_code=201)
def copy_preset_endpoint(
    session: SessionDep, preset_id: str, payload: PresetCopyRequest | None = None
) -> PipelineTemplate:
    name = payload.name if payload else None
    return templates.copy_preset_to_user_template(session, preset_id, name)


@router.get("/runs/{run_id}/plan")
def run_plan_endpoint(session: SessionDep, run_id: str) -> dict[str, Any]:
    from agents_ide.engine.plan_control import load_plan_items, plan_summary
    from agents_ide.persistence.models import Run as RunRow

    row = session.get(RunRow, run_id)
    if row is None:
        raise AppError("run_not_found", "Run not found", 404)
    return {
        "plan": json.loads(row.snapshot_json).get("plan"),
        "summary": plan_summary(session, run_id).as_work(),
        "items": [
            {
                "id": i.item_id,
                "order": i.order_index,
                "title": i.title,
                "acceptance_criteria": json.loads(i.acceptance_criteria_json),
                "status": i.status,
                "evidence_ids": json.loads(i.evidence_ids_json),
                "commit_shas": json.loads(i.commit_shas_json),
                "related_execution_ids": json.loads(i.related_execution_ids_json),
            }
            for i in load_plan_items(session, run_id)
        ],
    }


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
    session: SessionDep,
    chat_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
    latest: bool = False,
    before_id: str | None = None,
) -> list[Message]:
    return chats.list_messages(session, chat_id, limit=limit, latest=latest, before_id=before_id)


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
    secrets: SecretDep,
    connection_id: str,
) -> ProviderTest:
    """Run an explicit smoke probe: HTTP call plus optional catalog request."""

    return connections.test_connection(session, secrets, connection_id)


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


@router.get("/harness_profiles/{harness_id}/models", response_model=HarnessProfileCatalog)
def harness_models_endpoint(session: SessionDep, harness_id: str) -> HarnessProfileCatalog:
    return harness.model_catalog(session, harness_id)


@router.post("/harness_profiles/{harness_id}/test", response_model=HarnessProbe)
def harness_test_endpoint(session: SessionDep, harness_id: str) -> HarnessProbe:
    return harness.probe_harness(session, harness_id)


# ----------------------------------------------------------------------------- Runs


@router.get("/runs", response_model=list[Run])
def list_runs_endpoint(
    session: SessionDep,
    project_id: str | None = Query(default=None),
    chat_id: str | None = Query(default=None),
) -> list[Run]:
    return runs.list_runs(session, project_id=project_id, chat_id=chat_id)


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
def list_command_journal_endpoint(
    session: SessionDep,
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[CommandAccepted]:
    return runs.list_command_journal(session, run_id, offset=offset, limit=limit)


class ReservationCleanupRequest(ApiModel):
    command_id: str = Field(min_length=1, max_length=64)
    expected_state_version: int = Field(ge=0)


@router.post("/runs/{run_id}/reservations/cleanup")
def cleanup_reservation_endpoint(
    session: SessionDep, run_id: str, payload: ReservationCleanupRequest
) -> dict[str, Any]:
    from agents_ide.services.run_controls import cleanup_reservation

    return cleanup_reservation(session, run_id, payload.command_id, payload.expected_state_version)


@router.get("/runs/{run_id}/diagnostics")
def run_diagnostics_endpoint(session: SessionDep, run_id: str) -> dict[str, Any]:
    from sqlalchemy import select

    from agents_ide.persistence.models import (
        ProcessSupervision,
        StepAttempt,
        WorkspaceReservation,
    )
    from agents_ide.persistence.models import (
        Run as RunModel,
    )
    from agents_ide.worker.processes import process_state

    run = session.get(RunModel, run_id)
    if run is None:
        raise AppError("run_not_found", "Run не найден", 404)
    attempt = session.get(StepAttempt, run.current_attempt_id) if run.current_attempt_id else None
    return {
        "run_id": run.id,
        "state": run.state,
        "state_version": run.state_version,
        "resume_target": json.loads(run.resume_target_json or "{}"),
        "stop_goal": run.stop_goal,
        "waiting_reason": json.loads(run.waiting_reason_json or "null"),
        "attempt": {
            "id": attempt.id,
            "status": attempt.status,
            "heartbeat_at": attempt.heartbeat_at,
            "operation_id": attempt.operation_id,
        }
        if attempt
        else None,
        "processes": [
            {
                "id": p.id,
                "pid": p.pid,
                "create_time": p.create_time,
                "owner_generation": p.owner_generation,
                "state": p.state,
                "health": process_state(p.pid, p.create_time),
                "last_health_ok": p.last_health_ok,
                "last_external_event_at": p.last_external_event_at,
                "transport": p.transport,
                "port": p.port,
            }
            for p in session.scalars(
                select(ProcessSupervision).where(ProcessSupervision.run_id == run_id)
            )
        ],
        "reservation_ids": list(
            session.scalars(
                select(WorkspaceReservation.id).where(
                    WorkspaceReservation.run_id == run_id,
                    WorkspaceReservation.released_at.is_(None),
                )
            )
        ),
    }


@router.get("/runs/{run_id}/snapshot", response_model=RunSnapshot)
def run_snapshot_endpoint(session: SessionDep, run_id: str) -> dict[str, Any]:
    from sqlalchemy import func, select

    from agents_ide.persistence.models import Run as RunModel
    from agents_ide.persistence.models import RunEvent
    from agents_ide.services.run_observation import build_observation
    from agents_ide.services.run_selection import build_selection_summary

    session.connection().exec_driver_sql("BEGIN")
    run_row = session.get(RunModel, run_id)
    if run_row is None:
        raise AppError("run_not_found", "Run не найден", 404)
    run = runs.get_run(session, run_id)
    minimum, highest = session.execute(
        select(func.min(RunEvent.sequence), func.max(RunEvent.sequence)).where(
            RunEvent.run_id == run_id
        )
    ).one()
    selection = build_selection_summary(session, run_row)
    return {
        "run": run,
        "last_sequence": highest or 0,
        "min_retained_sequence": max(minimum or 0, run_row.retention_sequence),
        "selection": selection,
        "observation": build_observation(session, run_row),
        "planning_provenance": json.loads(run_row.snapshot_json).get("planning_provenance"),
    }


@router.get("/runs/{run_id}/artifacts", response_model=list[ArtifactView])
def run_artifacts_endpoint(
    session: SessionDep,
    run_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=100, ge=1, le=200),
) -> list[dict[str, Any]]:
    from sqlalchemy import select
    from sqlalchemy.orm import defer

    from agents_ide.persistence.models import ArtifactManifest

    runs.get_run(session, run_id)
    return [
        _artifact_view(row, include_body=False)
        for row in session.scalars(
            select(ArtifactManifest)
            .options(defer(ArtifactManifest.body_json))
            .where(ArtifactManifest.run_id == run_id)
            .order_by(ArtifactManifest.created_at, ArtifactManifest.id)
            .offset(offset)
            .limit(limit)
        )
    ]


@router.get("/runs/{run_id}/artifacts/{artifact_id}", response_model=ArtifactView)
def run_artifact_endpoint(session: SessionDep, run_id: str, artifact_id: str) -> dict[str, Any]:
    from agents_ide.persistence.models import ArtifactManifest

    row = session.get(ArtifactManifest, artifact_id)
    if row is None or row.run_id != run_id:
        raise AppError("artifact_not_found", "Артефакт не найден", 404)
    if row.purged_at is not None:
        raise AppError("artifact_expired", "Подробный вывод удалён по retention", 410)
    return _artifact_view(row, include_body=True)


@router.get("/runs/{run_id}/artifacts/{artifact_id}/content", response_model=ArtifactContent)
def run_artifact_content_endpoint(
    session: SessionDep,
    run_id: str,
    artifact_id: str,
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=16000, ge=1, le=16000),
    format: Literal["text", "json"] = "text",
) -> dict[str, Any]:
    from agents_ide.persistence.models import ArtifactManifest

    row = session.get(ArtifactManifest, artifact_id)
    if row is None or row.run_id != run_id:
        raise AppError("artifact_not_found", "Артефакт не найден", 404)
    if row.purged_at is not None:
        raise AppError("artifact_expired", "Подробный вывод удалён по retention", 410)
    body = json.loads(row.body_json) if row.body_json else None
    if format == "text" and isinstance(body, str):
        text = body
    elif format == "text" and isinstance(body, dict):
        text = "\n\n".join(
            f"{key}:\n"
            + (value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2))
            for key, value in body.items()
        )
    else:
        text = json.dumps(body, ensure_ascii=False, indent=2)
    return {
        "artifact": _artifact_view(row, include_body=False),
        "text": text[offset : offset + limit],
        "offset": offset,
        "total_chars": len(text),
    }


def _artifact_view(row: Any, *, include_body: bool) -> dict[str, Any]:
    return {
        "id": row.id,
        "run_id": row.run_id,
        "schema_type": row.schema_type,
        "source_kind": row.source_kind,
        "byte_length": row.byte_length,
        "content_hash": row.content_hash,
        "step_execution_id": row.step_execution_id,
        "step_attempt_id": row.step_attempt_id,
        "body": json.loads(row.body_json) if include_body and row.body_json else None,
        "redaction": json.loads(row.redaction_json or "[]"),
        "truncation": json.loads(row.truncation_json) if row.truncation_json else None,
    }


@router.get(
    "/runs/{run_id}/events", response_model=EventBatchResponse, response_model_exclude_none=True
)
def list_run_events_endpoint(
    session: SessionDep,
    run_id: str,
    after: int = Query(default=0, ge=0, le=2**63 - 1),
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    batch = fetch_events_after(session, run_id, after_sequence=after, limit=limit)
    return asdict(batch)


@router.get(
    "/runs/{run_id}/events/replay",
    response_model=EventBatchResponse,
    response_model_exclude_none=True,
)
def replay_run_events_endpoint(
    session: SessionDep,
    run_id: str,
    limit: int = Query(default=200, ge=1, le=1000),
) -> dict[str, Any]:
    """Read the first retained page; paginate using last_sequence."""

    batch = fetch_full_history(session, run_id, limit=limit)
    return asdict(batch)


@router.get("/runs/{run_id}/history", response_model=HistoryPage)
def run_history_endpoint(
    session: SessionDep,
    run_id: str,
    before: int | None = Query(default=None, ge=1, le=2**63 - 1),
    limit: int = Query(default=200, ge=1, le=200),
    category: HistoryCategory = "all",
    node_id: str | None = Query(default=None, max_length=64),
    execution_id: str | None = Query(default=None, max_length=32),
) -> HistoryPage:
    return read_history(
        session,
        run_id,
        before=before,
        limit=limit,
        category=category,
        node_id=node_id,
        execution_id=execution_id,
    )


@router.get("/runs/{run_id}/stream")
async def run_event_stream_endpoint(
    request: Request,
    session: SessionDep,
    run_id: str,
    after: int = Query(default=0, ge=0, le=2**63 - 1),
) -> StreamingResponse:
    """SSE stream of durable Run events with cookie-auth and reset_required."""

    cursor = after
    if (header := request.headers.get("last-event-id")) is not None:
        if (
            not header.isascii()
            or not header.isdigit()
            or len(header) > 19
            or int(header) > 2**63 - 1
        ):
            raise AppError("cursor_invalid", "Некорректный Last-Event-ID", 400)
        cursor = int(header)
    fetch_events_after(session, run_id, after_sequence=cursor)  # 404 before opening SSE.
    hub = request.app.state.run_streams
    token = request.cookies.get(COOKIE_NAME)

    async def event_gen() -> AsyncIterator[str]:
        subscription = await hub.subscribe(run_id)
        last_sequence = cursor
        heartbeat = asyncio.get_running_loop().time()
        try:
            yield f"event: stream.opened\ndata: {json.dumps({'last_sequence': cursor})}\n\n"
            replay = True
            while not await request.is_disconnected():
                try:
                    await asyncio.to_thread(request.app.state.auth.authenticate, token)
                except AppError:
                    yield "event: auth.expired\ndata: {}\n\n"
                    return
                if replay:
                    batch = await asyncio.to_thread(hub.read, run_id, last_sequence)
                else:
                    try:
                        batch = await asyncio.wait_for(subscription.get(), timeout=1)
                    except TimeoutError:
                        if asyncio.get_running_loop().time() - heartbeat >= 15:
                            heartbeat = asyncio.get_running_loop().time()
                            yield ": heartbeat\n\n"
                        continue
                if batch.reset_required or subscription.overflow:
                    reason = "slow_consumer" if subscription.overflow else "cursor_unavailable"
                    payload = {"reason": reason, "snapshot_url": f"/api/runs/{run_id}/snapshot"}
                    yield f"event: stream.reset_required\ndata: {json.dumps(payload)}\n\n"
                    return
                unseen = [event for event in batch.events if event["sequence"] > last_sequence]
                for chunk in format_sse(unseen):
                    yield chunk
                if unseen:
                    last_sequence = unseen[-1]["sequence"]
                if is_terminal(batch.final_state or "") and not batch.has_more:
                    closed_payload = {
                        "final_state": batch.final_state,
                        "last_sequence": last_sequence,
                    }
                    yield f"event: stream.closed\ndata: {json.dumps(closed_payload)}\n\n"
                    return
                replay = replay and batch.has_more
        finally:
            await hub.unsubscribe(run_id, subscription)

    return StreamingResponse(
        event_gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-store",
            "X-Accel-Buffering": "no",
        },
    )


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
    execution_mode: Literal["real", "simulated"] = "real"
    fake_scenario: FakeScenarioSpec | None = None
    inputs: dict[str, Any] = Field(default_factory=dict)
    overrides: SettingsOverrides = Field(default_factory=SettingsOverrides)
    single_agent: SingleAgentSpec | None = None
    planning_source: PlanningSource | None = None


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
        execution_mode=payload.execution_mode if payload else "real",
        fake_scenario=payload.fake_scenario.model_dump(mode="json")
        if payload and payload.fake_scenario
        else None,
        single_agent=payload.single_agent if payload else None,
        planning_source=payload.planning_source if payload else None,
    )
    return report.to_dict()


@router.get("/schema/events")
def event_schemas() -> dict[str, Any]:
    catalog = event_catalog()
    return {"schema_version": "1.0.0", "events": catalog["types"], **catalog}


# ----------------------------------------------------------------------------- Planning (Council)


@router.get("/planning_jobs", response_model=list[PlanningJobView])
def list_planning_jobs_endpoint(
    session: SessionDep,
    project_id: str | None = Query(default=None),
    chat_id: str | None = Query(default=None),
    include_completed: bool = Query(default=False),
) -> list[PlanningJobView]:
    return planning.list_planning_jobs(
        session,
        project_id=project_id,
        chat_id=chat_id,
        include_completed=include_completed,
    )


@router.post(
    "/planning_jobs",
    response_model=PlanningJobView,
    status_code=201,
)
def create_planning_job_endpoint(
    session: SessionDep, payload: PlanningJobCreate
) -> PlanningJobView:
    job = planning.create_planning_job(session, payload)
    return planning.load_planning_view(session, job.id)


@router.get("/planning_jobs/{job_id}", response_model=PlanningJobView)
def get_planning_job_endpoint(session: SessionDep, job_id: str) -> PlanningJobView:
    return planning.load_planning_view(session, job_id)


@router.post("/planning_jobs/{job_id}/cancel", response_model=PlanningJobView)
def cancel_planning_job_endpoint(
    session: SessionDep, job_id: str, payload: PlanningCancelRequest
) -> PlanningJobView:
    planning.cancel_planning_job(session, job_id, payload)
    return planning.load_planning_view(session, job_id)


@router.post("/planning_jobs/{job_id}/retry", response_model=PlanningRetried)
def retry_planning_job_endpoint(
    session: SessionDep, job_id: str, payload: PlanningRetryRequest
) -> PlanningRetried:
    return planning.retry_planning_job(session, job_id, payload)


@router.post(
    "/planning_jobs/{job_id}/promote_single",
    response_model=PlanningPromoteSingle,
)
def promote_single_planning_endpoint(
    session: SessionDep, job_id: str, payload: PlanningPromoteSingleRequest
) -> PlanningPromoteSingle:
    """Promote a single accepted draft to a revision after a quorum loss.

    The caller must explicitly set ``confirm_degraded``. Without that flag
    the request is rejected even if exactly one accepted draft exists. The
    resulting revision is recorded with ``author='single_member'`` and the
    job keeps ``degraded=true`` so downstream consumers can distinguish a
    degraded Council from a consensus plan.
    """
    return planning.promote_single_member_plan(session, job_id, payload)


@router.post(
    "/planning_jobs/{job_id}/answers",
    response_model=PlanningAnswersAcceptedSchema,
)
def submit_planning_answers_endpoint(
    session: SessionDep, job_id: str, payload: PlanningAnswersSubmit
) -> PlanningAnswersAcceptedSchema:
    return planning.submit_answers(session, job_id, payload)


@router.post(
    "/planning_jobs/{job_id}/confirm",
    response_model=PlanningConfirmedSchema,
)
def confirm_planning_endpoint(
    session: SessionDep, job_id: str, payload: PlanningConfirmRequest
) -> PlanningConfirmedSchema:
    return planning.confirm_planning(session, job_id, payload)


@router.get(
    "/planning_jobs/{job_id}/hash",
    response_model=dict[str, Any],
)
def planning_confirmation_hash_endpoint(
    session: SessionDep,
    job_id: str,
    revision_number: int = Query(ge=1),
) -> dict[str, Any]:
    """Return the confirmation hash a client must echo back at confirm time."""

    return planning.confirmation_hash_for_view(session, job_id, revision_number)


# ---- Capabilities and events


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
            "council_planning",
        ],
        "limits": {"max_projects": 1024, "max_chats_per_project": 256},
        "graph_limits": GRAPH_LIMITS,
        "supported_graph_features": sorted(SUPPORTED_FEATURES),
        "runtime_execution": "simulated",
        "real_execution": "unimplemented",
        "adapter_capabilities": "unverified",
        "frontend_origin": settings.allowed_origins,
    }
