"""Engine-side catalog of normalized events.

Every event the runner emits is described by an entry below. The catalog
exists so that client UIs and tests can reason about the event surface
without hard-coding ad-hoc string constants. All payloads are bounded to
:data:`MAX_PAYLOAD_BYTES` so a malformed adapter can never blow up the
queue.

The transition reason semantics follow the architecture document:

* ``run.created`` is emitted exactly once per Run, inside the atomic start
  transaction.
* ``model_group.candidate_selected`` is followed by ``attempt.*`` events for
  the chosen candidate; if the attempt fails with retryable outcome, the
  same candidate is retried (no further ``candidate_selected`` is emitted).
* ``model_group.candidate_switched`` is emitted once per transition to the
  next candidate and carries the reason for the previous failure.
* ``model_group.exhausted`` ends the visit and forces a
  ``waiting_input(model_group_exhausted)``.
"""

from __future__ import annotations

from typing import Any, Final

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, utc_now
from agents_ide.persistence.models import RunEvent

MAX_PAYLOAD_BYTES: Final = 16 * 1024

# Order here mirrors the typical lifecycle for documentation; the runner does
# not rely on order.
EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {
        "run.created",
        "run.started",
        "run.paused",
        "run.stopped",
        "run.completed",
        "run.failed",
        "run.cancelled",
        "run.waiting",
        "run.resumed",
        "run.reconciling",
        "node.entered",
        "node.left",
        "transition.selected",
        "assignment.applied",
        "attempt.prepared",
        "attempt.started",
        "attempt.progress",
        "attempt.finished",
        "model_group.candidate_selected",
        "model_group.candidate_skipped",
        "model_group.candidate_switched",
        "model_group.exhausted",
        "artifact.recorded",
        "command.finished",
        "evidence.collected",
        "error.reported",
        "limits.warning",
        "run.state_changed",
        "run.waiting_input",
        "control.applied",
        "control.requested",
        "workspace.reservation_released",
        "condition.evaluated",
        "budget.updated",
        "budget.exceeded",
        "error.technical",
        "attempt.retry_scheduled",
        "attempt.text_delta",
        "agent.input_requested",
        "agent.input_closed",
        "agent.user_message",
        "agent.user_message_status",
        "attempt.late_result",
        "process.supervised",
        "process.interrupted",
        "git.no_changes",
        "git.tree_built",
        "git.commit_created",
        "git.commit_intent_saved",
        "git.commit_recovered",
        "git.baseline_saved",
        "git.branch_selected",
        "workspace.worktree_planned",
        "workspace.worktree_created",
        "plan.item_changed",
        "plan.final_check",
        "plan.commit_attached",
        "agent.server_started",
        "agent.session_created",
        "agent.session_resumed",
        "agent.session_aborted",
        "agent.message_delta",
        "agent.tool_call",
        "agent.permission_requested",
        "agent.permission_resolved",
        "agent.session_invalidated",
        "agent.native_event",
        "agent.turn_started",
        "agent.output_delta",
        "agent.plan_updated",
    }
)

RETRIABLE_OUTCOMES: Final[frozenset[str]] = frozenset({"retryable_failure"})

CONFIRMED_FAILURE_OUTCOMES: Final[frozenset[str]] = frozenset({"unavailable"})


class EventEnvelope(BaseModel):
    event_version: int = 1
    run_id: str
    sequence: int
    type: str = Field(json_schema_extra={"enum": [str(value) for value in sorted(EVENT_TYPES)]})
    occurred_at: float
    persisted_at: float
    node_id: str | None = None
    step_execution_id: str | None = None
    step_attempt_id: str | None = None
    agent_session_id: str | None = None
    command_id: str | None = None
    worker_generation: int
    payload: dict[str, Any]


def append_event(
    session: Session,
    run_id: str,
    type_: str,
    payload: dict[str, Any],
    *,
    generation: int = 0,
    simulated: bool = False,
    node_id: str | None = None,
    execution_id: str | None = None,
    attempt_id: str | None = None,
    command_id: str | None = None,
) -> RunEvent:
    """Caller holds BEGIN IMMEDIATE; state, artifacts and sequence commit together."""
    from agents_ide.engine.artifacts import ArtifactPayload, encode, record_artifact, sanitize

    if type_ not in EVENT_TYPES:
        raise ValueError(f"Uncatalogued event: {type_}")
    from agents_ide.errors import AppError
    from agents_ide.operations.storage import DETAIL_TYPES, settings_for
    from agents_ide.persistence.models import Run

    if type_ in DETAIL_TYPES:
        run = session.get(Run, run_id)
        if run is not None:
            if settings_for(session).enforce_execution_limits and (
                (run.detailed_event_count or 0) >= settings_for(session).detailed_events_limit
            ):
                raise AppError(
                    "limit_exceeded",
                    "Лимит подробных событий Run",
                    409,
                    {"limit": "detailed_events_limit"},
                )
            run.detailed_event_count = (run.detailed_event_count or 0) + 1
    cleaned: dict[str, Any] = sanitize(
        {**payload, "source": "simulated" if simulated else "engine"}
    )
    if len(encode(cleaned).encode("utf-8")) > MAX_PAYLOAD_BYTES:
        artifact = record_artifact(
            session,
            run_id,
            ArtifactPayload("event_payload", body=cleaned),
            step_execution_id=execution_id,
            step_attempt_id=attempt_id,
            source_kind="simulated" if simulated else "engine",
        )
        cleaned = {"artifact_id": artifact.id, "source": "simulated" if simulated else "engine"}
    session.flush()
    sequence = (
        int(
            session.scalar(select(func.max(RunEvent.sequence)).where(RunEvent.run_id == run_id))
            or 0
        )
        + 1
    )
    now = utc_now()
    event = RunEvent(
        id=new_id(),
        run_id=run_id,
        sequence=sequence,
        event_version=1,
        type=type_,
        occurred_at=now,
        persisted_at=now,
        node_id=node_id,
        step_execution_id=execution_id,
        step_attempt_id=attempt_id,
        command_id=command_id,
        worker_generation=generation,
        payload_json=encode(cleaned),
    )
    session.add(event)
    session.flush()
    return event


def event_catalog() -> dict[str, Any]:
    return {
        "event_version": 1,
        "types": sorted(EVENT_TYPES),
        "max_payload_bytes": MAX_PAYLOAD_BYTES,
        "errors": [
            "unknown_external_result",
            "invalid_response_format",
            "model_unavailable",
            "model_group_exhausted",
            "permission_required",
            "configuration_invalid",
            "limit_exceeded",
            "workspace_conflict",
        ],
        "envelope": EventEnvelope.model_json_schema(),
    }
