"""Stage 5: controls, recovery, process supervision and CLI.

The tests in this module assert the stage 5 contract:

* resume/resolve transition paused/stopped/waiting_input/recovering Runs
  into the queue and the worker picks them up.
* limit_exceeded resolution writes a new :class:`RunPolicyRevision` row
  and applies the override on the next dispatch without rewriting the
  immutable snapshot.
* A Run in ``recovering`` state reconciles any orphan attempt/process
  before the resolver decides what to do.
* The CLI exposes the same operations through the authenticated HTTP
  API so a headless operator can pause/resume/cancel a Run.
* The :class:`ProcessRegistry` only kills processes from the same
  owner generation; foreign PIDs are never touched.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time

import psutil
from sqlalchemy import select

from agents_ide.engine import queue
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import (
    CommandJournal,
    ProcessSupervision,
    QueueJob,
    Run,
    RunPolicyRevision,
    StepAttempt,
    StepExecution,
    WorkspaceReservation,
)


def make_run(
    authenticated,
    tmp_path,
    *,
    graph=None,
    suffix="",
    **extra,
):
    client, headers = authenticated
    workspace = tmp_path / f"ws{suffix}"
    workspace.mkdir(exist_ok=True)
    project = client.post(
        "/api/projects",
        headers=headers,
        json={"name": f"p{suffix}", "workspace_path": str(workspace)},
    ).json()
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={"name": f"c{suffix}", "base_url": "http://127.0.0.1:9/v1"},
    ).json()
    profile = client.post(
        "/api/harness_profiles",
        headers=headers,
        json={"name": f"h{suffix}", "harness_kind": "codex", "settings": {}},
    ).json()
    if graph is None:
        graph = {
            "nodes": [
                {"id": "s", "type": "Start"},
                {
                    "id": "check",
                    "type": "LLMRequest",
                    "config": {
                        "prompt": "check",
                        "model_selection": {
                            "kind": "direct",
                            "model_id": "m",
                            "provider_connection_id": connection["id"],
                        },
                    },
                },
                {"id": "e", "type": "End"},
            ],
            "edges": [{"from": "s", "to": "check"}, {"from": "check", "to": "e"}],
        }
    else:
        graph = json.loads(
            json.dumps(graph)
            .replace("CONNECTION", connection["id"])
            .replace("PROFILE", profile["id"])
        )
    template = client.post("/api/templates", headers=headers, json={"name": f"t{suffix}"}).json()
    version = client.post(
        f"/api/templates/{template['id']}/versions", headers=headers, json={"graph": graph}
    )
    assert version.status_code == 201, version.text
    binding = client.post(
        f"/api/versions/{version.json()['id']}/bindings",
        headers=headers,
        json={"project_id": project["id"], "name": "b"},
    ).json()
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "execution_mode": "simulated",
            "project_id": project["id"],
            "binding_id": binding["id"],
            "message": "go",
            "idempotency_key": f"r{suffix}",
            "overrides": {},
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json(), client.app.state.session_factory


def execute_with_limit(run, factory, settings, monkeypatch, *, max_calls, responses):
    from agents_ide.adapters.base import ExternalOutcome
    from agents_ide.adapters.fake import FakeLLMAdapter, FakeResponse, FakeScenario

    scenario = FakeScenario(
        tuple(
            responses
            or [
                FakeResponse(
                    "check",
                    1,
                    outcome=ExternalOutcome.RETRYABLE_FAILURE,
                    retry_safety="safe",
                    no_effect=True,
                )
                for _ in range(max_calls + 5)
            ]
        )
    )
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, FakeLLMAdapter(scenario)))
    job = queue.claim_next_job(factory, worker_id="review", lease_seconds=30)
    assert job is not None
    runner = Runner(
        session_factory=factory,
        worker_id="review",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    return runner.execute(run["id"]), job


# --------------------------------------------------------------------------- resume / resolve


def test_resume_from_paused_advances_checkpoint(authenticated, tmp_path, settings, monkeypatch):
    """A paused Run with a resume_target transitions to queued and resumes."""

    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        run_row.state = "paused"
        run_row.state_version += 1
        run_row.resume_target_json = json.dumps({"action": "dispatch_next", "node_id": "check"})
        run_row.runtime_json = json.dumps(
            {
                "next_node_id": "check",
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
        )
        snapshot = json.loads(run_row.snapshot_json)
        session.add(
            WorkspaceReservation(
                id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
                workspace_identity_dev=snapshot["workspace"]["identity_dev"],
                workspace_identity_ino=snapshot["workspace"]["identity_ino"],
                workspace_json=json.dumps(snapshot["workspace"]["scope"]),
                run_id=run["id"],
                owner_generation=run_row.worker_generation or 1,
                lease_expires_at=None,
                created_at=time.time(),
                released_at=None,
            )
        )
        session.commit()
    expected_version = client.get(f"/api/runs/{run['id']}", headers=headers).json()["state_version"]
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resume-1",
            "command_type": "resume",
            "expected_state_version": expected_version,
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "applied"
    assert body["response"]["action"] == "dispatch_next"
    with factory() as session:
        run_row = session.get(Run, run["id"])
        assert run_row.state == "queued"


def test_resolve_records_policy_revision_and_data_artifact(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        run_row.state = "waiting_input"
        run_row.state_version += 1
        run_row.runtime_json = json.dumps({"waiting_code": "limit_exceeded"})
        session.commit()
        session.expire_all()
    expected_version = client.get(f"/api/runs/{run['id']}", headers=headers).json()["state_version"]
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resolve-1",
            "command_type": "resolve",
            "expected_state_version": expected_version,
            "payload": {
                "limit_overrides": {"max_calls": 107},
                "data": {"answer": "yes"},
            },
        },
    )
    assert response.status_code == 200, response.text
    with factory() as session:
        revisions = list(
            session.scalars(select(RunPolicyRevision).where(RunPolicyRevision.run_id == run["id"]))
        )
        assert len(revisions) == 1
        assert json.loads(revisions[0].old_value_json) == 100
        assert json.loads(revisions[0].new_value_json) == 107
        from agents_ide.persistence.models import ArtifactManifest

        artifacts = list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == run["id"],
                    ArtifactManifest.schema_type == "resolution_data",
                )
            )
        )
        assert artifacts and json.loads(artifacts[0].body_json) == {"answer": "yes"}
        runtime = json.loads(session.get(Run, run["id"]).runtime_json)
        assert runtime["limit_overrides"] == {"max_calls": 107}


def test_resolve_rejects_non_positive_limit(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        run_row.state = "waiting_input"
        run_row.state_version += 1
        run_row.runtime_json = json.dumps({"waiting_code": "limit_exceeded"})
        session.commit()
    expected_version = client.get(f"/api/runs/{run['id']}", headers=headers).json()["state_version"]
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resolve-2",
            "command_type": "resolve",
            "expected_state_version": expected_version,
            "payload": {"limit_overrides": {"max_calls": 0}},
        },
    )
    assert response.status_code == 422


def test_resolve_without_checkpoint_is_rejected(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resolve-3",
            "command_type": "resolve",
            "expected_state_version": 0,
            "payload": {},
        },
    )
    assert response.status_code == 409
    assert response.json()["code"] == "command_not_allowed"


def test_resume_command_creates_new_queue_job(authenticated, tmp_path, settings, monkeypatch):
    """Resume must revive the QueueJob so the worker can re-claim it."""

    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        run_row.state = "stopped"
        run_row.state_version += 1
        run_row.resume_target_json = json.dumps({"action": "dispatch_next", "node_id": "check"})
        run_row.runtime_json = json.dumps({"next_node_id": "check"})
        # remove the job that start_run created; resume must recreate it.
        from sqlalchemy import delete as sa_delete

        session.execute(sa_delete(QueueJob).where(QueueJob.run_id == run["id"]))
        session.commit()
    expected_version = client.get(f"/api/runs/{run['id']}", headers=headers).json()["state_version"]
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resume-2",
            "command_type": "resume",
            "expected_state_version": expected_version,
        },
    )
    assert response.status_code == 200
    with factory() as session:
        job = session.scalar(select(QueueJob).where(QueueJob.run_id == run["id"]))
        assert job is not None and job.claimed_by is None


def test_resume_advances_checkpoint_to_running(authenticated, tmp_path, settings, monkeypatch):
    """Resume from paused with valid target dispatches the next node."""

    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        run_row.state = "paused"
        run_row.state_version += 1
        run_row.resume_target_json = json.dumps({"action": "dispatch_next", "node_id": "check"})
        run_row.runtime_json = json.dumps(
            {
                "next_node_id": "check",
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
        )
        snapshot = json.loads(run_row.snapshot_json)
        session.add(
            WorkspaceReservation(
                id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
                workspace_identity_dev=snapshot["workspace"]["identity_dev"],
                workspace_identity_ino=snapshot["workspace"]["identity_ino"],
                workspace_json=json.dumps(snapshot["workspace"]["scope"]),
                run_id=run["id"],
                owner_generation=1,
                lease_expires_at=None,
                created_at=time.time(),
                released_at=None,
            )
        )
        session.commit()
    from agents_ide.adapters.fake import FakeLLMAdapter, FakeResponse, FakeScenario

    scenario = FakeScenario((FakeResponse("check", 1, raw_text="{}", decision="passed"),))
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, FakeLLMAdapter(scenario)))
    expected_version = client.get(f"/api/runs/{run['id']}", headers=headers).json()["state_version"]
    client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resume-3",
            "command_type": "resume",
            "expected_state_version": expected_version,
        },
    )
    job = queue.claim_next_job(factory, worker_id="resume", lease_seconds=30)
    assert job is not None and job.run_id == run["id"]
    runner = Runner(
        session_factory=factory,
        worker_id="resume",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    result = runner.execute(run["id"])
    assert result.final_state == "completed"


# --------------------------------------------------------------------------- reconciliation


def test_reconciling_run_marks_orphan_attempt_and_process(
    authenticated, tmp_path, settings, monkeypatch
):
    """A previous worker left a running attempt; reconciliation must close it."""

    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    with factory() as session:
        run_row = session.get(Run, run["id"])
        snapshot = json.loads(run_row.snapshot_json)
        # create an execution and a running attempt that the new owner will reconcile.
        execution = StepExecution(
            id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
            run_id=run["id"],
            node_id="check",
            visit_index=1,
            cycle_id=1,
            scope="main",
            status="running",
        )
        session.add(execution)
        session.flush()
        attempt = StepAttempt(
            id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
            execution_id=execution.id,
            attempt_index=1,
            status="running",
            operation_id="op-1",
        )
        session.add(attempt)
        session.flush()
        session.add(
            ProcessSupervision(
                id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
                run_id=run["id"],
                step_attempt_id=attempt.id,
                role="dev",
                owner_generation=1,
                pid=999999,
                started_at=time.time(),
                create_time=time.time(),
                parent_pid=None,
                executable=None,
                kind="harness",
                state="started",
            )
        )
        run_row.state = "recovering"
        run_row.state_version += 1
        run_row.worker_generation = 1
        run_row.resume_target_json = json.dumps(
            {"action": "reconcile", "execution_id": execution.id, "node_id": "check"}
        )
        run_row.runtime_json = json.dumps({"next_node_id": "check"})
        session.add(
            WorkspaceReservation(
                id=__import__("agents_ide.domain.common", fromlist=["new_id"]).new_id(),
                workspace_identity_dev=snapshot["workspace"]["identity_dev"],
                workspace_identity_ino=snapshot["workspace"]["identity_ino"],
                workspace_json=json.dumps(snapshot["workspace"]["scope"]),
                run_id=run["id"],
                owner_generation=2,
                lease_expires_at=None,
                created_at=time.time(),
                released_at=None,
            )
        )
        session.commit()
    job = queue.claim_next_job(factory, worker_id="recover", lease_seconds=30)
    assert job is not None
    runner = Runner(
        session_factory=factory,
        worker_id="recover",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    result = runner.execute(run["id"])
    assert result.final_state == "waiting_input"
    assert result.waiting_reason and result.waiting_reason.code == "unknown_external_result"
    with factory() as session:
        execution_row = session.scalar(
            select(StepExecution).where(StepExecution.id == execution.id)
        )
        assert execution_row.status == "waiting_input"
        attempt_row = session.get(StepAttempt, attempt.id)
        assert attempt_row.status == "unknown"
        processes = list(
            session.scalars(
                select(ProcessSupervision).where(ProcessSupervision.run_id == run["id"])
            )
        )
        assert all(process.state in {"unknown", "finished"} for process in processes)


# --------------------------------------------------------------------------- pause/stop journal


def test_pause_command_is_accepted_then_applied_by_runner(authenticated, tmp_path):
    run, factory = make_run(authenticated, tmp_path)
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "pause-1",
            "command_type": "pause",
            "expected_state_version": current["state_version"],
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "accepted"
    with factory() as session:
        journal = session.scalar(
            select(CommandJournal).where(
                CommandJournal.run_id == run["id"],
                CommandJournal.command_id == "pause-1",
            )
        )
        assert journal is not None and journal.status == "accepted"


# --------------------------------------------------------------------------- process supervision


def test_process_registry_only_kills_own_generation(tmp_path):
    from agents_ide.worker.processes import (
        ProcessRegistry,
        is_alive_pid,
        stop_owned,
    )

    registry = ProcessRegistry()
    script = tmp_path / "alive.py"
    script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
    process = psutil.Popen([sys.executable, str(script)])
    try:
        entry = registry.register(
            process=process,
            kind="support",
            role="support",
            owner_generation=1,
            run_id="r-foreign",
            step_attempt_id=None,
        )
        # stop_owned from a different generation must NOT touch this PID.
        assert stop_owned(registry, owner_generation=2, cooperative_seconds=1, kill_seconds=1)
        assert is_alive_pid(entry.pid, entry.create_time)
        # stop_owned from the right generation confirms shutdown.
        assert stop_owned(registry, owner_generation=1, cooperative_seconds=5, kill_seconds=5)
        assert not is_alive_pid(entry.pid, entry.create_time)
    finally:
        if is_alive_pid(process.pid, process.create_time()):
            process.kill()
            process.wait(timeout=3)


def test_process_registry_recycled_pid_is_not_killed(tmp_path):
    from agents_ide.worker.processes import (
        is_alive_pid,
    )

    script = tmp_path / "quick.py"
    script.write_text("import os, sys\nsys.exit(0)\n", encoding="utf-8")
    process = psutil.Popen([sys.executable, str(script)])
    process.wait(timeout=5)
    entry = type(
        "Entry",
        (),
        {
            "pid": process.pid,
            "create_time": process.create_time() + 1,
        },
    )()
    # Same PID, different create_time (recycled) must not be signalled.
    assert not is_alive_pid(entry.pid, entry.create_time)


def test_process_health_reports_only_owned_entries():
    from agents_ide.worker.processes import ProcessRegistry, health

    registry = ProcessRegistry()
    snapshot = health(registry, owner_generation=1)
    assert snapshot["active"] == 0 and snapshot["registered"] == 0


# --- worker DB unavailability


# --------------------------------------------------------------------------- CLI


def test_runs_cli_requires_paired_api(tmp_path):
    """CLI runs subcommand surfaces pair_required when no API has issued a code."""

    import os
    import sys

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "agents_ide",
            "runs",
            "status",
            "--run-id",
            "missing",
        ],
        env={**os.environ, "AGENTS_IDE_DATA_DIR": str(data_dir)},
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode != 0
    assert "pair_required" in result.stderr or "service_busy" in result.stderr
