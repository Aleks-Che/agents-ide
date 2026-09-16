"""Run service: idempotent start, immutable snapshot, command journal and
workspace reservations.

The snapshot is computed once on start and stored verbatim, including the
``execution_hash``, ``policy_hash`` and resolved settings. Subsequent edits to
the binding or template must never mutate an existing Run's snapshot.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import (
    content_hash,
    new_id,
    payload_hash,
    to_json,
    utc_now,
)
from agents_ide.domain.contracts import RunState
from agents_ide.domain.schemas import (
    CommandAccepted,
    Run,
    RunCommand,
    RunStart,
)
from agents_ide.domain.single_agent import (
    build_single_agent_graph,
    filter_single_agent_configuration,
)
from agents_ide.domain.workspace import (
    assert_identity_matches,
    collect_workspace,
    scopes_overlap,
    workspace_scope,
)
from agents_ide.errors import AppError
from agents_ide.persistence.models import Chat as ChatModel
from agents_ide.persistence.models import (
    CommandJournal as CommandJournalModel,
)
from agents_ide.persistence.models import Message as MessageModel
from agents_ide.persistence.models import (
    PipelineBinding as PipelineBindingModel,
)
from agents_ide.persistence.models import (
    PipelineVersion as PipelineVersionModel,
)
from agents_ide.persistence.models import (
    Project as ProjectModel,
)
from agents_ide.persistence.models import (
    QueueJob as QueueJobModel,
)
from agents_ide.persistence.models import (
    Run as RunModel,
)
from agents_ide.persistence.models import (
    RunEvent as RunEventModel,
)
from agents_ide.persistence.models import (
    WorkspaceReservation as WorkspaceReservationModel,
)
from agents_ide.services.mapping import get_or_404
from agents_ide.services.settings import (
    capture_dependencies,
    execution_hash,
    policy_hash,
    resolve_configuration,
)
from agents_ide.services.transactions import begin_write

_RUN_STATE_TERMINAL = {"completed", "failed", "cancelled"}


def _replay_start(model: RunModel, request_hash_value: str) -> Run:
    if model.request_hash is None:
        raise AppError("idempotency_unverifiable", "Старый Run не хранит исходный запрос", 409)
    if model.request_hash != request_hash_value:
        raise AppError("idempotency_mismatch", "Тот же ключ использован с другим запросом", 409)
    return _run_from_model(model)


def reserve_workspace(session: Session, run_id: str, generation: int) -> bool:
    """Worker-facing acquisition. A conflict leaves the Run queued.

    Must be called in a fresh session before external execution. Releasing a
    reservation requires proven process-tree termination (stage 5), never TTL alone.
    """
    run = get_or_404(session, RunModel, run_id)
    snapshot = json.loads(run.snapshot_json)
    workspace = snapshot["workspace"]
    _, normalized, dev, ino, git = collect_workspace(workspace["workspace_path"])
    scope = workspace_scope(Path(normalized), git)
    if [dev, ino] != [workspace["identity_dev"], workspace["identity_ino"]]:
        raise AppError("workspace_conflict", "Идентичность рабочего каталога изменилась", 409)
    if workspace.get("scope", {}).get("git_common_identity") != scope["git_common_identity"]:
        raise AppError("workspace_conflict", "Git checkout изменился или не был зафиксирован", 409)
    session.rollback()
    begin_write(session)
    run = get_or_404(session, RunModel, run_id)
    assert_identity_matches(Path(normalized), dev, ino)
    if run.state in _RUN_STATE_TERMINAL or generation < 1:
        raise AppError("reservation_not_allowed", "Run не может получить резервацию", 409)
    owned = None
    for reservation in session.scalars(
        select(WorkspaceReservationModel).where(WorkspaceReservationModel.released_at.is_(None))
    ):
        if reservation.workspace_json is None:
            return False  # Old scope is unknown; explicit recovery must resolve it.
        if reservation.run_id == run_id:
            owned = reservation
            continue
        if scopes_overlap(scope, json.loads(reservation.workspace_json)):
            return False
    if owned is not None:
        return owned.owner_generation == generation
    session.add(
        WorkspaceReservationModel(
            id=new_id(),
            workspace_identity_dev=dev,
            workspace_identity_ino=ino,
            workspace_json=to_json(scope),
            run_id=run_id,
            owner_generation=generation,
            lease_expires_at=None,
            created_at=utc_now(),
            released_at=None,
        )
    )
    session.flush()
    return True


def start_run(session: Session, payload: RunStart) -> Run:
    request = payload.model_dump(mode="json", exclude_none=True)
    request_hash_value = content_hash(request)
    existing = session.scalar(
        select(RunModel).where(RunModel.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        return _replay_start(existing, request_hash_value)
    registered = get_or_404(session, ProjectModel, payload.project_id)
    # Git/volume probes finish before the short write transaction.
    _, normalized, dev, ino, git = collect_workspace(registered.workspace_entered_path)
    scope = workspace_scope(Path(normalized), git)
    if (data_dir := session.info.get("data_dir")) and scopes_overlap(
        scope, workspace_scope(data_dir)
    ):
        raise AppError("workspace_conflict", "Рабочая область пересекает данные приложения", 409)
    session.rollback()
    begin_write(session)
    existing = session.scalar(
        select(RunModel).where(RunModel.idempotency_key == payload.idempotency_key)
    )
    if existing is not None:
        return _replay_start(existing, request_hash_value)
    binding = get_or_404(session, PipelineBindingModel, payload.binding_id)
    if binding.archived_at is not None:
        raise AppError("binding_archived", "Архивная привязка не запускается", 409)
    version = get_or_404(session, PipelineVersionModel, binding.version_id)
    project = get_or_404(session, ProjectModel, binding.project_id)
    if payload.project_id != project.id:
        raise AppError(
            "project_mismatch",
            "Привязка принадлежит другому проекту",
            400,
        )
    if project.archived_at is not None:
        raise AppError("project_archived", "Архивный проект не запускается", 409)
    if (dev, ino) != (project.workspace_identity_dev, project.workspace_identity_ino):
        raise AppError("workspace_conflict", "Идентичность рабочего каталога изменилась", 409)
    assert_identity_matches(Path(normalized), dev, ino)
    messages = []
    if payload.chat_id:
        chat = get_or_404(session, ChatModel, payload.chat_id)
        if chat.project_id != project.id or chat.archived_at is not None:
            raise AppError(
                "chat_unavailable", "Чат архивирован или принадлежит другому проекту", 409
            )
        messages = [
            {"id": row.id, "role": row.role, "content": row.content}
            for row in session.scalars(
                select(MessageModel)
                .where(MessageModel.chat_id == chat.id, MessageModel.archived_at.is_(None))
                .order_by(MessageModel.created_at, MessageModel.id)
            )
        ]
    configuration, sources = resolve_configuration(binding, version, payload.overrides)
    synthetic_graph = None
    if payload.single_agent is not None:
        synthetic_graph = build_single_agent_graph(version, payload.single_agent)
        filter_single_agent_configuration(
            configuration, sources, payload.single_agent, synthetic_graph
        )
    # Inject confirmed Council plan into inputs when planning_source is present.
    if payload.planning_source is not None:
        from agents_ide.services.planning import planning_inputs

        payload = payload.model_copy(
            update={
                "inputs": planning_inputs(
                    session, payload.planning_source, payload.project_id, payload.inputs
                )
            }
        )
    from agents_ide.domain.graph_validation import preflight as preflight_binding

    report = preflight_binding(
        session,
        binding,
        inputs=payload.inputs,
        overrides=payload.overrides,
        execution_mode=payload.execution_mode,
        fake_scenario=payload.fake_scenario.model_dump(mode="json")
        if payload.fake_scenario
        else None,
        workspace_state=(project.workspace_entered_path, normalized, dev, ino, git),
        single_agent=payload.single_agent,
    )
    if not report.ok:
        if len(report.errors) == 1 and report.errors[0].code in {
            "model_group_unavailable",
            "harness_unavailable",
            "connection_unavailable",
        }:
            issue = report.errors[0]
            raise AppError(issue.code, issue.message, 409, issue.details)
        raise AppError(
            "graph_validation_failed",
            "Граф не прошёл preflight",
            422,
            {"errors": [issue.to_dict() for issue in report.errors]},
        )
    if version.origin == "imported" and payload.trusted_execution_hash != report.execution_hash:
        raise AppError(
            "import_trust_required",
            "Подтвердите итоговый execution_hash из preflight",
            409,
            {"execution_hash": report.execution_hash},
        )
    if (
        payload.trusted_execution_hash is not None
        and payload.trusted_execution_hash != report.execution_hash
    ):
        raise AppError(
            "execution_hash_changed", "Исполняемая конфигурация изменилась после preflight", 409
        )
    snapshot = _build_snapshot(project, version)
    if payload.single_agent is not None:
        snapshot["single_agent"] = {
            "role": payload.single_agent.role,
            "selection": payload.single_agent.selection.model_dump(mode="json", exclude_none=True)
            if payload.single_agent.selection is not None
            else None,
            "parameters": payload.single_agent.parameters,
            **({"node_id": payload.single_agent.node_id} if payload.single_agent.node_id else {}),
        }
        snapshot["graph"] = synthetic_graph
        snapshot["required_features"] = sorted(
            set(snapshot.get("required_features", [])) | {"single_agent"}
        )
    if payload.planning_source is not None:
        snapshot["planning_source"] = payload.planning_source.model_dump(mode="json")
        snapshot["planning_provenance"] = payload.inputs["planning_provenance"]
    snapshot["execution_mode"] = payload.execution_mode
    snapshot["fake_scenario"] = (
        payload.fake_scenario.model_dump(mode="json") if payload.fake_scenario else None
    )
    snapshot["origin"] = version.origin
    snapshot["trusted_execution_hash"] = payload.trusted_execution_hash
    snapshot["input"] = {
        "message": payload.message,
        "messages": messages,
        "values": {**json.loads(version.inputs_json), **payload.inputs},
    }
    snapshot["resolved_settings"] = configuration
    snapshot["setting_sources"] = sources
    snapshot["dependencies"] = capture_dependencies(session, snapshot["graph"], configuration)
    if report.preview.get("command_programs"):
        snapshot["dependencies"]["command_programs"] = report.preview["command_programs"]
    if report.preview.get("git_dependencies"):
        snapshot["dependencies"]["git"] = report.preview["git_dependencies"]
    if report.preview.get("plan"):
        snapshot["plan"] = report.preview["plan"]
    snapshot["pipeline_execution_hash"] = version.execution_hash
    snapshot["execution_hash"] = execution_hash(
        version,
        configuration,
        snapshot["dependencies"],
        snapshot["input"]["values"],
        execution_mode=payload.execution_mode,
        fake_scenario=snapshot["fake_scenario"],
        graph=synthetic_graph,
        single_agent_role=payload.single_agent.role if payload.single_agent else None,
    )
    if snapshot["dependencies"]["model_groups"]:
        snapshot["required_features"] = sorted(
            {*snapshot.get("required_features", []), "model_groups"}
        )
    snapshot["policy_hash"] = policy_hash({**configuration, **snapshot["dependencies"]})
    snapshot["workspace"].update(
        {
            "workspace_path": normalized,
            "scope": scope,
            "git_head_sha": git.head_sha if git else None,
            "git_root_path": git.root_path if git else None,
        }
    )
    snapshot_payload = json.dumps(snapshot, sort_keys=True, separators=(",", ":"))
    snapshot_hash_value = content_hash(snapshot)
    now = utc_now()
    model = RunModel(
        id=new_id(),
        idempotency_key=payload.idempotency_key,
        request_hash=request_hash_value,
        request_json=to_json(request),
        project_id=project.id,
        chat_id=payload.chat_id,
        pipeline_version_id=version.id,
        binding_id=binding.id,
        state="queued",
        state_version=0,
        schema_version=version.schema_version,
        execution_hash=snapshot["execution_hash"],
        policy_hash=snapshot["policy_hash"],
        snapshot_json=snapshot_payload,
        resolved_settings_json=json.dumps(snapshot["resolved_settings"], sort_keys=True),
        current_node_id=None,
        current_execution_id=None,
        current_attempt_id=None,
        current_cycle_id=None,
        worker_id=None,
        worker_generation=0,
        waiting_reason_json=None,
        resume_target_json=None,
        stop_goal=None,
        created_at=now,
        updated_at=now,
        started_at=None,
        finished_at=None,
    )
    session.add(model)
    session.flush()
    if snapshot.get("plan"):
        from agents_ide.engine.plan_control import upsert_plan_items

        upsert_plan_items(session, model.id, snapshot["plan"]["items"])
    # A queued Run does not own its workspace. The worker reserves it at dispatch.
    session.add(
        QueueJobModel(
            id=new_id(),
            run_id=model.id,
            available_at=now,
            claimed_by=None,
            lease_expires_at=None,
            generation=1,
            created_at=now,
        )
    )
    session.add(
        RunEventModel(
            id=new_id(),
            run_id=model.id,
            sequence=_next_event_sequence(session, model.id),
            event_version=1,
            type="run.created",
            occurred_at=now,
            persisted_at=now,
            node_id=None,
            step_execution_id=None,
            step_attempt_id=None,
            agent_session_id=None,
            command_id=None,
            worker_generation=0,
            payload_json=to_json(
                {
                    "pipeline_version_id": version.id,
                    "snapshot_hash": snapshot_hash_value,
                    "execution_hash": model.execution_hash,
                    "source": "simulated" if payload.execution_mode == "simulated" else "engine",
                    "policy_hash": model.policy_hash,
                }
            ),
        )
    )
    session.flush()
    return _run_from_model(model)


def list_runs(
    session: Session,
    project_id: str | None = None,
    chat_id: str | None = None,
) -> list[Run]:
    stmt = select(RunModel).order_by(RunModel.created_at.desc())
    if project_id is not None:
        stmt = stmt.where(RunModel.project_id == project_id)
    if chat_id is not None:
        stmt = stmt.where(RunModel.chat_id == chat_id)
    return [_run_from_model(row) for row in session.scalars(stmt).all()]


def get_run(session: Session, run_id: str) -> Run:
    return _run_from_model(get_or_404(session, RunModel, run_id))


def submit_command(session: Session, run_id: str, payload: RunCommand) -> CommandAccepted:
    begin_write(session)
    run = get_or_404(session, RunModel, run_id)
    payload_hash_value = payload_hash(payload.model_dump(mode="json"))
    # First check duplicates: same command_id + same payload hash returns prior result.
    existing = session.execute(
        select(CommandJournalModel).where(
            CommandJournalModel.run_id == run_id,
            CommandJournalModel.command_id == payload.command_id,
        )
    ).scalar_one_or_none()
    if existing is not None:
        if existing.payload_hash != payload_hash_value:
            raise AppError(
                "command_id_conflict",
                "Тот же command_id использован с другим payload",
                409,
            )
        return CommandAccepted(
            command_id=existing.command_id,
            sequence=existing.sequence,
            status=existing.status,  # type: ignore[arg-type]
            response=json.loads(existing.response_json) if existing.response_json else None,
            applied_at=_dt(existing.applied_at),
        )
    if run.state_version != payload.expected_state_version:
        raise AppError(
            "version_conflict",
            "Версия Run не совпадает",
            409,
            {
                "expected_state_version": payload.expected_state_version,
                "actual_state_version": run.state_version,
            },
        )
    if not _command_allowed(payload.command_type, run.state):
        raise AppError(
            "command_not_allowed",
            f"Команда {payload.command_type} недоступна из состояния {run.state}",
            409,
            {"run_state": run.state},
        )
    from agents_ide.engine.artifacts import sanitize
    from agents_ide.services.run_controls import command_event, prepare_command

    previous = run.state
    status, response = prepare_command(session, run, payload)
    applied_at = utc_now() if status == "applied" else None
    now = utc_now()
    sequence = _next_command_sequence(session, run_id)
    journal = CommandJournalModel(
        id=new_id(),
        run_id=run_id,
        command_id=payload.command_id,
        command_type=payload.command_type,
        expected_state_version=payload.expected_state_version,
        payload_hash=payload_hash_value,
        payload_json=to_json(sanitize(payload.payload)),
        sequence=sequence,
        initiator="api",
        status=status,
        response_json=to_json(response) if response else None,
        created_at=now,
        applied_at=applied_at,
    )
    session.add(journal)
    run.state_version += 1
    run.updated_at = now
    command_event(session, run, payload, previous, status)
    session.flush()
    return CommandAccepted(
        command_id=payload.command_id,
        sequence=sequence,
        status=status,  # type: ignore[arg-type]
        response=response,
        applied_at=_dt(applied_at),
    )


def list_command_journal(session: Session, run_id: str) -> list[CommandAccepted]:
    if session.get(RunModel, run_id) is None:
        raise AppError("run_not_found", "Run не найден", 404)
    stmt = (
        select(CommandJournalModel)
        .where(CommandJournalModel.run_id == run_id)
        .order_by(CommandJournalModel.sequence.asc())
    )
    return [
        CommandAccepted(
            command_id=row.command_id,
            sequence=row.sequence,
            status=row.status,  # type: ignore[arg-type]
            response=json.loads(row.response_json) if row.response_json else None,
            applied_at=_dt(row.applied_at),
        )
        for row in session.scalars(stmt).all()
    ]


# ---------------------------------------------------------------------------- helpers


def _build_snapshot(project: ProjectModel, version: PipelineVersionModel) -> dict[str, Any]:
    from agents_ide import __version__

    return {
        "engine_version": __version__,
        "schema_version": version.schema_version,
        "execution_hash": version.execution_hash,
        "policy_hash": version.policy_hash,
        "graph": json.loads(version.graph_json),
        "required_features": json.loads(version.required_features_json),
        "workspace": {
            "project_id": project.id,
            "workspace_path": project.workspace_normalized_path,
            "identity_dev": project.workspace_identity_dev,
            "identity_ino": project.workspace_identity_ino,
            "git_root_path": project.git_root_path,
            "git_head_sha": project.git_head_sha,
        },
    }


def _next_event_sequence(session: Session, run_id: str) -> int:
    stmt = select(func.max(RunEventModel.sequence)).where(RunEventModel.run_id == run_id)
    current = session.execute(stmt).scalar()
    return int(current or 0) + 1


def _next_command_sequence(session: Session, run_id: str) -> int:
    stmt = select(func.max(CommandJournalModel.sequence)).where(
        CommandJournalModel.run_id == run_id
    )
    current = session.execute(stmt).scalar()
    return int(current or 0) + 1


def _command_allowed(command_type: str, state: str) -> bool:
    if state == "cancelled":
        return command_type == "cancel"
    if state in _RUN_STATE_TERMINAL:
        return False
    if state in {"queued", "running", "retry_wait"}:
        return command_type in {"pause", "stop", "cancel"}
    if state == "pause_requested":
        return command_type in {"pause", "stop", "cancel"}
    if state == "paused":
        return command_type in {"pause", "stop", "resume", "cancel", "resolve"}
    if state == "stop_requested":
        return command_type in {"stop", "cancel"}
    if state == "stopped":
        return command_type in {"stop", "resume", "cancel", "resolve"}
    if state == "waiting_input":
        return command_type in {"resolve", "resume", "pause", "stop", "cancel"}
    if state == "recovering":
        return command_type in {"stop", "cancel"}
    return False


def _run_from_model(model: RunModel) -> Run:
    return Run(
        simulated=json.loads(model.snapshot_json).get("execution_mode") == "simulated",
        runtime=json.loads(model.runtime_json),
        id=model.id,
        idempotency_key=model.idempotency_key,
        project_id=model.project_id,
        chat_id=model.chat_id,
        binding_id=model.binding_id,
        pipeline_version_id=model.pipeline_version_id,
        state=RunState(model.state),
        state_version=model.state_version,
        schema_version=model.schema_version,
        execution_hash=model.execution_hash,
        policy_hash=model.policy_hash,
        snapshot_hash=content_hash(json.loads(model.snapshot_json)),
        started_at=_dt(model.started_at),
        finished_at=_dt(model.finished_at),
        created_at=_dt(model.created_at) or _now(),
        updated_at=_dt(model.updated_at) or _now(),
        worker_id=model.worker_id,
        worker_generation=model.worker_generation,
        waiting_reason=json.loads(model.waiting_reason_json) if model.waiting_reason_json else None,
        active_intervals=json.loads(model.active_intervals_json),
    )


def _dt(value: float | None) -> Any:
    from datetime import UTC, datetime

    if value is None:
        return None
    return datetime.fromtimestamp(value, tz=UTC)


def _now() -> Any:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC)
