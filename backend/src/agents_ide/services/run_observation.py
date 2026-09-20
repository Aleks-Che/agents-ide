"""Read-only Run projection and bounded, backwards-paginated event history."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.engine.events import EventEnvelope
from agents_ide.engine.loops import loop_progress
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    Run,
    RunEvent,
    StepAttempt,
    StepExecution,
)

HistoryCategory = Literal["all", "steps", "messages", "tools", "models", "commands", "checks"]
CATEGORIES = {
    "steps": ("run.", "node.", "transition.", "attempt."),
    "messages": ("agent.message_delta", "attempt.text_delta", "agent.user_message", "agent.input_"),
    "tools": ("agent.tool_call", "agent.permission_"),
    "models": ("model_group.", "attempt.started", "attempt.finished", "attempt.retry_"),
    "commands": ("control.", "command.", "git."),
    "checks": ("condition.", "evidence.", "plan.", "artifact."),
}


class ObservedNode(BaseModel):
    id: str
    type: str
    label: str
    position: dict[str, float] | None = None
    execution_id: str | None = None
    status: str = "pending"
    visit_index: int = 0
    cycle_id: int | None = None
    attempt_count: int = 0
    started_at: float | None = None
    finished_at: float | None = None
    decision: str | None = None
    result_ref: str | None = None
    attempt_id: str | None = None
    model_id: str | None = None
    resource_id: str | None = None
    input_request: dict[str, Any] | None = None
    restart_blocked_reason: str | None = None


class ObservedEdge(BaseModel):
    id: str
    source: str
    target: str
    when: str | None = None
    loop_id: str | None = None


class ObservedLoop(BaseModel):
    id: str
    key: str
    scope: str
    completed: int
    max_iterations: int
    remaining: int
    total_completed: int
    edge_ids: list[str]
    can_adjust: bool = False
    waiting: bool = False


class RunObservation(BaseModel):
    nodes: list[ObservedNode] = Field(default_factory=list)
    edges: list[ObservedEdge] = Field(default_factory=list)
    current_node_id: str | None = None
    current_execution_id: str | None = None
    cycle_id: int | None = None
    last_transition: dict[str, Any] | None = None
    loops: list[ObservedLoop] = Field(default_factory=list)


class HistoryPage(BaseModel):
    events: list[EventEnvelope]
    next_before: int | None
    has_more: bool
    last_sequence: int
    min_retained_sequence: int


def build_observation(session: Session, run: Run) -> RunObservation:
    from agents_ide.services.run_loops import ADJUSTABLE_STATES
    from agents_ide.services.stage_restart import recoverable_restart_execution

    graph = effective_snapshot(run).get("graph", {})
    runtime = json.loads(run.runtime_json or "{}")
    waiting = json.loads(run.waiting_reason_json or "null") or {}
    latest = (
        select(StepExecution.node_id, func.max(StepExecution.visit_index).label("visit"))
        .where(
            StepExecution.run_id == run.id,
            StepExecution.id.not_in(runtime.get("invalidated_executions", [])),
        )
        .group_by(StepExecution.node_id)
        .subquery()
    )
    executions = {
        row.node_id: row
        for row in session.scalars(
            select(StepExecution)
            .join(
                latest,
                (StepExecution.node_id == latest.c.node_id)
                & (StepExecution.visit_index == latest.c.visit),
            )
            .where(StepExecution.run_id == run.id)
        )
    }
    executions = {
        node_id: execution
        for node_id, execution in executions.items()
        if execution.cycle_id >= runtime.get("loop_observation_cycles", {}).get(node_id, 0)
    }
    if recovered := recoverable_restart_execution(session, run, runtime):
        executions[recovered.node_id] = recovered
    latest_attempt = (
        select(StepAttempt.execution_id, func.max(StepAttempt.attempt_index).label("attempt"))
        .where(StepAttempt.execution_id.in_([row.id for row in executions.values()]))
        .group_by(StepAttempt.execution_id)
        .subquery()
    )
    attempts = (
        {
            row.execution_id: row
            for row in session.scalars(
                select(StepAttempt).join(
                    latest_attempt,
                    (StepAttempt.execution_id == latest_attempt.c.execution_id)
                    & (StepAttempt.attempt_index == latest_attempt.c.attempt),
                )
            )
        }
        if executions
        else {}
    )
    result = RunObservation(
        current_node_id=run.current_node_id,
        current_execution_id=run.current_execution_id,
        cycle_id=runtime.get("cycle_id", run.current_cycle_id),
        last_transition=runtime.get("last_transition"),
        loops=[
            ObservedLoop(
                **value,
                can_adjust=run.state in ADJUSTABLE_STATES,
                waiting=waiting.get("code") == "limit_exceeded"
                and waiting.get("details", {}).get("limit") == f"loop:{value['id']}",
            )
            for value in loop_progress(graph, runtime)
        ],
    )
    if (
        run.current_execution_id
        and run.current_node_id not in executions
        and runtime.get("next_node_id")
    ):
        result.current_node_id = runtime["next_node_id"]
        result.current_execution_id = None
    # Older Runs have no checkpoint; retained events can still supply the last edge.
    if result.last_transition is None:
        transition = session.scalar(
            select(RunEvent)
            .where(RunEvent.run_id == run.id, RunEvent.type == "transition.selected")
            .order_by(RunEvent.sequence.desc())
            .limit(1)
        )
        if transition:
            result.last_transition = json.loads(transition.payload_json)
    for node in graph.get("nodes", []):
        item = ObservedNode(
            id=node["id"],
            type=node["type"],
            label=node.get("label") or node["id"],
            position=node.get("position"),
        )
        execution = executions.get(item.id)
        from agents_ide.services.stage_restart import restart_blocked_reason

        item.restart_blocked_reason = restart_blocked_reason(
            session, run, runtime, item.type, execution
        )
        if execution:
            item.execution_id = execution.id
            for key in (
                "status",
                "visit_index",
                "cycle_id",
                "attempt_count",
                "started_at",
                "finished_at",
                "decision",
            ):
                setattr(item, key, getattr(execution, key))
            item.result_ref = execution.raw_result_ref
            attempt = attempts.get(execution.id)
            if attempt:
                selection = json.loads(attempt.selection_json)
                item.attempt_id = attempt.id
                item.model_id = selection.get("model_id")
                item.resource_id = selection.get("harness_profile_id") or selection.get(
                    "provider_connection_id"
                )
                if item.id == run.current_node_id and run.state == "running":
                    requests: dict[str, dict[str, Any]] = {}
                    for event in session.scalars(
                        select(RunEvent)
                        .where(
                            RunEvent.run_id == run.id,
                            RunEvent.step_attempt_id == attempt.id,
                            RunEvent.type.in_(["agent.input_requested", "agent.input_closed"]),
                        )
                        .order_by(RunEvent.sequence)
                    ):
                        payload = json.loads(event.payload_json)
                        if payload.get("artifact_id"):
                            artifact = session.get(ArtifactManifest, payload["artifact_id"])
                            if artifact and artifact.run_id == run.id:
                                payload = json.loads(artifact.body_json or "{}")
                        key = str(payload.get("question_id", ""))
                        if event.type == "agent.input_requested":
                            requests[key] = payload
                        else:
                            requests.pop(key, None)
                    item.input_request = next(reversed(requests.values()), None)
        result.nodes.append(item)
    for index, edge in enumerate(graph.get("edges", [])):
        result.edges.append(
            ObservedEdge(
                id=edge.get("id") or edge.get("edge_id") or f"edge_{index}",
                source=edge.get("from") or edge.get("source") or edge.get("from_node"),
                target=edge.get("to") or edge.get("target") or edge.get("to_node"),
                when=edge.get("when"),
                loop_id=(edge.get("loop") or {}).get("id"),
            )
        )
    return result


def read_history(
    session: Session,
    run_id: str,
    *,
    before: int | None,
    limit: int,
    category: HistoryCategory,
    node_id: str | None,
    execution_id: str | None,
) -> HistoryPage:
    from sqlalchemy import or_
    from sqlalchemy.orm import load_only

    from agents_ide.engine.events_stream import MAX_BUFFER_BYTES, _serialize_event

    session.connection().exec_driver_sql("BEGIN")
    if session.get(Run, run_id, options=[load_only(Run.id)]) is None:
        raise AppError("run_not_found", "Run не найден", 404)
    minimum, maximum = session.execute(
        select(
            select(func.min(RunEvent.sequence)).where(RunEvent.run_id == run_id).scalar_subquery(),
            select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id).scalar_subquery(),
        )
    ).one()
    query = select(RunEvent).where(RunEvent.run_id == run_id)
    if before is not None:
        query = query.where(RunEvent.sequence < before)
    if node_id:
        query = query.where(RunEvent.node_id == node_id)
    if execution_id:
        query = query.where(RunEvent.step_execution_id == execution_id)
    if category != "all":
        query = query.where(
            or_(
                *(
                    RunEvent.type.startswith(prefix, autoescape=True)
                    for prefix in CATEGORIES[category]
                )
            )
        )
    rows = list(session.scalars(query.order_by(RunEvent.sequence.desc()).limit(limit + 1)))
    events: list[EventEnvelope] = []
    size = 0
    for row in rows[:limit]:
        event = _serialize_event(row)
        encoded_size = len(json.dumps(event, ensure_ascii=False).encode("utf-8")) + 64
        if events and size + encoded_size > MAX_BUFFER_BYTES // 2:
            break
        size += encoded_size
        events.append(EventEnvelope.model_validate(event))
    return HistoryPage(
        events=events,
        next_before=events[-1].sequence if events else None,
        has_more=len(rows) > len(events),
        last_sequence=maximum or 0,
        min_retained_sequence=minimum or 0,
    )
