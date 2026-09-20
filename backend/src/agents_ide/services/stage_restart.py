"""Restart a visited stage without erasing its attempts or rolling back files."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json, utc_now
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    CommandJournal,
    QueueJob,
    Run,
    StepAttempt,
    StepExecution,
)

RESTART_STATES = {"queued", "running", "retry_wait", "paused", "stopped", "waiting_input"}
RESTART_TYPES = {"AgentTask", "LLMRequest", "Command", "CollectContext", "GitCommit"}
COMMIT_MESSAGE_ERRORS = {"commit_message_generation_failed", "commit_message_invalid"}


def restart_blocked_reason(
    session: Session,
    run: Run,
    runtime: dict[str, Any],
    node_type: str,
    execution: StepExecution | None,
) -> str | None:
    if run.state not in RESTART_STATES or run.stop_goal == "cancelled":
        return "Дождитесь остановки или завершения текущей команды"
    if node_type not in RESTART_TYPES:
        return "Этот служебный этап нельзя перезапустить отдельно"
    if not execution or execution.id in runtime.get("invalidated_executions", []):
        return "Этап ещё не выполнялся"
    if execution.cycle_id != runtime.get("cycle_id") or execution.scope != runtime.get(
        "work", {}
    ).get("scope"):
        return "Можно перезапустить этап текущего цикла и пункта плана"
    if node_type == "GitCommit":
        if (
            run.state not in {"waiting_input", "paused", "stopped"}
            or run.current_execution_id != execution.id
            or execution.status == "succeeded"
        ):
            return "Можно перезапустить только остановившийся Git-этап до создания коммита"
        if session.scalar(
            select(ArtifactManifest.id)
            .where(
                ArtifactManifest.step_execution_id == execution.id,
                ArtifactManifest.schema_type == "git_intent",
            )
            .limit(1)
        ):
            return "Создание коммита уже началось; сначала требуется сверка результата"
    return None


def prepare_restart(session: Session, run: Run, command: RunCommand) -> tuple[str, dict[str, Any]]:
    from agents_ide.services.run_controls import ensure_queue
    from agents_ide.worker.processes import stored_processes_stopped

    payload = command.payload
    if set(payload) != {"node_id", "execution_id"}:
        raise AppError("stage_restart_invalid", "Укажите этап и его попытку выполнения", 422)
    execution_id = payload["execution_id"]
    execution = session.get(StepExecution, execution_id) if isinstance(execution_id, str) else None
    if not execution or execution.run_id != run.id or execution.node_id != payload["node_id"]:
        raise AppError("stage_restart_invalid", "Выполнение этапа не найдено", 422)
    runtime = json.loads(run.runtime_json)
    nodes = {n["id"]: n for n in effective_snapshot(run)["graph"]["nodes"]}
    reason = restart_blocked_reason(
        session, run, runtime, nodes[execution.node_id]["type"], execution
    )
    latest = session.scalar(
        select(StepExecution.id)
        .where(StepExecution.run_id == run.id, StepExecution.node_id == execution.node_id)
        .order_by(StepExecution.visit_index.desc())
        .limit(1)
    )
    if reason or latest != execution.id:
        raise AppError("stage_restart_unavailable", reason or "Этап уже выполнялся повторно", 409)
    # A plan operation or commit has persistent effects, even if its outcome is unknown.
    later = session.scalars(
        select(StepExecution).where(
            StepExecution.run_id == run.id,
            StepExecution.started_at > execution.started_at,
        )
    )
    if any(nodes[row.node_id]["type"] in {"PlanControl", "GitCommit"} for row in later):
        raise AppError(
            "stage_restart_unavailable", "После этапа уже изменён план или создан коммит", 409
        )
    from agents_ide.services.stage_configuration import capture_stage_configuration

    configuration = capture_stage_configuration(session, run)
    if configuration is not None:
        runtime["pending_stage_configuration"] = {
            "command_id": command.command_id,
            "configuration": configuration,
        }
        run.runtime_json = to_json(runtime)
    job = session.scalar(select(QueueJob).where(QueueJob.run_id == run.id))
    can_apply = (job is None or job.claimed_by is None) and stored_processes_stopped(session, run)
    if nodes[execution.node_id]["type"] == "GitCommit" and not can_apply:
        # Do not defer this check: a still-active operation could save an intent
        # between accepting the restart and the worker applying it.
        raise AppError("stage_restart_unavailable", "Сначала дождитесь остановки операции Git", 409)
    if can_apply:
        from agents_ide.engine.events import append_event

        for pending in session.scalars(
            select(CommandJournal).where(
                CommandJournal.run_id == run.id,
                CommandJournal.status == "accepted",
                CommandJournal.command_type.in_(["pause", "stop", "restart_stage"]),
            )
        ):
            pending.status, pending.applied_at = "superseded", utc_now()
            append_event(
                session,
                run.id,
                "control.applied",
                {"command_type": pending.command_type, "status": "superseded"},
                command_id=pending.command_id,
                simulated=json.loads(run.snapshot_json).get("execution_mode") == "simulated",
            )
        apply_restart(session, run, payload, command.command_id)
        ensure_queue(session, run)
        return "applied", {"state": "queued", "node_id": execution.node_id}
    if run.state in {"paused", "stopped", "waiting_input"} or job is None or job.claimed_by is None:
        # A stale owner must be reconciled before another external call is possible.
        run.state = "recovering"
        ensure_queue(session, run)
    elif run.state != "queued":
        run.state = "stop_requested"
    run.stop_goal = "stopped"
    return "accepted", {"state": run.state, "node_id": execution.node_id}


def apply_restart(session: Session, run: Run, payload: dict[str, Any], command_id: str) -> None:
    """Called only after local writers have been proven stopped."""
    from agents_ide.services.run_controls import reset_stopped_agent, unsettled_attempts
    from agents_ide.services.run_messages import close_messages

    execution = session.get(StepExecution, payload["execution_id"])
    assert execution is not None
    runtime = json.loads(run.runtime_json)
    pending = runtime.pop("pending_stage_configuration", {})
    if pending.get("command_id") == command_id:
        configuration = pending["configuration"]
        runtime["template_configuration"] = configuration
        runtime.pop("group_dependencies", None)
        runtime.get("json_processing_overrides", {}).pop(execution.node_id, None)
    invalidated = set(runtime.get("invalidated_executions", []))
    affected = list(
        session.scalars(
            select(StepExecution).where(
                StepExecution.run_id == run.id,
                StepExecution.cycle_id == execution.cycle_id,
                StepExecution.scope == execution.scope,
                StepExecution.started_at >= execution.started_at,
            )
        )
    )
    # Do not fall back to older successful visits of nodes being redone.
    affected_nodes = {row.node_id for row in affected}
    invalidated.update(
        session.scalars(
            select(StepExecution.id).where(
                StepExecution.run_id == run.id,
                StepExecution.cycle_id == execution.cycle_id,
                StepExecution.scope == execution.scope,
                StepExecution.node_id.in_(affected_nodes),
            )
        )
    )
    authorized = set(runtime.get("retry_authorized_attempts", []))
    for attempt in unsettled_attempts(session, run):
        authorized.add(attempt.id)
        if attempt.status in {"prepared", "running", "waiting_input"}:
            attempt.status = "unknown"
            attempt.finished_at = utc_now()
    for row in affected:
        if row.status in {"running", "retry_wait", "waiting_input"}:
            row.status, row.finished_at = "interrupted", utc_now()
    close_messages(session, run)
    reset_stopped_agent(runtime)
    runtime["native_sessions"] = {}
    for key in (
        "waiting_code",
        "waiting_reason",
        "active_call_owner",
        "last_transition",
        "agent_continuation",
        "current_agent_session",
        "logical_evidence_hash",
        "json_reprocessing",
    ):
        runtime.pop(key, None)
    checkpoint = runtime.get("stage_work_checkpoints", {}).get(execution.id)
    if checkpoint:
        runtime["work"] = checkpoint
    runtime.update(
        next_node_id=execution.node_id,
        cycle_id=execution.cycle_id,
        invalidated_executions=sorted(invalidated),
        retry_authorized_attempts=sorted(authorized),
        candidate_history=[],
        server_retries=0,
        selection_round=0,
        active_since=None,
    )
    target = json.loads(run.resume_target_json or "{}")
    # Execution errors can be retried; workspace/configuration blockers still require repair.
    retryable = {
        "unknown_external_result",
        "invalid_response_format",
        "session_resume_unavailable",
        "model_unavailable",
        "model_group_exhausted",
        "permission_required",
        "process_not_responding",
        "owner_expired",
        "reconciliation_required",
    }
    # Message generation fails before git_intent is saved. It was historically
    # reported as configuration_invalid, including in already waiting runs.
    failed_attempt = (
        session.get(StepAttempt, run.current_attempt_id) if run.current_attempt_id else None
    )
    if (
        failed_attempt
        and failed_attempt.execution_id == execution.id
        and failed_attempt.status == "failed"
        and failed_attempt.retry_safety == "safe"
        and failed_attempt.error_code in COMMIT_MESSAGE_ERRORS
        and any(
            node["id"] == execution.node_id and node["type"] == "GitCommit"
            for node in effective_snapshot(run)["graph"]["nodes"]
        )
    ):
        retryable.add("configuration_invalid")
    run.resume_target_json = to_json(
        {
            "action": "dispatch_next",
            "node_id": execution.node_id,
            "blockers": [code for code in target.get("blockers", []) if code not in retryable],
        }
    )
    run.runtime_json = to_json(runtime)
    run.current_node_id = execution.node_id
    run.current_cycle_id = execution.cycle_id
    run.current_execution_id = run.current_attempt_id = None
    run.state, run.stop_goal, run.waiting_reason_json = "queued", None, None
