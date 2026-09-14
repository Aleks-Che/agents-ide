"""Sequential fake engine with durable dispatch intents and generation fencing.

Every mutation is a short BEGIN IMMEDIATE transaction. Adapter calls, workspace
probes and retry delays run outside transactions. An expired owner is never
allowed to commit a result or dispatch another attempt; stage 5 reconciles it.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from agents_ide import __version__
from agents_ide.adapters.base import (
    AdapterError,
    AgentAdapter,
    AgentAdapterRequest,
    AgentResult,
    ExternalOutcome,
    LLMAdapter,
    LLMAdapterRequest,
    LLMResult,
)
from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, parse_fake_scenario
from agents_ide.domain.common import new_id, to_json, utc_now
from agents_ide.domain.contracts import RunState
from agents_ide.domain.graph_ast import (
    ASTError,
    ASTNode,
    EvaluationContext,
    Value,
    apply_assignments,
    evaluate_truth,
    substitute,
)
from agents_ide.domain.graph_validation import check_version_features, validate_graph
from agents_ide.domain.schemas import WaitingReason
from agents_ide.domain.workspace import collect_workspace, workspace_scope
from agents_ide.engine import artifacts, events, visits
from agents_ide.engine.queue import owned_job
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    AgentSession,
    CommandJournal,
    HarnessProfile,
    ProviderConnection,
    QueueJob,
    Run,
    StepAttempt,
    StepExecution,
    WorkspaceReservation,
)
from agents_ide.security.secrets import SecretStore
from agents_ide.services.transactions import begin_write


@dataclass
class RunnerResult:
    final_state: RunState
    waiting_reason: WaitingReason | None = None


def build_runner(
    *,
    session_factory: sessionmaker[Session],
    worker_id: str,
    generation: int,
    data_dir: Path,
    secret_store: SecretStore | None,
    abort: threading.Event | None = None,
) -> Runner:
    return Runner(
        session_factory=session_factory,
        worker_id=worker_id,
        generation=generation,
        data_dir=data_dir,
        secret_store=secret_store,
        abort=abort,
    )


class Runner:
    def __init__(
        self,
        *,
        session_factory: sessionmaker[Session],
        worker_id: str,
        generation: int,
        data_dir: Path,
        secret_store: SecretStore | None,
        abort: threading.Event | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.worker_id, self.generation = worker_id, generation
        self.data_dir, self.secret_store = Path(data_dir), secret_store
        self.abort = abort or threading.Event()
        self.run_id = ""
        self.snapshot: dict[str, Any] = {}
        self.runtime: dict[str, Any] = {}
        self.nodes: dict[str, Any] = {}
        self.simulated = False

    @contextmanager
    def _write(self) -> Iterator[tuple[Session, Run]]:
        if self.abort.is_set():
            raise AppError("queue_job_lost", "Worker запретил новые действия", 409)
        with self.session_factory() as session:
            begin_write(session)
            job = owned_job(session, self.run_id, self.worker_id, self.generation)
            run = session.get(Run, self.run_id)
            if (
                run is None
                or run.worker_id != self.worker_id
                or run.worker_generation != self.generation
            ):
                raise AppError("queue_job_lost", "Поколение владельца изменилось", 409)
            reservation = session.scalar(
                select(WorkspaceReservation).where(
                    WorkspaceReservation.run_id == self.run_id,
                    WorkspaceReservation.released_at.is_(None),
                    WorkspaceReservation.owner_generation == self.generation,
                )
            )
            if reservation is None:
                raise AppError("queue_job_lost", "Нет резервации рабочего каталога", 409)
            yield session, run
            if (
                self.abort.is_set()
                or job.lease_expires_at is None
                or job.lease_expires_at <= utc_now()
            ):
                raise AppError("queue_job_lost", "Владение истекло до фиксации результата", 409)
            session.commit()

    def _event(
        self,
        session: Session,
        type_: str,
        payload: dict[str, Any],
        visit: visits.VisitState | None = None,
        attempt_id: str | None = None,
        command_id: str | None = None,
    ) -> None:
        events.append_event(
            session,
            self.run_id,
            type_,
            payload,
            generation=self.generation,
            simulated=self.simulated,
            node_id=visit.node_id if visit else None,
            execution_id=visit.execution_id if visit else None,
            attempt_id=attempt_id,
            command_id=command_id,
        )

    def _persist(self, run: Run) -> None:
        now = utc_now()
        active = self.runtime.get("active_since")
        if active is not None:
            self.runtime["duration_seconds"] += max(0, now - active)
            self.runtime["active_since"] = now
        run.runtime_json = to_json(self.runtime)
        run.current_cycle_id = self.runtime.get("cycle_id")
        run.updated_at = now

    def _state(
        self, session: Session, run: Run, state: str, reason: WaitingReason | None = None
    ) -> RunnerResult:
        previous = run.state
        self._persist(run)
        now = utc_now()
        intervals = json.loads(run.active_intervals_json)
        if intervals and intervals[-1]["ended_at"] is None:
            intervals[-1]["ended_at"] = datetime.fromtimestamp(now, UTC).isoformat()
        if state in {"running", "retry_wait"}:
            intervals.append(
                {
                    "state": state,
                    "started_at": datetime.fromtimestamp(now, UTC).isoformat(),
                    "ended_at": None,
                    "quality": "observed",
                }
            )
            self.runtime["active_since"] = now
        else:
            self.runtime["active_since"] = None
        run.active_intervals_json = to_json(intervals)
        run.runtime_json = to_json(self.runtime)
        run.state, run.state_version = state, run.state_version + 1
        run.waiting_reason_json = to_json(reason.model_dump(mode="json")) if reason else None
        if state in {"completed", "failed", "cancelled"}:
            run.finished_at = now
            for reservation in session.scalars(
                select(WorkspaceReservation).where(
                    WorkspaceReservation.run_id == run.id,
                    WorkspaceReservation.released_at.is_(None),
                    WorkspaceReservation.owner_generation == self.generation,
                )
            ):
                reservation.released_at = now
        if state not in {"running", "retry_wait"}:
            job = session.scalar(select(QueueJob).where(QueueJob.run_id == run.id))
            if job is not None:
                session.delete(job)
        self._event(
            session,
            "run.state_changed",
            {"from": previous, "to": state, "state_version": run.state_version},
        )
        if reason:
            self._event(
                session,
                "run.waiting_input",
                {
                    "from": previous,
                    "to": state,
                    "state_version": run.state_version,
                    "reason": reason.model_dump(mode="json"),
                    "allowed_actions": reason.allowed_actions,
                },
            )
        elif state in {"completed", "failed", "cancelled", "paused", "stopped"}:
            self._event(session, f"run.{state}", {})
        return RunnerResult(RunState(state), reason)

    def _waiting(
        self, code: str, details: dict[str, Any], visit: visits.VisitState | None = None
    ) -> RunnerResult:
        reason = WaitingReason.model_validate(
            {
                "code": code,
                "details": artifacts.sanitize(details),
                "allowed_actions": [],
            }
        )
        with self._write() as (session, run):
            if visit and visit.execution_id:
                execution = session.get(StepExecution, visit.execution_id)
                if execution and execution.status == "running":
                    execution.status = "waiting_input"
            run.resume_target_json = to_json(
                {
                    "action": "reconcile"
                    if code == "unknown_external_result"
                    else "retry_attempt"
                    if visit
                    else "dispatch_next",
                    "node_id": visit.node_id if visit else self.runtime.get("next_node_id"),
                    "execution_id": visit.execution_id if visit else None,
                    "retry_at": self.runtime.get("retry_at"),
                    "blockers": [code],
                }
            )
            if code == "limit_exceeded":
                self._event(session, "budget.exceeded", details, visit)
            return self._state(session, run, "waiting_input", reason)

    def execute(self, run_id: str) -> RunnerResult:
        self.run_id = run_id
        with self.session_factory() as session:
            row = session.get(Run, run_id)
            if row is None:
                raise AppError("run_not_found", "Run не найден", 404)
            if row.state != "queued":
                return RunnerResult(RunState(row.state))
            self.snapshot = json.loads(row.snapshot_json)
            self.runtime = json.loads(row.runtime_json)
            has_history = (
                session.scalar(
                    select(StepExecution.id).where(StepExecution.run_id == run_id).limit(1)
                )
                is not None
            )
        self.simulated = self.snapshot.get("execution_mode") == "simulated"
        graph = self.snapshot["graph"]
        self.nodes = {node["id"]: node for node in graph["nodes"]}
        if self.runtime or has_history:
            # No implicit restart from Start. Resume/reconciliation belongs to stage 5.
            return self._waiting(
                "unknown_external_result", {"reason": "checkpoint_requires_recovery"}
            )
        start = next((n["id"] for n in graph["nodes"] if n["type"] == "Start"), None)
        self.runtime = {
            "next_node_id": start,
            "cycle_id": 1,
            "external_calls": 0,
            "visits": 0,
            "backward_transitions": 0,
            "duration_seconds": 0.0,
            "active_since": None,
            "loop_counts": {},
            "retry_at": None,
            "candidate_index": None,
            "candidate_history": [],
            "tokens_used": 0,
            "cost_estimated": 0.0,
            "budget_quality": "unknown",
            "work": {
                "mode": "initial",
                "feedback": "",
                "plan_feedback": "",
                "scope": "main",
                "cycle_id": 1,
                "current_plan_item_id": None,
                "plan_item_ids": [],
                "completed_items": [],
                "remaining_items": [],
            },
        }
        with self._write() as (session, run):
            if run.state != "queued":
                return RunnerResult(RunState(run.state))
            if run.runtime_json != "{}":
                raise AppError("run_already_started", "Run уже имеет checkpoint", 409)
            run.started_at = run.started_at or utc_now()
            self._state(session, run, "running")
            self._event(
                session, "run.started", {"worker_id": self.worker_id, "generation": self.generation}
            )
        report = validate_graph(graph, inputs=self.snapshot["input"]["values"])
        check_version_features(
            self.snapshot["schema_version"], self.snapshot.get("required_features", []), report
        )
        if (
            self.snapshot.get("dependencies", {}).get("model_groups")
            and self.snapshot["dependencies"].get("model_selection_version") != 1
        ):
            return self._waiting(
                "schema_unsupported", {"reason": "incomplete_model_group_snapshot"}
            )
        if not report.ok or self.snapshot.get("engine_version") != __version__:
            return self._waiting(
                "schema_unsupported", {"errors": [e.to_dict() for e in report.errors]}
            )
        if not self.simulated and any(
            n["type"] not in {"Start", "End", "Condition"} for n in self.nodes.values()
        ):
            return self._waiting(
                "configuration_invalid",
                {"reason": "real_adapters_unimplemented", "required_stages": [5, 6, 7, 8]},
            )
        try:
            self._workspace_check()
            agent, llm = self._build_adapters(self.snapshot)
            while self.runtime["next_node_id"] is not None:
                control = self._controls()
                if control:
                    return control
                limit = self._limit("visits")
                if limit:
                    return self._waiting("limit_exceeded", {"limit": limit})
                node = self.nodes[self.runtime["next_node_id"]]
                with self._write() as (session, run):
                    visit = visits.VisitState(
                        run_id,
                        node["id"],
                        visits.next_visit_index(session, run_id, node["id"]),
                        self.runtime["cycle_id"],
                        scope=self.runtime["work"]["scope"],
                    )
                    visits.create_execution(session, visit)
                    self.runtime["visits"] += 1
                    self.runtime["candidate_index"], self.runtime["candidate_history"] = None, []
                    run.current_node_id, run.current_execution_id, run.current_attempt_id = (
                        node["id"],
                        visit.execution_id,
                        None,
                    )
                    self._persist(run)
                    self._event(
                        session,
                        "node.entered",
                        {"visit_index": visit.visit_index, "cycle_id": visit.cycle_id},
                        visit,
                    )
                result: AgentResult | LLMResult | None = None
                if node["type"] in {"AgentTask", "LLMRequest"}:
                    external = self._external(
                        node, visit, agent if node["type"] == "AgentTask" else llm
                    )
                    if isinstance(external, RunnerResult):
                        return external
                    result = external
                elif node["type"] not in {"Start", "End", "Condition"}:
                    return self._waiting(
                        "configuration_invalid",
                        {"node_id": node["id"], "reason": "node_executor_unimplemented"},
                        visit,
                    )
                finished = self._finish_visit(node, visit, result)
                if finished:
                    return finished
            return RunnerResult(RunState.COMPLETED)
        except ASTError:
            return self._waiting("configuration_invalid", {"reason": "runtime_expression_invalid"})
        except AppError as exc:
            if exc.code in {"queue_job_lost", "database_unavailable"}:
                raise
            return self._waiting(
                "workspace_conflict"
                if exc.code in {"workspace_conflict", "path_invalid", "path_unavailable"}
                else "configuration_invalid",
                {"reason": exc.code},
            )
        except (ValueError, OSError):
            return self._waiting(
                "configuration_invalid", {"reason": "runtime_configuration_invalid"}
            )

    def _limits(self) -> dict[str, int]:
        return {
            "max_calls": 100,
            "max_node_visits": 1000,
            "max_backward_transitions": 200,
            "max_duration_seconds": 86400,
            **self.snapshot.get("resolved_settings", {}).get("limit_overrides", {}),
        }

    def _limit(self, operation: str) -> str | None:
        limits = self._limits()
        elapsed = self.runtime["duration_seconds"] + (
            max(0, utc_now() - self.runtime["active_since"]) if self.runtime["active_since"] else 0
        )
        if elapsed >= limits["max_duration_seconds"]:
            return "max_duration_seconds"
        key = {"visits": "max_node_visits", "external_calls": "max_calls"}.get(operation)
        if key and self.runtime[operation] >= limits[key]:
            return key
        return None

    def _workspace_check(self) -> None:
        workspace = self.snapshot["workspace"]
        _, normalized, dev, ino, git = collect_workspace(workspace["workspace_path"])
        if [dev, ino] != [workspace["identity_dev"], workspace["identity_ino"]] or workspace_scope(
            Path(normalized), git
        ).get("git_common_identity") != workspace["scope"].get("git_common_identity"):
            raise AppError("workspace_conflict", "Рабочий каталог изменился после Start", 409)

    def _build_adapters(self, snapshot: dict[str, Any]) -> tuple[AgentAdapter, LLMAdapter]:
        if not self.simulated:
            # Pure server graphs never call these; there is no implicit simulation.
            return AgentAdapter(), LLMAdapter()
        root = self.data_dir.resolve() / "simulated"
        workspace = root / self.run_id
        workspace.mkdir(parents=True, exist_ok=True)
        if root.resolve() != root or workspace.resolve() != workspace:
            raise AppError("path_violation", "Каталог simulation перенаправлен", 409)
        scenario = parse_fake_scenario(snapshot.get("fake_scenario"))
        return FakeAgentAdapter(scenario, workspace), FakeLLMAdapter(scenario, workspace)

    def _context(self, session: Session) -> EvaluationContext:
        work = {key: Value.of(value) for key, value in self.runtime["work"].items()}
        return EvaluationContext(
            inputs={
                key: Value.of(value) for key, value in self.snapshot["input"]["values"].items()
            },
            latest=visits.load_latest_results(
                session,
                self.run_id,
                self.runtime["cycle_id"],
                self.nodes,
                scope=self.runtime["work"]["scope"],
            ),
            work=work,
            known_node_ids=frozenset(self.nodes),
            project={
                "id": Value.of(self.snapshot["workspace"]["project_id"]),
                "path": Value.of(self.snapshot["workspace"]["workspace_path"]),
            },
            run={
                "id": Value.of(self.run_id),
                "cycle_id": Value.of(self.runtime["cycle_id"]),
                "remaining_limits": Value.of(
                    {
                        "max_calls": max(
                            0, self._limits()["max_calls"] - self.runtime["external_calls"]
                        )
                    }
                ),
            },
            cycle_id=self.runtime["cycle_id"],
            scope=self.runtime["work"]["scope"],
        )

    def _controls(self, *, allow_pause: bool = True) -> RunnerResult | None:
        with self._write() as (session, run):
            commands = list(
                session.scalars(
                    select(CommandJournal)
                    .where(
                        CommandJournal.run_id == run.id,
                        CommandJournal.status == "accepted",
                        CommandJournal.command_type.in_(["pause", "stop", "cancel"]),
                    )
                    .order_by(CommandJournal.sequence)
                )
            )
            relevant = [c for c in commands if allow_pause or c.command_type != "pause"]
            if not relevant:
                return None
            chosen = max(
                relevant,
                key=lambda c: ({"pause": 1, "stop": 2, "cancel": 3}[c.command_type], c.sequence),
            )
            target = {"pause": "paused", "stop": "stopped", "cancel": "cancelled"}[
                chosen.command_type
            ]
            for command in commands:
                command.status = "applied" if command.id == chosen.id else "superseded"
                command.applied_at = utc_now()
                command.response_json = to_json({"state": target})
                self._event(
                    session,
                    "control.applied",
                    {"command_type": command.command_type, "status": command.status},
                    command_id=command.command_id,
                )
            execution = (
                session.get(StepExecution, run.current_execution_id)
                if run.current_execution_id
                else None
            )
            if execution and execution.status in {"running", "retry_wait"}:
                execution.status = "interrupted"
            run.resume_target_json = to_json(
                {
                    "action": "retry_attempt"
                    if execution and execution.status == "interrupted"
                    else "dispatch_next",
                    "execution_id": run.current_execution_id,
                    "node_id": self.runtime["next_node_id"],
                    "retry_at": self.runtime.get("retry_at"),
                    "blockers": [],
                }
            )
            return self._state(session, run, target)

    def _candidate_metadata(
        self, node: dict[str, Any], candidate: dict[str, Any]
    ) -> dict[str, Any]:
        config = self.snapshot["dependencies"]["nodes"][node["id"]]
        group_id = config.get("model_group_id")
        group = self.snapshot["dependencies"].get("model_groups", {}).get(group_id, {})
        return {
            "group_id": group_id,
            "group_revision": group.get("revision"),
            "kind": "agent" if node["type"] == "AgentTask" else "llm",
            "member_id": candidate.get("id"),
            "member_index": candidate["member_index"],
            "profile_or_connection_id": candidate.get("harness_profile_id")
            or candidate.get("provider_connection_id"),
            "harness_profile_id": candidate.get("harness_profile_id"),
            "provider_connection_id": candidate.get("provider_connection_id"),
            "model_id": candidate["model_id"],
            "params": candidate.get("params", {}),
        }

    def _availability(self, candidate: dict[str, Any]) -> str | None:
        if not candidate.get("enabled", True):
            return "disabled"
        if candidate.get("unavailable_reason"):
            return str(candidate["unavailable_reason"])
        agent = bool(candidate.get("harness_profile_id"))
        ref = candidate.get("harness_profile_id") or candidate.get("provider_connection_id")
        with self.session_factory() as session:
            resource = (
                session.get(HarnessProfile, ref) if agent else session.get(ProviderConnection, ref)
            )
            if resource is None:
                return "missing"
            if resource.archived_at is not None:
                return "archived"
            if (
                candidate.get("resource_version")
                and resource.version != candidate["resource_version"]
            ):
                return "resource_changed"
            if isinstance(resource, ProviderConnection) and resource.secret_reference:
                if self.secret_store is None:
                    return "secret_unavailable"
                try:
                    self.secret_store.get(resource.secret_reference)
                except Exception:
                    return "secret_unavailable"
        return None

    def _candidate_list(self, node: dict[str, Any]) -> list[dict[str, Any]]:
        config = self.snapshot["dependencies"]["nodes"][node["id"]]
        if "candidates" in config:
            return list(config["candidates"])
        if config.get("model"):
            return [
                {
                    "id": None,
                    "member_index": 0,
                    "enabled": True,
                    "model_id": config["model"],
                    "harness_profile_id": config.get("harness_profile_id"),
                    "provider_connection_id": config.get("connection_id"),
                    "params": config.get("params", {}),
                }
            ]
        return []

    def _external(
        self, node: dict[str, Any], visit: visits.VisitState, adapter: AgentAdapter | LLMAdapter
    ) -> AgentResult | LLMResult | RunnerResult:
        config = self.snapshot["dependencies"]["nodes"][node["id"]]
        mode = self.runtime["work"]["mode"]
        template = config.get(
            "prompt_repair"
            if mode == "repair"
            else "prompt_next_item"
            if mode == "next_item"
            else "prompt"
        ) or config.get("prompt", "")
        with self.session_factory() as session:
            prompt = substitute(template, self._context(session), strict=True)
        previous: dict[str, Any] | None = None
        diagnostics: list[dict[str, Any]] = []
        for candidate in self._candidate_list(node):
            metadata = self._candidate_metadata(node, candidate)
            reason = self._availability(candidate)
            if reason:
                diagnostics.append({**metadata, "reason": reason})
                with self._write() as (session, run):
                    self.runtime["candidate_history"].append(diagnostics[-1])
                    self._event(session, "model_group.candidate_skipped", diagnostics[-1], visit)
                    self._persist(run)
                continue
            with self._write() as (session, run):
                self.runtime["candidate_index"] = candidate["member_index"]
                if previous:
                    self._event(
                        session,
                        "model_group.candidate_switched",
                        {
                            **metadata,
                            "previous_member_id": previous["member_id"],
                            "reason": previous["reason"],
                            "history_length": len(self.runtime["candidate_history"]),
                        },
                        visit,
                    )
                self._event(
                    session,
                    "model_group.candidate_selected",
                    {**metadata, "reason": "first_available" if previous is None else "fallback"},
                    visit,
                )
                self._persist(run)
            retries = 0
            while True:
                control = self._controls()
                if control:
                    return control
                if limit := self._limit("external_calls"):
                    return self._waiting("limit_exceeded", {"limit": limit}, visit)
                self._workspace_check()
                reason = self._availability(candidate)
                if reason:
                    diagnostics.append({**metadata, "reason": reason})
                    break
                with self._write() as (session, run):
                    attempt = visits.create_attempt(session, visit)
                    attempt_id = attempt.id
                    attempt.operation_id = new_id()
                    attempt.selection_json = to_json(
                        {
                            **metadata,
                            "reason": "retry" if retries else "fallback" if previous else "initial",
                            "retry_index": retries,
                        }
                    )
                    attempt.status = "running"
                    context_package = {
                        "__node_id__": node["id"],
                        "input": self.snapshot["input"],
                        "work": self.runtime["work"],
                    }
                    artifact = self._artifact(
                        session,
                        visit,
                        "attempt_input",
                        {
                            "prompt": prompt,
                            "inputs": self.snapshot["input"],
                            "context": context_package,
                            "candidate": metadata,
                            "visit_index": visit.visit_index,
                        },
                        attempt_id,
                    )
                    attempt.request_artifact_id = artifact.id
                    run.current_attempt_id = attempt_id
                    self.runtime["external_calls"] += 1
                    self.runtime["retry_at"] = None
                    if node["type"] == "AgentTask":
                        session.add(
                            AgentSession(
                                id=new_id(),
                                attempt_id=attempt_id,
                                harness_kind="fake",
                                role=config.get("role", ""),
                                capabilities_json=to_json({"simulated": True, "network": False}),
                                started_at=utc_now(),
                            )
                        )
                    self._persist(run)
                    self._event(
                        session,
                        "attempt.started",
                        {
                            **metadata,
                            "attempt_index": visit.attempt_index,
                            "operation_id": attempt.operation_id,
                            "input_artifact_id": artifact.id,
                        },
                        visit,
                        attempt_id,
                    )
                request: AgentAdapterRequest | LLMAdapterRequest

                def emit_progress(
                    type_: str, payload: dict[str, Any], _attempt_id: str = attempt_id
                ) -> None:
                    if type_ != "attempt.text_delta":
                        raise ValueError("Adapter cannot emit state transitions")
                    with self._write() as (progress_session, progress_run):
                        self._event(
                            progress_session,
                            type_,
                            {**payload, "late": progress_run.current_attempt_id != _attempt_id},
                            visit,
                            _attempt_id,
                        )
                        self._persist(progress_run)

                common = {
                    "role": str(config.get("role", "")),
                    "model_id": str(candidate["model_id"]),
                    "prompt": prompt,
                    "context_package": context_package,
                    "params": candidate.get("params", {}),
                    "attempt_index": visit.attempt_index,
                    "visit_index": visit.visit_index,
                    "emit_event": emit_progress,
                    "deadline_at": utc_now()
                    + min(
                        node.get("timeout_seconds", 86400),
                        max(
                            0,
                            self._limits()["max_duration_seconds"]
                            - self.runtime["duration_seconds"],
                        ),
                    ),
                }
                if node["type"] == "AgentTask":
                    request = AgentAdapterRequest(
                        **common,
                        workspace_path=str(self.data_dir / "simulated" / self.run_id),
                        capabilities={"source": "simulated", "network": False},
                    )
                else:
                    request = LLMAdapterRequest(
                        **common,
                        response_format=config.get("response_format", "text"),
                        output_schema=config.get("output_schema"),
                    )
                try:
                    result = call_adapter(adapter, request)
                except Exception:
                    result = LLMResult(
                        ExternalOutcome.UNKNOWN,
                        "",
                        None,
                        None,
                        error=AdapterError(
                            "adapter_exception", "Адаптер не подтвердил исход", "unknown"
                        ),
                    )
                validation_error = self._normalize_result(config, result)
                self._save_attempt(visit, attempt_id, result, validation_error)
                if result.outcome in {
                    ExternalOutcome.SUCCEEDED,
                    ExternalOutcome.INVALID_FORMAT,
                } or (
                    result.no_effect
                    and result.outcome
                    not in {
                        ExternalOutcome.UNKNOWN,
                        ExternalOutcome.TRANSPORT_DROPPED,
                        ExternalOutcome.PROCESS_DIED,
                    }
                ):
                    control = self._controls(allow_pause=False)
                    if control:
                        return control
                if result.outcome == ExternalOutcome.SUCCEEDED and validation_error is None:
                    return result
                if validation_error or result.outcome == ExternalOutcome.INVALID_FORMAT:
                    return self._waiting(
                        "invalid_response_format",
                        {
                            "node_id": node["id"],
                            "attempt_id": attempt_id,
                            "reason": validation_error or "adapter_invalid_format",
                        },
                        visit,
                    )
                if result.outcome == ExternalOutcome.PERMISSION_DENIED:
                    return self._waiting(
                        "permission_required",
                        {"node_id": node["id"], "attempt_id": attempt_id},
                        visit,
                    )
                safe = bool(
                    result.no_effect and result.error and result.error.retry_safety == "safe"
                )
                if result.outcome == ExternalOutcome.RETRYABLE_FAILURE and safe:
                    if retries < node.get("max_retries", 2):
                        retries += 1
                        wait = self._retry(visit, retries)
                        if wait:
                            return wait
                        continue
                    reason = "retries_exhausted"
                elif result.outcome == ExternalOutcome.UNAVAILABLE and safe:
                    reason = result.error.code if result.error else "unavailable"
                elif result.outcome == ExternalOutcome.CONFIRMED_FAILURE and safe:
                    if result.error and result.error.code != "configuration_invalid":
                        with self._write() as (session, run):
                            execution = session.get(StepExecution, visit.execution_id)
                            assert execution is not None
                            execution.status, execution.finished_at = "failed", utc_now()
                            self._event(
                                session,
                                "error.technical",
                                {
                                    "code": result.error.code,
                                    "retry_safety": "safe",
                                    "outcome": result.outcome.value,
                                },
                                visit,
                                attempt_id,
                            )
                            return self._state(session, run, "failed")
                    return self._waiting(
                        "configuration_invalid",
                        {
                            "node_id": node["id"],
                            "error": result.error.code if result.error else "confirmed_failure",
                        },
                        visit,
                    )
                else:
                    return self._waiting(
                        "unknown_external_result",
                        {
                            "node_id": node["id"],
                            "attempt_id": attempt_id,
                            "outcome": result.outcome.value,
                        },
                        visit,
                    )
                previous = {**metadata, "reason": reason}
                diagnostics.append(previous)
                with self._write() as (session, run):
                    self.runtime["candidate_history"].append(previous)
                    self._persist(run)
                break
        grouped = bool(config.get("model_group_id"))
        if grouped:
            with self._write() as (session, run):
                artifact = self._artifact(session, visit, "candidate_diagnostics", diagnostics)
                group = self.snapshot["dependencies"]["model_groups"][config["model_group_id"]]
                self._event(
                    session,
                    "model_group.exhausted",
                    {
                        "group_id": group["id"],
                        "group_revision": group["revision"],
                        "kind": group["kind"],
                        "history_length": len(self.runtime["candidate_history"]),
                        "diagnostics_ref": artifact.id,
                    },
                    visit,
                )
                self._persist(run)
        return self._waiting(
            "model_group_exhausted" if grouped else "model_unavailable",
            {"node_id": node["id"], "candidates": diagnostics},
            visit,
        )

    def _retry(self, visit: visits.VisitState, retries: int) -> RunnerResult | None:
        retry_at = utc_now() + min(0.25 * 2 ** (retries - 1), 5)
        self.runtime["retry_at"] = retry_at
        with self._write() as (session, run):
            execution = session.get(StepExecution, visit.execution_id)
            assert execution is not None
            execution.status = "retry_wait"
            run.resume_target_json = to_json(
                {
                    "action": "retry_attempt",
                    "execution_id": visit.execution_id,
                    "retry_at": retry_at,
                    "candidate_index": self.runtime["candidate_index"],
                }
            )
            self._event(
                session,
                "attempt.retry_scheduled",
                {"retry_at": retry_at, "retry_index": retries},
                visit,
            )
            self._state(session, run, "retry_wait")
        while utc_now() < retry_at:
            if control := self._controls():
                return control
            if limit := self._limit("external_calls"):
                return self._waiting("limit_exceeded", {"limit": limit}, visit)
            self.abort.wait(min(0.05, max(0, retry_at - utc_now())))
        with self._write() as (session, run):
            execution = session.get(StepExecution, visit.execution_id)
            assert execution is not None
            execution.status = "running"
            self._state(session, run, "running")
        return None

    def _normalize_result(
        self, config: dict[str, Any], result: AgentResult | LLMResult
    ) -> str | None:
        if result.outcome != ExternalOutcome.SUCCEEDED:
            return None
        if len(result.raw_text.encode("utf-8")) > artifacts.MAX_ARTIFACT_BYTES:
            return "response_too_large"
        body = result.validated_result
        if config.get("response_format") == "json" or config.get("output_schema"):
            try:
                body = json.loads(result.raw_text)
                if config.get("output_schema") and not Draft202012Validator(
                    config["output_schema"]
                ).is_valid(body):
                    return "schema_mismatch"
                if not isinstance(body, dict):
                    return "object_required"
            except (ValueError, RecursionError):
                return "invalid_json"
        verdict = (
            body.get("verdict", result.decision) if isinstance(body, dict) else result.decision
        )
        if verdict is not None and (
            not isinstance(verdict, str)
            or verdict not in {"passed", "failed", "inconclusive", "unknown"}
        ):
            return "invalid_verdict"
        try:
            if len(artifacts.encode(body).encode("utf-8")) > artifacts.MAX_ARTIFACT_BYTES:
                return "response_too_large"
        except (ValueError, TypeError, RecursionError):
            return "invalid_result"
        # Result envelopes are frozen: only the server-created normalized copy is persisted.
        object.__setattr__(result, "validated_result", artifacts.sanitize(body))
        object.__setattr__(result, "decision", verdict)
        return None

    def _artifact(
        self,
        session: Session,
        visit: visits.VisitState,
        kind: str,
        body: Any,
        attempt_id: str | None = None,
    ) -> Any:
        manifest = artifacts.record_artifact(
            session,
            self.run_id,
            artifacts.ArtifactPayload(kind, body=body),
            step_execution_id=visit.execution_id,
            step_attempt_id=attempt_id,
            cycle_id=visit.cycle_id,
            source_kind="simulated" if self.simulated else "engine",
        )
        self._event(
            session,
            "artifact.recorded",
            {
                "artifact_id": manifest.id,
                "manifest_id": manifest.id,
                "schema_type": kind,
                "byte_length": manifest.byte_length,
                "content_hash": manifest.content_hash,
            },
            visit,
            attempt_id,
        )
        return manifest

    def _save_attempt(
        self,
        visit: visits.VisitState,
        attempt_id: str,
        result: AgentResult | LLMResult,
        validation_error: str | None,
    ) -> None:
        with self._write() as (session, run):
            attempt = session.get(StepAttempt, attempt_id)
            assert attempt is not None
            unknown = result.outcome in {
                ExternalOutcome.UNKNOWN,
                ExternalOutcome.TRANSPORT_DROPPED,
                ExternalOutcome.PROCESS_DIED,
            } or (
                result.outcome != ExternalOutcome.SUCCEEDED
                and not result.no_effect
                and result.outcome
                not in {ExternalOutcome.PERMISSION_DENIED, ExternalOutcome.INVALID_FORMAT}
            )
            attempt.status = (
                "unknown"
                if unknown
                else "succeeded"
                if result.outcome == ExternalOutcome.SUCCEEDED and validation_error is None
                else "failed"
            )
            attempt.finished_at = utc_now()
            attempt.external_outcome = result.outcome.value
            attempt.retry_safety = result.error.retry_safety if result.error else "unsafe"
            attempt.error_code = validation_error or (result.error.code if result.error else None)
            attempt.error_details_json = (
                to_json(
                    artifacts.sanitize(
                        {
                            "message": result.error.message,
                            "details": result.error.details,
                            "no_effect": result.no_effect,
                        }
                    )
                )
                if result.error
                else None
            )
            if isinstance(result, LLMResult):
                attempt.tokens_used, attempt.cost_estimated, attempt.budget_quality = (
                    result.tokens_used,
                    result.cost_estimated,
                    result.budget_quality or "unknown",
                )
                self.runtime["tokens_used"] += result.tokens_used or 0
                self.runtime["cost_estimated"] += result.cost_estimated or 0
                self.runtime["budget_quality"] = (
                    "unknown"
                    if result.tokens_used is None or result.budget_quality in {None, "unknown"}
                    else result.budget_quality
                    if self.runtime["external_calls"] == 1
                    else "unknown"
                    if self.runtime["budget_quality"] == "unknown"
                    else "estimated"
                    if "estimated" in {self.runtime["budget_quality"], result.budget_quality}
                    else "observed"
                )
            artifact = self._artifact(
                session,
                visit,
                "agent_response" if isinstance(result, AgentResult) else "llm_response",
                {
                    "raw_text": result.raw_text,
                    "validated": result.validated_result if validation_error is None else None,
                    "decision": result.decision if validation_error is None else None,
                    "error_code": attempt.error_code,
                },
                attempt_id,
            )
            attempt.result_artifact_id = artifact.id
            for agent_session in session.scalars(
                select(AgentSession).where(AgentSession.attempt_id == attempt_id)
            ):
                agent_session.finished_at = attempt.finished_at
            self._event(
                session,
                "attempt.finished",
                {
                    "attempt_index": attempt.attempt_index,
                    "operation_id": attempt.operation_id,
                    "outcome": result.outcome.value,
                    "status": attempt.status,
                    "error_code": attempt.error_code,
                    "result_ref": artifact.id,
                    "elapsed_seconds": result.elapsed_seconds,
                },
                visit,
                attempt_id,
            )
            self._event(
                session,
                "budget.updated",
                {
                    "external_calls": self.runtime["external_calls"],
                    "tokens_used": self.runtime["tokens_used"],
                    "cost_estimated": self.runtime["cost_estimated"],
                    "quality": self.runtime["budget_quality"],
                },
                visit,
                attempt_id,
            )
            self._persist(run)

    def _finish_visit(
        self, node: dict[str, Any], visit: visits.VisitState, result: AgentResult | LLMResult | None
    ) -> RunnerResult | None:
        with self._write() as (session, run):
            execution = session.get(StepExecution, visit.execution_id)
            assert execution is not None
            execution.status, execution.finished_at = "succeeded", utc_now()
            if result:
                execution.decision = {"passed": "true", "failed": "false"}.get(
                    result.decision or ""
                )
                execution.validated_result_json = to_json(
                    result.validated_result if result.validated_result is not None else {}
                )
                attempt = session.get(StepAttempt, run.current_attempt_id)
                assert attempt is not None
                execution.raw_result_ref = attempt.result_artifact_id
            session.flush()
            context = self._context(session)
            edges = [
                e
                for e in self.snapshot["graph"]["edges"]
                if (e.get("from") or e.get("source") or e.get("from_node")) == node["id"]
            ]
            truth: str | None = None
            if node["type"] == "Condition":
                truth = evaluate_truth(ASTNode.from_json(node["expression"]), context).value
                edges = [e for e in edges if e.get("when") == truth]
                self._event(
                    session,
                    "condition.evaluated",
                    {
                        "result": {"true": True, "false": False}.get(truth),
                        "expression_ref": node["id"],
                    },
                    visit,
                )
            self._event(
                session,
                "node.left",
                {
                    "visit_index": visit.visit_index,
                    "cycle_id": visit.cycle_id,
                    "decision": {"true": True, "false": False}.get(execution.decision or ""),
                    "result_ref": execution.raw_result_ref,
                },
                visit,
            )
            if node["type"] == "End":
                self.runtime["next_node_id"] = None
                return self._state(session, run, "completed")
            if len(edges) != 1:
                raise ASTError("Ambiguous runtime transition")
            edge = edges[0]
            target = edge.get("to") or edge.get("target") or edge.get("to_node")
            loop = edge.get("loop")
            if loop:
                count = self.runtime["loop_counts"].get(loop["id"], 0)
                limit = (
                    "max_backward_transitions"
                    if self.runtime["backward_transitions"]
                    >= self._limits()["max_backward_transitions"]
                    else f"loop:{loop['id']}"
                    if count >= loop["max_iterations"]
                    else None
                )
                if limit:
                    reason = WaitingReason(
                        code="limit_exceeded",
                        details={"limit": limit, "node_id": node["id"]},
                        allowed_actions=[],
                    )
                    run.resume_target_json = to_json(
                        {
                            "action": "dispatch_next",
                            "blocked_edge": edge,
                            "blockers": ["limit_exceeded"],
                        }
                    )
                    return self._state(session, run, "waiting_input", reason)
            updates = apply_assignments(edge.get("assignments", {}), context)
            self.runtime["work"].update({key: value.raw for key, value in updates.items()})
            if loop:
                self.runtime["loop_counts"][loop["id"]] = (
                    self.runtime["loop_counts"].get(loop["id"], 0) + 1
                )
                self.runtime["backward_transitions"] += 1
                self.runtime["cycle_id"] += 1
                self.runtime["work"]["cycle_id"] = self.runtime["cycle_id"]
            self.runtime["next_node_id"] = target
            run.resume_target_json = to_json(
                {"action": "dispatch_next", "node_id": target, "blockers": []}
            )
            run.state_version += 1
            self._persist(run)
            assignments_ref = None
            if edge.get("assignments"):
                assignments_ref = self._artifact(
                    session,
                    visit,
                    "transition_assignments",
                    {
                        "assignments": edge["assignments"],
                        "values": {key: value.raw for key, value in updates.items()},
                    },
                ).id
            self._event(
                session,
                "transition.selected",
                {
                    "edge_id": edge.get("id") or edge.get("edge_id"),
                    "source_node_id": node["id"],
                    "target": target,
                    "backward": bool(loop),
                    "reason": truth or "single_edge",
                    "assignments_ref": assignments_ref,
                    "cycle_id": self.runtime["cycle_id"],
                },
                visit,
            )
        return None


def call_adapter(
    adapter: AgentAdapter | LLMAdapter, request: AgentAdapterRequest | LLMAdapterRequest
) -> AgentResult | LLMResult:
    return adapter.run(request)  # type: ignore[arg-type]


async def run_async(runner: Runner, run_id: str) -> RunnerResult:
    return await asyncio.to_thread(runner.execute, run_id)
