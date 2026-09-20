"""Audited continuation of a stopped agent attempt without restarting its stage."""

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session, object_session

from agents_ide.adapters.history import tool_summaries
from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import RunCommand
from agents_ide.engine import artifacts
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.persistence.models import AgentSession, ArtifactManifest, Run, RunEvent, StepAttempt

RECOVERABLE_REASONS = {
    "unknown_external_result",
    "model_unavailable",
    "model_group_exhausted",
    "session_resume_unavailable",
    "process_not_responding",
}


def recovery_options(run: Run) -> dict[str, Any]:
    if run.state not in {"waiting_input", "paused"} or not run.current_attempt_id:
        return {}
    runtime = json.loads(run.runtime_json)
    waiting = json.loads(run.waiting_reason_json or "null") or runtime.get("waiting_reason") or {}
    if waiting.get("code") not in RECOVERABLE_REASONS:
        return {}
    snapshot = effective_snapshot(run)
    from agents_ide.services.live_groups import candidate_identity, refresh_groups

    session = object_session(run)
    if session:
        snapshot["dependencies"] = refresh_groups(session, snapshot, run.current_node_id)
    node: dict[str, Any] = next(
        (n for n in snapshot["graph"]["nodes"] if n["id"] == run.current_node_id), {}
    )
    if node.get("type") != "AgentTask":
        return {}
    current = runtime.get("current_agent_session", {})
    native = runtime.get("native_sessions", {}).get(current.get("session_key"), {})
    config = snapshot["dependencies"]["nodes"].get(run.current_node_id, {})
    attempt = session.get(StepAttempt, run.current_attempt_id) if session else None
    selection = json.loads(attempt.selection_json) if attempt else {}
    candidates = config.get("candidates", [])
    match = next(
        (c for c in candidates if candidate_identity(c) == candidate_identity(selection)), None
    )
    index = match["member_index"] if match else None
    actions = []
    if (
        current.get("execution_id") == run.current_execution_id
        and native.get("session_id")
        and (not config.get("model_group_id") or (match and match.get("enabled", True)))
    ):
        actions.append("continue_session")
    following = (
        [
            c
            for c in candidates
            if (index is None or c["member_index"] > index)
            and c.get("enabled", True)
            and not c.get("unavailable_reason")
        ]
        if config.get("model_group_id")
        else []
    )
    if following:
        actions.append("next_candidate")
    return (
        {
            "attempt_id": run.current_attempt_id,
            "actions": actions,
            "next_model": following[0]["model_id"] if following else None,
            "current_member_index": index,
            "next_member_index": following[0]["member_index"] if following else None,
        }
        if actions
        else {}
    )


def _body(session: Session, artifact_id: str | None, run_id: str) -> dict[str, Any]:
    row = session.get(ArtifactManifest, artifact_id) if artifact_id else None
    if row is None or row.run_id != run_id:
        return {}
    body = json.loads(row.body_json or "{}")
    return body if isinstance(body, dict) else {}


def _handoff_context(session: Session, run: Run, attempt: StepAttempt) -> dict[str, Any]:
    runtime = json.loads(run.runtime_json)
    prior = runtime.get("agent_handoff") or runtime.get("agent_continuation", {}).get(
        "handoff_context", {}
    )
    if prior.get("execution_id") != attempt.execution_id:
        prior = {}
    tools: dict[str, dict[str, Any]] = {}
    text = ""
    # A bounded tail is enough for cross-harness handoff; same-harness recovery
    # additionally resumes the native session with its complete conversation.
    rows = list(
        session.scalars(
            select(RunEvent)
            .where(
                RunEvent.run_id == run.id,
                RunEvent.step_attempt_id == attempt.id,
                RunEvent.type.in_(["agent.tool_call", "attempt.text_delta"]),
            )
            .order_by(RunEvent.sequence.desc())
            .limit(300)
        )
    )
    for row in reversed(rows):
        payload = json.loads(row.payload_json)
        if payload.get("artifact_id"):
            payload = _body(session, payload["artifact_id"], run.id)
        if row.type == "attempt.text_delta":
            text = (text + str(payload.get("text", "")))[-8000:]
        else:
            item = payload.get("item", payload)
            key = str(item.get("call_id") or item.get("id") or row.id)
            tools[key] = {
                "tool": item.get("tool") or item.get("type"),
                "status": item.get("status") or payload.get("phase"),
                "summary": str(item.get("summary") or item.get("command") or "")[:1000],
            }
    result = _body(session, attempt.result_artifact_id, run.id)
    request = _body(session, attempt.request_artifact_id, run.id)
    return {
        "execution_id": attempt.execution_id,
        "attempt_id": attempt.id,
        "last_output": str(result.get("raw_text") or text or prior.get("last_output", ""))[-8000:],
        "tool_calls": tool_summaries(
            prior.get("tool_calls", [])
            + list(tools.values())
            + json.loads(attempt.error_details_json or "{}").get("tool_summary", [])
        ),
        "source_request_artifact_id": attempt.request_artifact_id,
        "source_result_artifact_id": attempt.result_artifact_id,
        "original_prompt": prior.get("original_prompt") or request.get("prompt", ""),
        "workspace_changes_preserved": True,
    }


def prepare_agent_recovery(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    from agents_ide.services.run_controls import unsettled_attempts
    from agents_ide.worker.processes import stored_processes_stopped

    value = command.payload["agent_recovery"]
    options = recovery_options(run)
    if (
        set(command.payload) != {"agent_recovery"}
        or not isinstance(value, dict)
        or set(value) != {"attempt_id", "action"}
        or value.get("attempt_id") != options.get("attempt_id")
        or value.get("action") not in options.get("actions", [])
    ):
        raise AppError("resolution_invalid", "Продолжение недоступно для этой попытки", 409)
    attempt = session.get(StepAttempt, run.current_attempt_id)
    if (
        attempt is None
        or attempt.execution_id != run.current_execution_id
        or attempt.status not in {"unknown", "failed", "interrupted"}
        or attempt.finished_at is None
        or any(a.id != attempt.id for a in unsettled_attempts(session, run))
    ):
        raise AppError("reconciliation_required", "Предыдущая попытка ещё не завершена", 409)
    if not stored_processes_stopped(session, run):
        raise AppError("reconciliation_required", "Дождитесь остановки прежнего исполнителя", 409)
    native_row = session.scalar(select(AgentSession).where(AgentSession.attempt_id == attempt.id))
    runtime = json.loads(run.runtime_json)
    from agents_ide.services.live_groups import refresh_groups, save_group_dependencies

    save_group_dependencies(
        runtime, refresh_groups(session, effective_snapshot(run), run.current_node_id)
    )
    selection = json.loads(attempt.selection_json)
    index = selection.get("member_index")
    if not isinstance(index, int):
        raise AppError("resolution_invalid", "Не сохранена выбранная модель попытки", 409)
    current = runtime.get("current_agent_session", {})
    native = runtime.get("native_sessions", {}).get(current.get("session_key"), {})
    saved_session = {
        **current,
        "session_id": native.get("session_id"),
        "server_version": native.get("server_version"),
        "member_index": options["current_member_index"]
        if options.get("current_member_index") is not None
        else index,
        "selection": selection,
    }
    context = _handoff_context(session, run, attempt)
    context.update(model_id=selection.get("model_id"), reason="user_requested_handoff")
    if value["action"] == "continue_session":
        if native_row is None or native_row.external_session_id != native.get("session_id"):
            raise AppError("resolution_invalid", "Сохранённая сессия не соответствует попытке", 409)
        runtime["agent_continuation"] = {**saved_session, "handoff_context": context}
        runtime["next_candidate_index"] = saved_session["member_index"]
        runtime.pop("agent_handoff", None)
    else:
        runtime.pop("agent_continuation", None)
        runtime["next_candidate_index"] = options["next_member_index"]
        if (
            native_row is not None
            and native_row.harness_kind == "opencode"
            and native_row.external_session_id == native.get("session_id")
        ):
            context["native_session"] = {
                **saved_session,
                "harness_profile_id": selection.get("harness_profile_id"),
            }
        runtime["agent_handoff"] = context
        runtime.setdefault("candidate_history", []).append(
            {
                **selection,
                "reason": "user_requested_handoff",
            }
        )
    # Preserve the old attempt and its unknown effects. This authorization is
    # exclusively for continuation, separate from permission to replay a task.
    runtime.setdefault("agent_recovery_attempts", {})[attempt.id] = value["action"]
    runtime["candidate_index"] = (
        saved_session["member_index"]
        if value["action"] == "continue_session"
        else options.get("current_member_index")
    )
    # Readiness lasts until resume consumes this decision. The audit above must
    # remain for reconciliation, including failures before a new attempt exists.
    runtime["pending_agent_recovery"] = {**value, "execution_id": attempt.execution_id}
    runtime["candidate_retries"] = 0
    runtime["retry_at"] = None
    runtime["logical_evidence_hash"] = None
    runtime.pop("retry_resolution", None)
    artifact = artifacts.record_artifact(
        session,
        run.id,
        artifacts.ArtifactPayload(
            "resolution_data",
            body={
                "agent_recovery": value,
                "context": context,
            },
        ),
        source_kind="api",
        step_execution_id=attempt.execution_id,
        step_attempt_id=attempt.id,
        cycle_id=run.current_cycle_id,
    )
    artifact.source_ref = command.command_id
    runtime.setdefault("resolution_artifact_ids", []).append(artifact.id)
    waiting = json.loads(run.waiting_reason_json or "null") or runtime.get("waiting_reason") or {}
    waiting["allowed_actions"] = list(
        dict.fromkeys([*waiting.get("allowed_actions", []), "resume"])
    )
    run.waiting_reason_json = to_json(waiting)
    runtime["waiting_reason"] = waiting
    run.runtime_json = to_json(runtime)
    target = json.loads(run.resume_target_json or "{}")
    target.update(
        action="retry_attempt",
        node_id=run.current_node_id,
        execution_id=attempt.execution_id,
        blockers=[
            b
            for b in target.get("blockers", [])
            if b not in RECOVERABLE_REASONS | {"owner_expired", "reconciliation_required"}
        ],
    )
    run.resume_target_json = to_json(target)
    return {
        "applied": True,
        "continuation_pending": True,
        "artifact_id": artifact.id,
        "action": value["action"],
    }
