"""Visit tracking: indexes a Run into ``StepExecution`` and ``StepAttempt`` rows.

A visit is the engine's unit of work for one arrival at a node. A node may be
visited multiple times (loops, repair, PlanControl ``next_item``). Each visit
gets exactly one :class:`StepExecution` row; each external call during the
visit gets a separate :class:`StepAttempt` row.

The visit manager is responsible for the deterministic key derivation
(``(run_id, node_id, visit_index, cycle_id)``) and the counter bookkeeping
the architecture requires. Counter writes happen inside the short write
transaction held by the runner so resume of an interrupted worker does not
double-count visits.
"""

from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import new_id, utc_now
from agents_ide.persistence.models import Run as RunModel
from agents_ide.persistence.models import StepAttempt as StepAttemptModel
from agents_ide.persistence.models import StepExecution as StepExecutionModel


@dataclass
class VisitState:
    """Bookkeeping for a single node visit inside a Run."""

    run_id: str
    node_id: str
    visit_index: int
    cycle_id: int
    scope: str | None = None
    attempt_index: int = 0
    execution_id: str | None = None
    plan_item_ids: tuple[str, ...] = field(default_factory=tuple)
    excluded_member_indexes: set[int] = field(default_factory=set)


def next_visit_index(session: Session, run_id: str, node_id: str) -> int:
    """Return the visit_index for the next visit at ``node_id``."""

    stmt = (
        select(StepExecutionModel.visit_index)
        .where(StepExecutionModel.run_id == run_id)
        .where(StepExecutionModel.node_id == node_id)
        .order_by(StepExecutionModel.visit_index.desc())
    )
    highest = session.execute(stmt).scalars().first()
    return int(highest or 0) + 1


def next_cycle_id(session: Session, run_id: str) -> int:
    """Return the next ``cycle_id`` for a Run.

    Cycles are global counters shared across nodes. They never reset; the
    architecture contract requires preserving them across resume.
    """

    row = session.get(RunModel, run_id)
    if row is None:
        raise ValueError(f"Run {run_id} not found")
    if row.current_cycle_id is None:
        new_cycle: int = 1
    else:
        new_cycle = int(row.current_cycle_id) + 1
    row.current_cycle_id = new_cycle
    return new_cycle


def create_execution(session: Session, visit: VisitState) -> StepExecutionModel:
    """Insert the :class:`StepExecution` row for ``visit``.

    Must be called inside the runner's short write transaction.
    """

    execution = StepExecutionModel(
        id=new_id(),
        run_id=visit.run_id,
        node_id=visit.node_id,
        visit_index=visit.visit_index,
        cycle_id=visit.cycle_id,
        scope=visit.scope,
        status="running",
        started_at=utc_now(),
        finished_at=None,
        validated_result_json=None,
        decision=None,
        evidence_manifest_id=None,
        plan_item_ids_json="[]",
        attempt_count=0,
    )
    session.add(execution)
    session.flush()
    visit.execution_id = execution.id
    return execution


def create_attempt(session: Session, visit: VisitState) -> StepAttemptModel:
    """Insert the :class:`StepAttempt` row for the next attempt."""

    visit.attempt_index += 1
    attempt = StepAttemptModel(
        id=new_id(),
        execution_id=visit.execution_id or "",
        attempt_index=visit.attempt_index,
        status="prepared",
        operation_id=None,
        retry_safety="unknown",
        started_at=utc_now(),
        finished_at=None,
        external_outcome=None,
        error_code=None,
        error_details_json=None,
        tokens_used=None,
        cost_estimated=None,
        budget_quality=None,
        heartbeat_at=utc_now(),
    )
    session.add(attempt)
    session.flush()
    update_execution_counters(session, visit, visit.attempt_index)
    return attempt


def update_execution_counters(session: Session, visit: VisitState, attempt_count: int) -> None:
    """Persist the final :class:`StepExecution` counters for the visit."""

    execution = session.get(StepExecutionModel, visit.execution_id)
    if execution is None:
        return
    execution.attempt_count = max(int(execution.attempt_count or 0), attempt_count)


def mark_execution_status(
    session: Session,
    execution_id: str,
    status: str,
    *,
    decision: str | None = None,
    validated_result_json: str | None = None,
    finished_at: float | None = None,
) -> None:
    """Persist a terminal status for the execution row.

    Allowed statuses follow the ``ck_executions_status`` constraint defined
    in the 0002 migration.
    """

    execution = session.get(StepExecutionModel, execution_id)
    if execution is None:
        return
    execution.status = status
    if decision is not None:
        execution.decision = decision
    if validated_result_json is not None:
        execution.validated_result_json = validated_result_json
    execution.finished_at = finished_at if finished_at is not None else utc_now()


def find_executions(
    session: Session, run_id: str, node_id: str | None = None
) -> list[StepExecutionModel]:
    stmt = select(StepExecutionModel).where(StepExecutionModel.run_id == run_id)
    if node_id is not None:
        stmt = stmt.where(StepExecutionModel.node_id == node_id)
    stmt = stmt.order_by(
        StepExecutionModel.cycle_id, StepExecutionModel.visit_index, StepExecutionModel.node_id
    )
    return list(session.scalars(stmt))


def load_latest_results(
    session: Session,
    run_id: str,
    cycle_id: int | None,
    node_ids: Iterable[str],
    *,
    scope: str | None = None,
) -> dict[str, Any]:
    """Build the latest-decision mapping used by AST references.

    Returns a dict keyed by node id whose value matches
    :class:`agents_ide.domain.graph_ast.LatestResult`.
    """

    from agents_ide.domain.graph_ast import LatestResult, TruthValue
    from agents_ide.persistence.models import Run

    nodes = set(node_ids)
    run = session.get(Run, run_id)
    invalidated = (
        set(json.loads(run.runtime_json).get("invalidated_executions", [])) if run else set()
    )
    results: dict[str, Any] = {}
    for execution in find_executions(session, run_id):
        if execution.id in invalidated:
            continue
        if execution.node_id not in nodes:
            continue
        if execution.status != "succeeded" or (scope is not None and execution.scope != scope):
            continue
        if cycle_id is not None and execution.cycle_id != cycle_id:
            continue
        if not execution.validated_result_json:
            continue
        decision: TruthValue
        if execution.decision == "true":
            decision = TruthValue.TRUE
        elif execution.decision == "false":
            decision = TruthValue.FALSE
        else:
            decision = TruthValue.UNKNOWN
        try:
            payload = json.loads(execution.validated_result_json)
            # Plain-text agent output is also its report. Keep compatibility with
            # already completed steps, without changing their stored result.
            if isinstance(payload, dict) and isinstance(payload.get("text"), str):
                payload.setdefault("report", payload["text"])
            from agents_ide.domain.graph_ast import Value

            results[execution.node_id] = LatestResult(
                decision=decision,
                validated_result=Value.of(payload),
                evidence_manifest_id=execution.evidence_manifest_id,
                plan_item_ids=tuple(),
                execution_id=execution.id,
                attempt_id=session.scalar(
                    select(StepAttemptModel.id)
                    .where(
                        StepAttemptModel.execution_id == execution.id,
                        StepAttemptModel.status == "succeeded",
                    )
                    .order_by(StepAttemptModel.attempt_index.desc())
                    .limit(1)
                ),
                cycle_id=execution.cycle_id,
                scope=execution.scope,
                raw_result_ref=execution.raw_result_ref,
            )
        except (ValueError, TypeError, json.JSONDecodeError):
            results[execution.node_id] = LatestResult(
                decision=decision,
                validated_result=None,
                evidence_manifest_id=execution.evidence_manifest_id,
                plan_item_ids=tuple(),
                execution_id=execution.id,
                attempt_id=None,
                cycle_id=execution.cycle_id,
                scope=execution.scope,
                raw_result_ref=execution.raw_result_ref,
            )
    return results
