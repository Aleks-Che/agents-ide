"""Native Council attempts share process launch/kill mechanics with Run.

Every Windows process is registered while suspended, before it can call a
model. The ledger is fenced by the planning lease and is checked before retry.
Only the selected attempt's artifact directory is exposed to Codex tools.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Callable
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import Any

import psutil
from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.adapters.base import AgentAdapter, AgentAdapterRequest, AgentResult
from agents_ide.adapters.codex import CodexAdapter
from agents_ide.adapters.opencode import OpenCodeAdapter
from agents_ide.domain.common import to_json
from agents_ide.engine.artifacts import sanitize
from agents_ide.engine.codex_runtime import CodexRuntime
from agents_ide.engine.opencode_runtime import OpenCodeRuntime
from agents_ide.engine.stream_buffer import ProgressBatch, StreamEventBuffer
from agents_ide.errors import AppError
from agents_ide.persistence.models import PlanningAttempt, PlanningJob
from agents_ide.services.transactions import begin_write
from agents_ide.worker.processes import (
    ProcessGroup,
    ProcessRegistryEntry,
    ProcessSupervisor,
    capture_tree,
    interrupt,
    process_state,
    wait_descendants_stopped,
)


def processes_stopped(session: Session, job_id: str) -> bool:
    for attempt in session.scalars(select(PlanningAttempt).where(PlanningAttempt.job_id == job_id)):
        runtime = json.loads(attempt.runtime_json or "{}")
        for process in runtime.get("processes", []):
            if process_state(process["pid"], process["create_time"]) != "dead":
                return False
            if any(
                process_state(int(pid), created) != "dead"
                for pid, created in process.get("pids", {}).items()
            ):
                return False
            if not process.get("job_owned") and not process.get("tree_verified"):
                return False
    return True


class PlanningSupervisor(ProcessSupervisor):
    def _attempt(self, session: Session, attempt_id: str | None) -> PlanningAttempt:
        job = session.get(PlanningJob, self.run_id)
        attempt = session.get(PlanningAttempt, attempt_id)
        if (
            job is None
            or job.generation != self.generation
            or job.lease_owner != self.worker_id
            or job.lease_expires_at is None
            or attempt is None
            or attempt.job_id != job.id
            or attempt.generation != self.generation
            or attempt.outcome != "running"
        ):
            raise AppError("planning_lease_lost", "Planning process ownership lost", 409)
        return attempt

    def _register_callback(
        self,
        group: ProcessGroup,
        holder: dict[str, ProcessRegistryEntry],
        *,
        role: str,
        kind: str,
        attempt_id: str | None,
        transport: str,
        port: int | None,
    ) -> Callable[[psutil.Process], None]:
        def register(process: psutil.Process) -> None:
            entry = self.registry.register(
                process=process,
                kind=kind,
                role=role,
                owner_generation=self.generation,
                run_id=self.run_id,
                step_attempt_id=attempt_id,
                parent_pid=os.getpid(),
            )
            entry.group = group
            holder["entry"] = entry
            with self.factory() as session:
                begin_write(session)
                attempt = self._attempt(session, attempt_id)
                job = session.get(PlanningJob, self.run_id)
                assert job
                if job.state not in {"drafting", "merging"}:
                    raise AppError("dispatch_blocked", "Planning was stopped", 409)
                runtime = json.loads(attempt.runtime_json or "{}")
                runtime.setdefault("processes", []).append(
                    {
                        "pid": entry.pid,
                        "create_time": entry.create_time,
                        "parent_pid": entry.parent_pid,
                        "role": role,
                        "transport": transport,
                        "port": port,
                        "started_at": entry.started_at,
                        "state": "started",
                        "job_owned": sys.platform == "win32",
                        "pids": {},
                    }
                )
                attempt.runtime_json = to_json(runtime)
                session.commit()

        return register

    def refresh_health(self, *, external_event: bool = False) -> None:
        for entry in self.registry.by_run(self.run_id):
            if entry.owner_generation != self.generation:
                continue
            verified = capture_tree(entry)
            stopped = wait_descendants_stopped(entry, 0)
            with self.factory() as session:
                begin_write(session)
                attempt = self._attempt(session, entry.step_attempt_id)
                runtime = json.loads(attempt.runtime_json or "{}")
                for process in runtime.get("processes", []):
                    if (process["pid"], process["create_time"]) == (entry.pid, entry.create_time):
                        process.update(
                            pids=entry.descendants,
                            tree_verified=verified,
                            state="finished" if stopped else "running",
                        )
                attempt.runtime_json = to_json(runtime)
                session.commit()
            if stopped:
                self.registry.mark_finished(
                    entry.pid, self.generation, create_time=entry.create_time
                )

    def stop(self, *, cooperative_seconds: float = 0, kill_seconds: float = 5) -> bool:
        stopped = True
        for entry in self.registry.by_run(self.run_id):
            if entry.owner_generation == self.generation:
                stopped = (
                    interrupt(
                        entry, cooperative_seconds=cooperative_seconds, kill_seconds=kill_seconds
                    )
                    and stopped
                )
        with suppress(AppError):
            self.refresh_health()
        return stopped

    def emit(self, attempt_id: str, kind: str, payload: dict[str, Any]) -> None:
        self.emit_batch(attempt_id, [(kind, payload)])

    def emit_batch(self, attempt_id: str, batch: ProgressBatch) -> None:
        from agents_ide.operations.storage import check_capacity
        from agents_ide.services.planning import _record_event

        with self.factory() as session:
            begin_write(session)
            attempt = self._attempt(session, attempt_id)
            runtime = json.loads(attempt.runtime_json or "{}")
            for kind, payload in batch:
                body = sanitize(payload)
                encoded = to_json(body)
                if kind in {"agent.session_created", "agent.session_resumed", "agent.turn_started"}:
                    runtime.update({k: body[k] for k in ("session_id", "turn_id") if body.get(k)})
                size = len(encoded.encode("utf-8"))
                if (
                    runtime.get("event_bytes", 0) + size <= 4 * 1024 * 1024
                    and runtime.get("event_count", 0) < 2000
                ):
                    check_capacity(session, extra=size)
                    _record_event(
                        session,
                        self.run_id,
                        member_id=attempt.member_id,
                        event_type="planning." + kind,
                        payload={"attempt_id": attempt_id, **body},
                    )
                    runtime["event_bytes"] = runtime.get("event_bytes", 0) + size
                    runtime["event_count"] = runtime.get("event_count", 0) + 1
                elif not runtime.get("events_truncated"):
                    runtime["events_truncated"] = True
                    _record_event(
                        session,
                        self.run_id,
                        member_id=attempt.member_id,
                        event_type="planning.native_events_truncated",
                        payload={"attempt_id": attempt_id},
                    )
            attempt.runtime_json = to_json(runtime)
            session.commit()


def run_native(
    candidate: dict[str, Any],
    request: AgentAdapterRequest,
    supervisor: PlanningSupervisor,
    attempt_id: str,
) -> AgentResult:
    from agents_ide.operations.storage import settings_for
    from agents_ide.security.filesystem import atomic_write, private_directory
    from agents_ide.security.native_credentials import fingerprint

    with supervisor.factory() as session:
        settings = settings_for(session)
    parent = settings.data_dir / "temp" / "council"
    private_directory(parent)
    with tempfile.TemporaryDirectory(prefix=attempt_id + "-", dir=parent) as directory:
        root = Path(directory)
        private_directory(root)
        atomic_write(root / "request.md", request.prompt.encode("utf-8"))
        atomic_write(root / "context.json", to_json(request.context_package).encode("utf-8"))
        artifact_paths = ["request.md", "context.json"]
        for index, draft in enumerate(request.context_package.get("drafts", [])):
            # IDs are data, never path components; the scratch directory includes attempt ID.
            name = f"draft-{index + 1}.json"
            atomic_write(root / name, to_json(draft).encode("utf-8"))
            artifact_paths.append(name)
        profile = candidate["harness"]
        kind = profile["harness_kind"]
        expected_fingerprint = profile.get("native_fingerprint")

        def check_access() -> None:
            if request.check_owned:
                request.check_owned()
            if (
                not expected_fingerprint
                or fingerprint(kind, profile["executable_path"]) != expected_fingerprint
            ):
                raise AppError(
                    "native_credentials_changed", "Native access changed; retry explicitly", 409
                )

        check_access()
        kwargs: dict[str, Any] = dict(
            executable=profile["executable_path"],
            workspace_path=root,
            supervisor=supervisor,
            attempt_id=attempt_id,
            check_owned=request.check_owned,
            stop_event=request.stop_event,
        )
        runtime: CodexRuntime | OpenCodeRuntime | None = None
        adapter: AgentAdapter
        progress = StreamEventBuffer(
            lambda batch: supervisor.emit_batch(attempt_id, batch), record_tool_history=False
        )

        def check_progress() -> None:
            if request.check_owned:
                request.check_owned()
            progress.flush_due()

        try:
            if kind == "codex":
                runtime = CodexRuntime.start(**kwargs, isolated_read=True)
                adapter = CodexAdapter(stream=runtime.stream, session=runtime.session())
            elif kind == "opencode":
                runtime = OpenCodeRuntime.start(**kwargs)
                adapter = OpenCodeAdapter(session=runtime.session())
            else:
                raise AppError("harness_unavailable", "Unknown planning harness", 422)
            check_access()
            native_prompt = request.prompt
            if kind == "codex":
                native_prompt += (
                    "\nThe supplied packet is also available as read-only artifacts in this "
                    "isolated working directory: " + ", ".join(artifact_paths) + ". "
                    "Only these artifacts are task context; do not access other paths."
                )
            return adapter.run(
                replace(
                    request,
                    prompt=native_prompt,
                    context_package={
                        key: value
                        for key, value in request.context_package.items()
                        if key not in {"context", "drafts"}
                    },
                    workspace_path=str(root),
                    emit_event=progress.emit,
                    record_tool_history=False,
                    check_owned=check_progress,
                )
            )
        finally:
            try:
                progress.close()
            finally:
                try:
                    if runtime:
                        runtime.close()
                finally:
                    if not supervisor.stop():
                        raise AppError(
                            "process_not_responding",
                            "Planning process termination unconfirmed",
                            409,
                        )
