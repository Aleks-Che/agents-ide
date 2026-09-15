"""Pinned choices and durable evidence for the latest visit of each model node."""

from __future__ import annotations

import json
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from agents_ide.persistence.models import Run, StepAttempt, StepExecution


class CandidateEntry(BaseModel):
    member_id: str | None = None
    member_index: int
    enabled: bool = True
    harness_profile_id: str | None = None
    provider_connection_id: str | None = None
    model_id: str
    params: dict[str, Any] = Field(default_factory=dict)
    parameter_sources: dict[str, str] = Field(default_factory=dict)
    unavailable_reason: str | None = None
    state: Literal["available", "skipped", "consumed", "current", "selected", "succeeded"] = (
        "available"
    )
    last_reason: str | None = None


class ActualSelection(BaseModel):
    attempt_id: str
    status: str
    outcome: str | None = None
    member_index: int
    model_id: str
    harness_profile_id: str | None = None
    provider_connection_id: str | None = None


class VisitCandidateState(BaseModel):
    current_node_id: str | None = None
    execution_id: str | None = None
    visit_index: int | None = None
    cycle_id: int | None = None
    status: str | None = None
    current_member_index: int | None = None
    current_member_id: str | None = None
    current_model_id: str | None = None
    current_profile_or_connection_id: str | None = None
    retries: int = 0
    selection_round: int = 0
    history: list[dict[str, Any]] = Field(default_factory=list)
    history_complete: bool = True


class NodeSelection(BaseModel):
    node_id: str
    node_type: str
    role: str | None = None
    kind: Literal["agent", "llm"]
    model_group_id: str | None = None
    selection_kind: Literal["direct", "group"]
    direct_model_id: str | None = None
    direct_harness_profile_id: str | None = None
    direct_provider_connection_id: str | None = None
    candidates: list[CandidateEntry] = Field(default_factory=list)
    visit: VisitCandidateState | None = None
    actual: ActualSelection | None = None


class GroupRef(BaseModel):
    id: str
    name: str
    kind: str
    revision: int
    member_count: int
    enabled_count: int


class SelectionSummary(BaseModel):
    groups: list[GroupRef] = Field(default_factory=list)
    nodes: list[NodeSelection] = Field(default_factory=list)
    visit: VisitCandidateState | None = None
    group_changes_apply_only_to_new_runs: bool = True


def save_node_selection(runtime: dict[str, Any], run: Run, snapshot: dict[str, Any]) -> None:
    """Keep one checkpoint per node, before the runner clears the next visit's state."""
    config = snapshot.get("dependencies", {}).get("nodes", {}).get(run.current_node_id, {})
    if not run.current_execution_id or not (config.get("candidates") or config.get("model")):
        return
    runtime.setdefault("node_selections", {})[run.current_node_id] = {
        "execution_id": run.current_execution_id,
        **{
            key: runtime.get(key)
            for key in (
                "candidate_index",
                "next_candidate_index",
                "candidate_retries",
                "candidate_history",
                "selection_round",
            )
        },
    }


def build_selection_summary(session: Session, run: Run) -> SelectionSummary | None:
    """Read pinned configuration and attempts, never live catalogs or retained SSE events."""
    snapshot = json.loads(run.snapshot_json)
    runtime = json.loads(run.runtime_json or "{}")
    dependencies = snapshot.get("dependencies")
    if not isinstance(dependencies, dict):
        return None
    latest = (
        select(StepExecution.node_id, func.max(StepExecution.visit_index).label("visit_index"))
        .where(StepExecution.run_id == run.id)
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
                & (StepExecution.visit_index == latest.c.visit_index),
            )
            .where(StepExecution.run_id == run.id)
        )
    }
    attempts: dict[str, list[StepAttempt]] = {}
    if executions:
        for attempt in session.scalars(
            select(StepAttempt)
            .where(StepAttempt.execution_id.in_([row.id for row in executions.values()]))
            .order_by(StepAttempt.attempt_index)
        ):
            attempts.setdefault(attempt.execution_id, []).append(attempt)
    result = SelectionSummary()
    groups = dependencies.get("model_groups", {})
    for group in groups.values():
        result.groups.append(
            GroupRef(
                id=group["id"],
                name=group["name"],
                kind=group["kind"],
                revision=group["revision"],
                member_count=len(group["members"]),
                enabled_count=sum(bool(m.get("enabled", True)) for m in group["members"]),
            )
        )
    for graph_node in snapshot.get("graph", {}).get("nodes", []):
        if graph_node["type"] not in {"AgentTask", "LLMRequest"}:
            continue
        node_id = graph_node["id"]
        config = dependencies.get("nodes", {}).get(node_id, {})
        group_id = config.get("model_group_id")
        raw = config.get("candidates", [])
        if not raw and config.get("model"):
            raw = [
                {
                    "member_index": 0,
                    "model_id": config["model"],
                    "harness_profile_id": config.get("harness_profile_id"),
                    "provider_connection_id": config.get("connection_id"),
                    "params": config.get("params", {}),
                }
            ]
        node = NodeSelection(
            node_id=node_id,
            node_type=graph_node["type"],
            role=config.get("role"),
            kind="agent" if graph_node["type"] == "AgentTask" else "llm",
            model_group_id=group_id,
            selection_kind="group" if group_id else "direct",
            candidates=[
                CandidateEntry.model_validate({**c, "member_id": c.get("id")}) for c in raw
            ],
        )
        if not group_id and node.candidates:
            direct = node.candidates[0]
            node.direct_model_id = direct.model_id
            node.direct_harness_profile_id = direct.harness_profile_id
            node.direct_provider_connection_id = direct.provider_connection_id
        execution = executions.get(node_id)
        history: list[dict[str, Any]] = []
        used: dict[int, StepAttempt] = {}
        state: dict[str, Any] = {}
        if execution:
            state = runtime.get("node_selections", {}).get(node_id, {})
            if state.get("execution_id") != execution.id:
                state = {}
            if execution.id == run.current_execution_id:
                state = runtime
            history = list(state.get("candidate_history") or [])
            for attempt in attempts.get(execution.id, []):
                metadata = json.loads(attempt.selection_json)
                if "member_index" not in metadata:
                    continue
                used[metadata["member_index"]] = attempt
                node.actual = ActualSelection(
                    **{
                        key: metadata[key]
                        for key in (
                            "member_index",
                            "model_id",
                            "harness_profile_id",
                            "provider_connection_id",
                        )
                        if key in metadata
                    },
                    attempt_id=attempt.id,
                    status=attempt.status,
                    outcome=attempt.external_outcome,
                )
            visit = VisitCandidateState(
                current_node_id=node_id,
                execution_id=execution.id,
                visit_index=execution.visit_index,
                cycle_id=execution.cycle_id,
                status=execution.status,
                retries=state.get("candidate_retries") or 0,
                selection_round=state.get("selection_round") or 0,
                history=history,
                history_complete=bool(state),
            )
            node.visit = visit
            if execution.id == run.current_execution_id:
                result.visit = visit
        # Reasons belong to this node/visit. Later rounds overwrite earlier diagnostics.
        reasons = {entry["member_index"]: entry["reason"] for entry in history}
        for candidate in node.candidates:
            index = candidate.member_index
            used_attempt = used.get(index)
            candidate.last_reason = reasons.get(index) or (
                used_attempt.error_code if used_attempt else None
            )
            if not candidate.enabled or candidate.unavailable_reason:
                candidate.state = "skipped"
                candidate.last_reason = (
                    candidate.last_reason or candidate.unavailable_reason or "disabled"
                )
            elif used_attempt:
                candidate.state = "succeeded" if used_attempt.status == "succeeded" else "consumed"
            elif index in reasons:
                candidate.state = "skipped"
            # candidate_index remains set even after exhaustion; the forward cursor disqualifies it.
            if (
                node.visit
                and execution
                and execution.id == run.current_execution_id
                and execution.status != "succeeded"
                and index == state.get("candidate_index")
                and index >= (state.get("next_candidate_index") or 0)
                and run.state not in {"completed", "failed", "cancelled"}
            ):
                candidate.state = (
                    "current" if used_attempt and used_attempt.status == "running" else "selected"
                )
                node.visit.current_member_index = index
                node.visit.current_member_id = candidate.member_id
                node.visit.current_model_id = candidate.model_id
                node.visit.current_profile_or_connection_id = (
                    candidate.harness_profile_id or candidate.provider_connection_id
                )
        result.nodes.append(node)
    return result
