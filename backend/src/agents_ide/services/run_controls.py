"""Transactional command handling shared by REST and the diagnostic CLI."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine import artifacts
from agents_ide.engine.events import append_event
from agents_ide.engine.policy import effective_limits
from agents_ide.engine.run_configuration import effective_snapshot, harness_configuration_matches
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    AgentSession,
    ArtifactManifest,
    CommandJournal,
    HarnessProfile,
    ProviderConnection,
    QueueJob,
    Run,
    RunPolicyRevision,
    StepAttempt,
    StepExecution,
    WorkspaceReservation,
)


def ensure_queue(session: Session, run: Run) -> None:
    job = session.scalar(select(QueueJob).where(QueueJob.run_id == run.id))
    if job is not None:
        if job.claimed_by is not None:
            raise AppError("owner_active", "Сначала дождитесь сверки владельца", 409)
        job.available_at = utc_now()
        return
    session.add(
        QueueJob(
            id=new_id(),
            run_id=run.id,
            available_at=utc_now(),
            generation=run.worker_generation or 0,
            created_at=utc_now(),
        )
    )
    # The existing reservation is transferred by claim, never released by resume.


def unsettled_attempts(session: Session, run: Run) -> list[StepAttempt]:
    runtime = json.loads(run.runtime_json)
    authorized = runtime.get("retry_authorized_attempts", [])
    return [
        a
        for a in session.scalars(
            select(StepAttempt)
            .join(StepExecution)
            .where(
                StepExecution.run_id == run.id,
                StepAttempt.status.in_(["prepared", "running", "unknown", "waiting_input"]),
            )
        )
        if a.id not in authorized and a.id not in runtime.get("agent_recovery_attempts", {})
    ]


def reset_stopped_agent(runtime: dict[str, Any], session_id: str | None = None) -> None:
    """STOP retries the stage's original task in a fresh native session."""
    current = runtime.pop("current_agent_session", {})
    continuation = runtime.pop("agent_continuation", {})
    runtime.pop("agent_handoff", None)
    runtime.pop("pending_agent_recovery", None)
    for key in (current.get("session_key"), continuation.get("session_key")):
        runtime.get("native_sessions", {}).pop(key, None)
    # Runs stopped before session checkpoints were introduced still have AgentSession rows.
    if session_id:
        runtime["native_sessions"] = {
            key: value
            for key, value in runtime.get("native_sessions", {}).items()
            if value.get("session_id") != session_id
        }
    runtime.update(candidate_index=None, next_candidate_index=0, candidate_retries=0)
    runtime.pop("retry_at", None)


def _resolve(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    payload = command.payload
    if set(payload) - {
        "limit_overrides",
        "data",
        "reason",
        "retry",
        "reconciliation",
        "json_processing",
        "agent_recovery",
    }:
        raise AppError("resolution_invalid", "Неизвестные поля решения", 422)
    if "agent_recovery" in payload:
        from agents_ide.services.agent_recovery import prepare_agent_recovery

        return prepare_agent_recovery(session, run, command)
    if "json_processing" in payload:
        from agents_ide.services.json_resolution import prepare_json_reprocessing

        return prepare_json_reprocessing(session, run, command)
    runtime, snapshot = json.loads(run.runtime_json), effective_snapshot(run)
    reason = json.loads(run.waiting_reason_json or "{}")
    code = reason.get("code") or runtime.get("waiting_code")
    response: dict[str, Any] = {"applied": True}
    if "limit_overrides" in payload:
        overrides = payload["limit_overrides"]
        old_limits = effective_limits(snapshot, runtime)
        if (
            code != "limit_exceeded"
            or not isinstance(overrides, dict)
            or not overrides
            or any(
                key not in old_limits
                or type(value) is not int
                or value <= old_limits[key]
                or value > 2**53 - 1
                for key, value in overrides.items()
            )
        ):
            raise AppError(
                "limit_invalid",
                "Разрешено только увеличение известных целых лимитов при limit_exceeded",
                422,
            )
        for key, value in overrides.items():
            session.add(
                RunPolicyRevision(
                    id=new_id(),
                    run_id=run.id,
                    limit_name=key,
                    old_value_json=to_json(old_limits[key]),
                    new_value_json=to_json(value),
                    reason=str(artifacts.sanitize(payload.get("reason") or "resolve")),
                    author="api",
                    created_at=utc_now(),
                )
            )
        runtime["limit_overrides"] = {**runtime.get("limit_overrides", {}), **overrides}
        response["limit_overrides"] = runtime["limit_overrides"]
    if "reconciliation" in payload:
        resolution = payload["reconciliation"]
        if (
            code != "unknown_external_result"
            or not isinstance(resolution, dict)
            or set(resolution) != {"attempt_id", "action", "evidence"}
            or resolution["action"] not in {"retry_authorized", "accept_result"}
            or not isinstance(resolution["evidence"], str)
            or not resolution["evidence"].strip()
        ):
            raise AppError(
                "resolution_invalid",
                "Нужны attempt_id, action=retry_authorized|accept_result и evidence",
                422,
            )
        # A statement in a payload is never proof of process shutdown.
        from agents_ide.worker.processes import stored_processes_stopped

        if not stored_processes_stopped(session, run):
            raise AppError(
                "reconciliation_required",
                "Остановка прежнего владельца и процессов не подтверждена",
                409,
            )
        attempt = next(
            (a for a in unsettled_attempts(session, run) if a.id == resolution["attempt_id"]), None
        )
        if attempt is None or attempt.status != "unknown":
            raise AppError(
                "resolution_invalid", "Решение не соответствует неизвестной попытке", 409
            )
        if resolution["action"] == "accept_result":
            artifact_id = runtime.get("late_result_refs", {}).get(attempt.id)
            artifact = session.get(ArtifactManifest, artifact_id) if artifact_id else None
            body = json.loads(artifact.body_json or "{}") if artifact else {}
            if (
                artifact is None
                or artifact.run_id != run.id
                or artifact.step_attempt_id != attempt.id
                or not body.get("validated_by_server")
            ):
                raise AppError(
                    "resolution_invalid", "Нет подтверждённого валидного позднего результата", 409
                )
            attempt.status, attempt.external_outcome = "succeeded", "succeeded"
            attempt.result_artifact_id, attempt.error_code = artifact.id, None
        else:
            runtime.setdefault("retry_authorized_attempts", []).append(attempt.id)
        # Preserve the unknown outcome; explicit authority permits a new attempt,
        # and does not relabel past effects as success or no_effect.
    if "retry" in payload:
        if payload["retry"] is not True or code not in {
            "invalid_response_format",
            "permission_required",
        }:
            raise AppError("resolution_invalid", "Повтор недоступен для этой причины", 422)
        runtime["retry_resolution"] = code
        runtime.pop("json_reprocessing", None)
    if "data" in payload or "reconciliation" in payload:
        body = payload.get("data", payload.get("reconciliation"))
        if len(artifacts.encode(body).encode("utf-8")) > 1024 * 1024:
            raise AppError("resolution_invalid", "Решение превышает 1 MiB", 422)
        artifact = artifacts.record_artifact(
            session,
            run.id,
            artifacts.ArtifactPayload("resolution_data", body=body),
            source_kind="api",
            step_execution_id=run.current_execution_id,
            step_attempt_id=run.current_attempt_id,
            cycle_id=run.current_cycle_id,
        )
        artifact.source_ref = command.command_id
        runtime.setdefault("resolution_artifact_ids", []).append(artifact.id)
        response["artifact_id"] = artifact.id
    if len(response) == 1 and not any(k in payload for k in ("retry", "reconciliation")):
        raise AppError("resolution_invalid", "Решение не содержит применимых данных", 422)
    run.runtime_json = to_json(runtime)
    return response


def _git_check_retry(
    session: Session, run: Run, snapshot: dict[str, Any], waiting: dict[str, Any]
) -> str | None:
    """A failed Git guard can be checked again before any durable commit intent."""
    if waiting.get("code") != "external_change_detected" or not run.current_attempt_id:
        return None
    attempt = session.get(StepAttempt, run.current_attempt_id)
    execution = session.get(StepExecution, attempt.execution_id) if attempt else None
    if (
        not attempt
        or attempt.status != "unknown"
        or attempt.error_code
        not in {"external_change_detected", "git_index_dirty", "path_violation"}
        or not execution
        or execution.run_id != run.id
        or execution.id != run.current_execution_id
        or not any(
            node["id"] == execution.node_id and node["type"] == "GitCommit"
            for node in snapshot["graph"]["nodes"]
        )
    ):
        return None
    if session.scalar(
        select(ArtifactManifest.id).where(
            ArtifactManifest.step_execution_id == execution.id,
            ArtifactManifest.schema_type == "git_intent",
        )
    ):
        return None
    # execute() persists its intent before invoking git commit. Without it,
    # retrying only reruns the guards; a real conflict still blocks the commit.
    return attempt.id


def _check_resume(session: Session, run: Run) -> None:
    from agents_ide.services.live_groups import refresh_groups
    from agents_ide.worker.processes import stored_processes_stopped

    runtime, snapshot = json.loads(run.runtime_json), effective_snapshot(run)
    snapshot["dependencies"] = refresh_groups(session, snapshot)
    target = json.loads(run.resume_target_json or "{}")
    waiting = json.loads(run.waiting_reason_json or "{}") or runtime.get("waiting_reason") or {}
    if not stored_processes_stopped(session, run):
        raise AppError("reconciliation_required", "Прежняя операция требует сверки", 409)
    git_retry = _git_check_retry(session, run, snapshot, waiting)
    if any(attempt.id != git_retry for attempt in unsettled_attempts(session, run)):
        raise AppError("reconciliation_required", "Прежняя операция требует сверки", 409)
    if git_retry and git_retry not in runtime.get("retry_authorized_attempts", []):
        runtime.setdefault("retry_authorized_attempts", []).append(git_retry)
    if run.state == "stopped":
        execution = (
            session.get(StepExecution, run.current_execution_id)
            if run.current_execution_id
            else None
        )
        if execution and execution.status != "succeeded":
            session_id = session.scalar(
                select(AgentSession.external_session_id).where(
                    AgentSession.attempt_id == run.current_attempt_id
                )
            )
            reset_stopped_agent(runtime, session_id)
    blockers = target.get("blockers", [])
    for code in blockers:
        if code == "limit_exceeded":
            from agents_ide.engine.loops import loop_key, loop_limit

            loop = target.get("blocked_edge", {}).get("loop")
            if loop and waiting.get("details", {}).get("limit") == f"loop:{loop['id']}":
                if runtime.get("loop_counts", {}).get(loop_key(loop, runtime), 0) >= loop_limit(
                    loop, runtime
                ):
                    raise AppError(
                        "limit_exceeded",
                        "Добавьте оставшиеся итерации цикла перед продолжением",
                        409,
                    )
                continue
            from agents_ide.operations.storage import settings_for

            if not settings_for(session).enforce_execution_limits:
                continue
            limits = effective_limits(snapshot, runtime)
            reason = (
                json.loads(run.waiting_reason_json or "{}") or runtime.get("waiting_reason") or {}
            )
            key = reason.get("details", {}).get("limit")
            if key in {
                "run_artifact_bytes",
                "data_budget_bytes",
                "disk_free_bytes",
                "detailed_events_limit",
            }:
                from agents_ide.operations.storage import check_capacity

                check_capacity(session, run.id)
                continue
            used = {
                "max_calls": runtime.get("external_calls", 0),
                "max_node_visits": runtime.get("visits", 0),
                "max_backward_transitions": runtime.get("backward_transitions", 0),
                "max_duration_seconds": runtime.get("duration_seconds", 0),
            }
            if key not in limits or used[key] >= limits[key]:
                raise AppError("limit_exceeded", "Лимит не увеличен выше сохранённого расхода", 409)
        elif code in {"model_group_exhausted", "model_unavailable"}:
            config = (
                snapshot.get("dependencies", {}).get("nodes", {}).get(target.get("node_id"), {})
            )
            candidates = config.get("candidates", [])
            available = False
            for candidate in candidates:
                model = (
                    HarnessProfile if candidate.get("harness_profile_id") else ProviderConnection
                )
                ref = candidate.get("harness_profile_id") or candidate.get("provider_connection_id")
                resource = (
                    session.get(HarnessProfile, ref)
                    if model is HarnessProfile
                    else session.get(ProviderConnection, ref)
                )
                if (
                    candidate.get("enabled", True)
                    and not candidate.get("unavailable_reason")
                    and resource
                    and resource.archived_at is None
                    and (
                        not candidate.get("resource_version")
                        or resource.version == candidate["resource_version"]
                        or (
                            isinstance(resource, HarnessProfile)
                            and harness_configuration_matches(
                                resource,
                                snapshot["dependencies"]["harness_profiles"].get(ref, {}),
                            )
                        )
                    )
                ):
                    available = True
            if not available:
                raise AppError("model_unavailable", "Нет доступного кандидата из snapshot", 409)
            runtime.update(next_candidate_index=0, candidate_index=None, candidate_retries=0)
            runtime["selection_round"] = runtime.get("selection_round", 0) + 1
        elif code in {"invalid_response_format", "permission_required"}:
            if code == "invalid_response_format" and (
                runtime.get("json_reprocessing", {}).get("attempt_id") == run.current_attempt_id
                and run.current_attempt_id is not None
            ):
                continue
            if runtime.get("retry_resolution") != code:
                raise AppError(
                    "resolution_required", "Сначала сохраните явное решение о повторе", 409
                )
            runtime.pop("retry_resolution", None)
            if run.current_attempt_id:
                runtime.setdefault("retry_authorized_attempts", []).append(run.current_attempt_id)
        elif (code, waiting.get("details", {}).get("reason")) in {
            ("configuration_invalid", "git_verification_required"),
            ("missing_data", "git_verification_not_passed"),
        } and any(
            node["id"] == target.get("node_id") and node["type"] == "GitCommit"
            for node in snapshot["graph"]["nodes"]
        ):
            # Old runs may still carry the removed verification gate. Git safety
            # checks run again in the worker before the commit can be attempted.
            continue
        elif (
            code == "configuration_invalid"
            and json.loads(run.waiting_reason_json or "{}").get("details", {}).get("reason")
            == "runtime_expression_invalid"
        ):
            _validate_current_prompt(session, run, snapshot, runtime, target)
        elif code == "session_resume_unavailable":
            # Retry only the saved session; STOP explicitly starts this stage afresh.
            continue
        elif code == "external_change_detected" and git_retry:
            # The worker rechecks HEAD, index, config and protected files before dispatch.
            continue
        elif code in {"unknown_external_result", "owner_expired", "reconciliation_required"}:
            if not runtime.get("work"):
                raise AppError(
                    "checkpoint_missing", "Checkpoint утрачен: создайте новый Run после сверки", 409
                )
        else:
            raise AppError(
                "resolution_required", "Причина ожидания не устранена", 409, {"blocker": code}
            )
    target["blockers"] = []
    runtime.pop("pending_agent_recovery", None)
    if target.get("action") == "reconcile":
        target["action"] = "retry_attempt" if target.get("execution_id") else "dispatch_next"
    run.resume_target_json, run.runtime_json = to_json(target), to_json(runtime)


def _validate_current_prompt(
    session: Session,
    run: Run,
    snapshot: dict[str, Any],
    runtime: dict[str, Any],
    target: dict[str, Any],
) -> None:
    from agents_ide.domain.graph_ast import ASTError, EvaluationContext, Value, substitute
    from agents_ide.engine.visits import load_latest_results

    nodes = {node["id"]: node for node in snapshot["graph"]["nodes"]}
    node_id = target.get("node_id")
    if nodes.get(node_id, {}).get("type") not in {"AgentTask", "LLMRequest"}:
        raise AppError(
            "resolution_required", "Исправьте выражение в шаблоне и создайте новый запуск", 409
        )
    config = snapshot["dependencies"]["nodes"][node_id]
    work = runtime["work"]
    key = {"repair": "prompt_repair", "next_item": "prompt_next_item"}.get(work["mode"], "prompt")
    context = EvaluationContext(
        inputs={key: Value.of(value) for key, value in snapshot["input"]["values"].items()},
        latest=load_latest_results(
            session, run.id, runtime["cycle_id"], nodes, scope=work["scope"]
        ),
        work={key: Value.of(value) for key, value in work.items()},
        known_node_ids=frozenset(nodes),
        project={
            "id": Value.of(run.project_id),
            "path": Value.of(runtime.get("workspace", snapshot["workspace"])["workspace_path"]),
        },
        run={"id": Value.of(run.id), "cycle_id": Value.of(runtime["cycle_id"])},
        cycle_id=runtime["cycle_id"],
        scope=work["scope"],
    )
    try:
        substitute(config.get(key) or config.get("prompt", ""), context, strict=True)
    except ASTError as exc:
        raise AppError(
            "resolution_required", f"Исправьте выражение в шаблоне: {exc}", 409
        ) from None


def prepare_command(
    session: Session, run: Run, command: RunCommand
) -> tuple[str, dict[str, Any] | None]:
    previous = run.state
    kind = command.command_type
    if kind == "adjust_loop":
        from agents_ide.services.run_loops import adjust_loop

        return "applied", adjust_loop(session, run, command)
    if kind == "restart_stage":
        from agents_ide.services.stage_restart import prepare_restart

        return prepare_restart(session, run, command)
    if kind == "message":
        from agents_ide.services.run_messages import prepare_message

        return "accepted", prepare_message(session, run, command)
    if kind == "resolve":
        target = json.loads(run.resume_target_json or "{}")
        if run.state != "waiting_input" and not (
            run.state in {"paused", "stopped"} and target.get("blockers")
        ):
            raise AppError("command_not_allowed", "Resolve доступен после окончания сверки", 409)
        return "applied", _resolve(session, run, command)
    if kind == "resume":
        _check_resume(session, run)
        ensure_queue(session, run)
        run.state, run.waiting_reason_json = "queued", None
        run.stop_goal = None
        return "applied", {
            "from": previous,
            "action": json.loads(run.resume_target_json or "{}").get("action", "dispatch_next"),
        }
    if run.state == "cancelled":
        return "applied", {"state": "cancelled"}
    if run.state in {"paused", "stopped", "waiting_input"}:
        from agents_ide.worker.processes import stored_processes_stopped

        if not stored_processes_stopped(session, run) or (
            kind != "cancel" and unsettled_attempts(session, run)
        ):
            # Keep all blockers and the reservation until worker reconciliation.
            run.stop_goal = (
                "cancelled" if kind == "cancel" else "stopped" if kind == "stop" else run.stop_goal
            )
            if kind in {"stop", "cancel"}:
                run.state = "recovering"
                ensure_queue(session, run)
            return "accepted", None
        run.state = {"pause": "paused", "stop": "stopped", "cancel": "cancelled"}[kind]
        if kind == "stop":
            runtime = json.loads(run.runtime_json)
            reset_stopped_agent(runtime)
            run.runtime_json = to_json(runtime)
            target = json.loads(run.resume_target_json or "{}")
            target["blockers"] = [
                code for code in target.get("blockers", []) if code != "session_resume_unavailable"
            ]
            run.resume_target_json = to_json(target)
        if run.state == "cancelled":
            run.finished_at = utc_now()
            for reservation in session.scalars(
                select(WorkspaceReservation).where(
                    WorkspaceReservation.run_id == run.id,
                    WorkspaceReservation.released_at.is_(None),
                )
            ):
                reservation.released_at = utc_now()
        return "applied", {"state": run.state}
    if run.state in {"running", "pause_requested", "stop_requested"}:
        if kind != "pause":
            run.state = "stop_requested"
            if kind == "cancel" or run.stop_goal != "cancelled":
                run.stop_goal = "cancelled" if kind == "cancel" else "stopped"
        elif run.state == "running":
            run.state = "pause_requested"
    return "accepted", None


def command_event(
    session: Session, run: Run, command: RunCommand, previous: str, status: str
) -> None:
    simulated = json.loads(run.snapshot_json).get("execution_mode") == "simulated"
    append_event(
        session,
        run.id,
        "control.applied" if status == "applied" else "control.requested",
        {
            "command_type": command.command_type,
            "status": status,
            "state_version": run.state_version,
        },
        command_id=command.command_id,
        simulated=simulated,
    )
    if previous != run.state:
        if previous in {"running", "retry_wait", "pause_requested", "stop_requested"}:
            from datetime import UTC, datetime

            from agents_ide.domain.active_intervals import close_interval

            intervals = json.loads(run.active_intervals_json)
            now, quality = close_interval(intervals, datetime.now(UTC))
            if run.state in {"running", "retry_wait", "pause_requested", "stop_requested"}:
                intervals.append(
                    {"state": run.state, "started_at": now, "ended_at": None, "quality": quality}
                )
            run.active_intervals_json = to_json(intervals)
        append_event(
            session,
            run.id,
            "run.state_changed",
            {"from": previous, "to": run.state, "state_version": run.state_version},
            command_id=command.command_id,
            simulated=simulated,
        )


def cleanup_reservation(
    session: Session, run_id: str, command_id: str, expected_version: int
) -> dict[str, Any]:
    from agents_ide.services.transactions import begin_write
    from agents_ide.worker.processes import stored_processes_stopped

    begin_write(session)
    run = session.get(Run, run_id)
    if run is None:
        raise AppError("run_not_found", "Run не найден", 404)
    existing = session.scalar(
        select(CommandJournal).where(
            CommandJournal.run_id == run_id, CommandJournal.command_id == command_id
        )
    )
    if existing:
        if (
            existing.command_type != "cleanup"
            or existing.expected_state_version != expected_version
        ):
            raise AppError("command_id_conflict", "command_id уже использован", 409)
        result: dict[str, Any] = json.loads(existing.response_json or "{}")
        return result
    if run.state_version != expected_version:
        raise AppError("version_conflict", "Версия Run изменилась", 409)
    job = session.scalar(select(QueueJob).where(QueueJob.run_id == run_id))
    if (
        run.state not in {"paused", "stopped", "waiting_input", "completed", "failed", "cancelled"}
        or job is not None
        or not stored_processes_stopped(session, run)
        or any(a.finished_at is None for a in unsettled_attempts(session, run))
    ):
        raise AppError(
            "reservation_busy", "Владелец или дерево процессов не подтверждены остановленными", 409
        )
    released = []
    for reservation in session.scalars(
        select(WorkspaceReservation).where(
            WorkspaceReservation.run_id == run_id, WorkspaceReservation.released_at.is_(None)
        )
    ):
        reservation.released_at = utc_now()
        released.append(reservation.id)
    result = {"released_reservation_ids": released, "state": run.state}
    from sqlalchemy import func

    sequence = (
        session.scalar(
            select(func.max(CommandJournal.sequence)).where(CommandJournal.run_id == run_id)
        )
        or 0
    ) + 1
    session.add(
        CommandJournal(
            id=new_id(),
            run_id=run_id,
            command_id=command_id,
            command_type="cleanup",
            expected_state_version=expected_version,
            payload_hash="cleanup",
            payload_json="{}",
            sequence=sequence,
            initiator="api",
            status="applied",
            response_json=to_json(result),
            created_at=utc_now(),
            applied_at=utc_now(),
        )
    )
    run.state_version += 1
    append_event(
        session,
        run_id,
        "workspace.reservation_released",
        result,
        command_id=command_id,
        simulated=json.loads(run.snapshot_json).get("execution_mode") == "simulated",
    )
    return result
