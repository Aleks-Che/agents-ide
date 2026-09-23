"""Stage 5 review: drive actual commands/runner/processes through failure boundaries."""

import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import psutil
import pytest
from sqlalchemy import select
from test_stage4_review import make_run

from agents_ide.adapters.base import AdapterError, ExternalOutcome, LLMResult
from agents_ide.engine import queue
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    CommandJournal,
    ProcessSupervision,
    QueueJob,
    Run,
    RunPolicyRevision,
    StepAttempt,
    StepExecution,
    WorkspaceReservation,
)
from agents_ide.worker.processes import ProcessRegistry, ProcessSupervisor, process_state


def command(authenticated, run, kind, payload=None, command_id=None):
    client, headers = authenticated
    row = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": command_id or f"{kind}-{row['state_version']}",
            "command_type": kind,
            "expected_state_version": row["state_version"],
            "payload": payload or {},
        },
    )
    return response


def runner_for(run, factory, settings, owner="review5"):
    job = queue.claim_next_job(factory, worker_id=owner, lease_seconds=30)
    assert job and job.run_id == run["id"]
    return Runner(
        session_factory=factory,
        worker_id=owner,
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    ), job


def run_now(run, factory, settings):
    runner, _ = runner_for(run, factory, settings)
    return runner.execute(run["id"])


def wait_for(factory, predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with factory() as session:
            if result := predicate(session):
                return result
        time.sleep(0.02)
    raise AssertionError("Timed out waiting for persisted state")


@pytest.mark.parametrize(
    "kind,state", [("pause", "paused"), ("stop", "stopped"), ("cancel", "cancelled")]
)
def test_control_during_live_call(authenticated, tmp_path, settings, kind, state):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={
            "responses": [
                {"node_id": "check", "delay_seconds": 1.2, "files": {"result.txt": "effect"}}
            ]
        },
    )
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_now, run, factory, settings)
        wait_for(
            factory, lambda s: s.scalar(select(StepAttempt).where(StepAttempt.status == "running"))
        )
        response = command(authenticated, run, kind)
        assert response.status_code == 200, response.text
        assert future.result(timeout=8).final_state == state
    with factory() as session:
        attempt = session.scalar(select(StepAttempt))
        if kind == "pause":
            assert attempt.status == "succeeded"
        else:
            assert attempt.status == "interrupted"
        reservation = session.scalar(select(WorkspaceReservation))
        assert (reservation.released_at is not None) == (kind == "cancel")
    file = settings.data_dir / "simulated" / run["id"] / "result.txt"
    assert file.exists() == (kind == "pause")
    if kind != "cancel":
        assert command(authenticated, run, "resume").status_code == 200
        assert run_now(run, factory, settings).final_state == "completed"
        with factory() as session:
            steps = list(
                session.scalars(select(StepExecution).where(StepExecution.node_id == "check"))
            )
            assert len(steps) == 1 and steps[0].attempt_count == (1 if kind == "pause" else 2)


@pytest.mark.parametrize("kind", ["stop", "cancel"])
def test_success_arriving_after_control_is_preserved_without_reexecution(
    authenticated, tmp_path, settings, monkeypatch, kind
):
    run, factory = make_run(authenticated, tmp_path)

    class Adapter:
        def run(self, request):
            response = command(authenticated, run, kind)
            assert response.status_code == 200
            return LLMResult(
                ExternalOutcome.SUCCEEDED, '{"verdict":"passed"}', {"verdict": "passed"}, "passed"
            )

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).final_state == (
        "stopped" if kind == "stop" else "cancelled"
    )
    with factory() as session:
        step = session.scalar(select(StepExecution).where(StepExecution.node_id == "check"))
        assert step.status == "succeeded" and step.attempt_count == 1
        assert json.loads(session.get(Run, run["id"]).runtime_json)["next_node_id"] == "e"
    if kind == "stop":
        assert command(authenticated, run, "resume").status_code == 200
        assert run_now(run, factory, settings).final_state == "completed"


def test_resume_keeps_candidate_retry_and_visit(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path, group=True)
    calls = []

    class Adapter:
        def run(self, request):
            calls.append(request.model_id)
            if request.model_id == "alpha":
                return LLMResult(
                    ExternalOutcome.UNAVAILABLE,
                    "",
                    None,
                    None,
                    error=AdapterError("unavailable", "unavailable", "safe"),
                    no_effect=True,
                )
            if len(calls) == 2:
                assert command(authenticated, run, "pause").status_code == 200
                return LLMResult(
                    ExternalOutcome.RETRYABLE_FAILURE,
                    "",
                    None,
                    None,
                    error=AdapterError("busy", "busy", "safe"),
                    no_effect=True,
                )
            return LLMResult(ExternalOutcome.SUCCEEDED, "{}", {}, "passed")

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).final_state == "paused"
    with factory() as session:
        before = json.loads(session.get(Run, run["id"]).runtime_json)
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    assert calls == ["alpha", "beta", "beta"]
    with factory() as session:
        step = session.scalar(select(StepExecution).where(StepExecution.node_id == "check"))
        assert step.visit_index == 1 and step.attempt_count == 3
        after = json.loads(session.get(Run, run["id"]).runtime_json)
        assert after["external_calls"] == before["external_calls"] + 1
        assert after["visits"] == before["visits"] + 1  # only End is new


@pytest.mark.parametrize("kind", ["resume", "pause", "stop", "cancel"])
def test_unknown_cannot_be_bypassed(authenticated, tmp_path, settings, kind):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    assert run_now(run, factory, settings).final_state == "waiting_input"
    response = command(authenticated, run, kind)
    assert response.status_code == (409 if kind == "resume" else 200)
    with factory() as session:
        state = session.get(Run, run["id"]).state
        assert state == "cancelled" if kind == "cancel" else state not in {"queued", "cancelled"}
        released = session.scalar(select(WorkspaceReservation)).released_at
        assert released is not None if kind == "cancel" else released is None
        assert session.scalar(select(StepAttempt)).status == "unknown"


def test_expired_job_reclaimed_for_reconciliation_without_releasing_workspace(
    authenticated, tmp_path, settings
):
    run, factory = make_run(authenticated, tmp_path)
    old, job = runner_for(run, factory, settings, "old")
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state = "running"
        saved = session.get(QueueJob, job.job_id)
        saved.lease_expires_at = time.time() - 1
        saved.owner_pid, saved.owner_create_time = None, None
        session.commit()
    recovery, claimed = runner_for(run, factory, settings, "new")
    assert claimed.generation > job.generation
    result = recovery.execute(run["id"])
    assert result.final_state == "waiting_input"
    with factory() as session:
        reservations = list(session.scalars(select(WorkspaceReservation)))
        assert len(reservations) == 1 and reservations[0].released_at is None
    with pytest.raises(AppError):
        queue.refresh_lease(
            factory,
            job_id=job.job_id,
            worker_id="old",
            expected_generation=job.generation,
            lease_seconds=30,
        )


def test_limits_resolve_is_effective_and_audited(authenticated, tmp_path, settings):
    settings.enforce_execution_limits = True
    run, factory = make_run(
        authenticated,
        tmp_path,
        overrides={"limit_overrides": {"max_calls": 1}},
        fake_scenario={
            "responses": [
                {
                    "node_id": "check",
                    "outcome": "retryable_failure",
                    "retry_safety": "safe",
                    "no_effect": True,
                }
            ]
        },
    )
    assert run_now(run, factory, settings).waiting_reason.code == "limit_exceeded"
    assert command(authenticated, run, "resume").status_code == 409
    for value in (2, 3):
        response = command(
            authenticated,
            run,
            "resolve",
            {"limit_overrides": {"max_calls": value}, "reason": "review"},
        )
        assert response.status_code == 200, response.text
    with factory() as session:
        revisions = list(
            session.scalars(select(RunPolicyRevision).order_by(RunPolicyRevision.created_at))
        )
        assert [
            (json.loads(r.old_value_json), json.loads(r.new_value_json)) for r in revisions
        ] == [(1, 2), (2, 3)]
        row = session.get(Run, run["id"])
        assert (
            json.loads(row.snapshot_json)["resolved_settings"]["limit_overrides"]["max_calls"] == 1
        )
        assert row.state == "waiting_input"
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        assert (
            session.scalar(
                select(StepExecution).where(StepExecution.node_id == "check")
            ).attempt_count
            == 2
        )


@pytest.mark.parametrize(
    "overrides",
    [
        {"max_calls": "x"},
        {"max_calls": True},
        {"max_calls": 0},
        {"bogus": 5},
        {"max_calls": 1.5},
        {"max_calls": {}},
        {"max_calls": []},
    ],
)
def test_invalid_limit_payload_is_422_without_revision(
    authenticated, tmp_path, settings, overrides
):
    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state = "waiting_input"
        row.runtime_json = '{"waiting_code":"limit_exceeded"}'
        session.commit()
    response = command(authenticated, run, "resolve", {"limit_overrides": overrides})
    assert response.status_code == 422, response.text
    with factory() as session:
        assert session.scalar(select(RunPolicyRevision)) is None


def test_resolution_artifact_redacts_secrets(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    run_now(run, factory, settings)
    response = command(authenticated, run, "resolve", {"data": {"api_key": "never-store-this"}})
    assert response.status_code == 200
    assert "never-store-this" not in response.text
    with factory() as session:
        artifact = session.scalar(
            select(ArtifactManifest).where(ArtifactManifest.schema_type == "resolution_data")
        )
        journal = session.scalar(
            select(CommandJournal).where(CommandJournal.command_type == "resolve")
        )
        assert artifact.source_ref and artifact.step_attempt_id
        assert "never-store-this" not in artifact.body_json + journal.payload_json


def test_attempt_heartbeat_advances_without_output(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "delay_seconds": 1.5}]},
    )
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_now, run, factory, settings)
        attempt = wait_for(factory, lambda s: s.scalar(select(StepAttempt)))
        wait_for(
            factory,
            lambda s: s.get(StepAttempt, attempt.id).heartbeat_at > attempt.heartbeat_at + 0.2,
        )
        assert future.result(timeout=5).final_state == "completed"


def test_supervisor_persists_before_execution_and_kills_surviving_child(
    authenticated, tmp_path, settings
):
    run, factory = make_run(authenticated, tmp_path)
    _, job = runner_for(run, factory, settings)
    registry = ProcessRegistry()
    supervisor = ProcessSupervisor(factory, registry, run["id"], "review5", job.generation)
    marker = tmp_path / "child.json"
    startup_log = tmp_path / "child-startup.log"
    script = tmp_path / "parent.py"
    script.write_text(
        f"import sys\nsys.stderr=open({str(startup_log)!r},'w',buffering=1)\n"
        "import subprocess,sys,time,json,psutil,sqlite3,os\nfrom pathlib import Path\n"
        "from agents_ide.security.filesystem import atomic_write\n"
        f"db=sqlite3.connect({str(settings.database_path)!r})\n"
        "ancestors={os.getpid(),*(p.pid for p in psutil.Process().parents())}\n"
        "assert any(pid in ancestors for (pid,) in "
        "db.execute('SELECT pid FROM process_supervision'))\n"
        "db.close()\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"atomic_write(Path({str(marker)!r}),json.dumps([p.pid,psutil.Process(p.pid).create_time()]).encode())\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    entry = supervisor.start([sys.executable, str(script)], tmp_path, dict(os.environ))
    child = None
    try:
        deadline = time.monotonic() + 5
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists(), startup_log.read_text(encoding="utf-8")
        child = json.loads(marker.read_text())
        supervisor.refresh_health()
        with factory() as session:
            row = session.scalar(select(ProcessSupervision))
            assert row.pid == entry.pid and row.last_health_ok is not None
            assert str(child[0]) in json.loads(row.tree_json)["pids"]
        psutil.Process(entry.pid).kill()  # Child survives its parent until Job cleanup.
        assert supervisor.stop(cooperative_seconds=0.1, kill_seconds=3)
        assert process_state(*child) == "dead"
    finally:
        if entry.group:
            entry.group.close()
        if child and process_state(*child) == "alive":
            psutil.Process(child[0]).kill()


def test_process_access_failure_is_not_proof_of_exit(monkeypatch):
    from agents_ide.worker import windows_jobs

    monkeypatch.setattr(windows_jobs, "process_exited", lambda _: False)
    def denied(_):
        raise psutil.AccessDenied()

    monkeypatch.setattr(psutil, "Process", denied)
    assert process_state(123, 1.0) == "unknown"


def test_dispatch_failure_stops_owned_processes(authenticated, tmp_path, settings, monkeypatch):
    from sqlalchemy.exc import OperationalError

    from agents_ide.worker.main import dispatch_once

    run, _ = make_run(authenticated, tmp_path)
    entries = []

    def fail(self, run_id):
        supervisor = ProcessSupervisor(
            self.session_factory, self.registry, run_id, self.worker_id, self.generation
        )
        entries.append(
            supervisor.start(
                [sys.executable, "-c", "import time; time.sleep(60)"], tmp_path, dict(os.environ)
            )
        )
        raise OperationalError("INSERT result", {}, Exception("fault injection"))

    monkeypatch.setattr(Runner, "execute", fail)
    with pytest.raises(OperationalError):
        dispatch_once(settings, "save-failure")
    assert entries and entries[0].run_id == run["id"]
    assert process_state(entries[0].pid, entries[0].create_time) == "dead"


def test_unconfirmed_stop_keeps_reservation_and_late_result_needs_resolution(
    authenticated, tmp_path, settings, monkeypatch
):
    import agents_ide.engine.runner as engine

    monkeypatch.setattr(engine, "INTERRUPT_SECONDS", 0.05)
    monkeypatch.setattr(engine, "KILL_SECONDS", 0.05)
    run, factory = make_run(authenticated, tmp_path)
    release = threading.Event()

    class Adapter:
        def run(self, request):
            release.wait(5)  # Deliberately ignores request.stop_event.
            workspace = settings.data_dir / "simulated" / run["id"]
            workspace.mkdir(parents=True, exist_ok=True)
            (workspace / "late.txt").write_text("confirmed late effect")
            return LLMResult(ExternalOutcome.SUCCEEDED, "{}", {}, "passed")

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(run_now, run, factory, settings)
            attempt = wait_for(factory, lambda s: s.scalar(select(StepAttempt)))
            assert command(authenticated, run, "stop").status_code == 200
            result = future.result(timeout=3)
            assert result.waiting_reason.code == "process_not_responding"
        assert command(authenticated, run, "resume").status_code == 409
        with factory() as session:
            assert session.scalar(select(WorkspaceReservation)).released_at is None
            assert session.get(StepAttempt, attempt.id).finished_at is None
        release.set()
        wait_for(
            factory,
            lambda s: json.loads(s.get(Run, run["id"]).runtime_json).get("late_result_refs"),
        )
        assert command(authenticated, run, "resume").status_code == 409
        response = command(
            authenticated,
            run,
            "resolve",
            {
                "reconciliation": {
                    "attempt_id": attempt.id,
                    "action": "accept_result",
                    "evidence": "Reviewed the confirmed late result.",
                }
            },
        )
        assert response.status_code == 200, response.text
        assert command(authenticated, run, "resume").status_code == 200
        assert run_now(run, factory, settings).final_state == "stopped"  # retained stop intent
        assert command(authenticated, run, "resume").status_code == 200
        assert run_now(run, factory, settings).final_state == "completed"
        with factory() as session:
            assert len(list(session.scalars(select(StepAttempt)))) == 1
    finally:
        release.set()


def test_pause_cannot_erase_limit_blocker(authenticated, tmp_path, settings):
    settings.enforce_execution_limits = True
    run, factory = make_run(
        authenticated,
        tmp_path,
        overrides={"limit_overrides": {"max_calls": 1}},
        fake_scenario={
            "responses": [
                {
                    "node_id": "check",
                    "outcome": "retryable_failure",
                    "retry_safety": "safe",
                    "no_effect": True,
                }
            ]
        },
    )
    assert run_now(run, factory, settings).waiting_reason.code == "limit_exceeded"
    assert command(authenticated, run, "pause").status_code == 200
    assert command(authenticated, run, "resume").status_code == 409
    with factory() as session:
        target = json.loads(session.get(Run, run["id"]).resume_target_json)
        assert "limit_exceeded" in target["blockers"]
    assert (
        command(authenticated, run, "resolve", {"limit_overrides": {"max_calls": 2}}).status_code
        == 200
    )
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"


def test_resume_detects_private_workspace_changes(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path)

    class Adapter:
        def run(self, request):
            command(authenticated, run, "pause")
            return LLMResult(ExternalOutcome.SUCCEEDED, "{}", {}, "passed")

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).final_state == "paused"
    workspace = settings.data_dir / "simulated" / run["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / "external.txt").write_text("external modification")
    assert command(authenticated, run, "resume").status_code == 200
    result = run_now(run, factory, settings)
    assert result.waiting_reason.code == "external_change_detected"
    with factory() as session:
        assert session.scalar(select(WorkspaceReservation)).released_at is None


@pytest.mark.parametrize(
    "outcome,expected,code",
    [
        ("invalid_format", "waiting_input", "invalid_response_format"),
        ("permission_denied", "waiting_input", "permission_required"),
        ("confirmed_failure", "failed", None),
    ],
)
def test_crash_after_failed_result_does_not_retry(
    authenticated, tmp_path, settings, outcome, expected, code
):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={
            "responses": [
                {"node_id": "check", "outcome": outcome, "no_effect": True, "retry_safety": "safe"}
            ]
        },
    )
    crash = tmp_path / "failed_worker.py"
    crash.write_text(
        "import os\nfrom agents_ide.engine.runner import Runner\n"
        "from agents_ide.worker.main import run_worker\nfrom agents_ide.config import Settings\n"
        "original=Runner._save_attempt\ndef crash(self,*args):\n"
        " original(self,*args)\n os._exit(73)\n"
        "Runner._save_attempt=crash\nrun_worker(Settings())\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(crash)],
        env={**os.environ, "AGENTS_IDE_DATA_DIR": str(settings.data_dir)},
        capture_output=True,
        timeout=12,
    )
    assert process.returncode == 73
    with factory() as session:
        session.scalar(select(QueueJob)).lease_expires_at = time.time() - 1
        session.commit()
    result = run_now(run, factory, settings)
    assert result.final_state == expected
    assert (result.waiting_reason.code if result.waiting_reason else None) == code
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1


@pytest.mark.parametrize(
    "phase", ["after_claim", "before_call", "after_result", "after_transition"]
)
def test_real_worker_crash_recovers_without_duplicate_call(
    authenticated, tmp_path, settings, phase
):
    run, factory = make_run(authenticated, tmp_path)
    crash = tmp_path / "crash_worker.py"
    crash.write_text(
        "import os\nfrom agents_ide.engine.runner import Runner\n"
        "from agents_ide.worker.main import run_worker\nfrom agents_ide.config import Settings\n"
        + (
            "Runner.execute=lambda *a: os._exit(73)\n"
            if phase == "after_claim"
            else "import agents_ide.engine.runner as m\nm.call_adapter=lambda *a: os._exit(73)\n"
            if phase == "before_call"
            else "original=Runner._save_attempt\ndef crash(self,*args,**kw):\n"
            " original(self,*args,**kw)\n os._exit(73)\nRunner._save_attempt=crash\n"
            if phase == "after_result"
            else "original=Runner._finish_visit\ndef crash(self,node,*args,**kw):\n"
            " result=original(self,node,*args,**kw)\n if node['id']=='check': os._exit(73)\n"
            " return result\nRunner._finish_visit=crash\n"
        )
        + "run_worker(Settings())\n",
        encoding="utf-8",
    )
    completed = subprocess.run(
        [sys.executable, str(crash)],
        env={
            **os.environ,
            "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
            "AGENTS_IDE_HEARTBEAT_SECONDS": "0.1",
        },
        capture_output=True,
        timeout=15,
    )
    assert completed.returncode == 73, completed.stderr.decode(errors="replace")
    with factory() as session:
        job = session.scalar(select(QueueJob))
        job.lease_expires_at = (
            time.time() - 1
        )  # advance the lease-expiry boundary without a 30s sleep
        session.commit()
    result = run_now(run, factory, settings)
    assert result.final_state == ("waiting_input" if phase == "before_call" else "completed")
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt)))
        assert len(attempts) == 1
        assert attempts[0].status == ("unknown" if phase == "before_call" else "succeeded")
        assert json.loads(session.get(Run, run["id"]).runtime_json)["external_calls"] == 1


def test_worker_stops_live_fake_when_database_checks_fail(
    authenticated, tmp_path, settings, monkeypatch
):
    from sqlalchemy.exc import OperationalError

    import agents_ide.worker.main as main

    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={
            "responses": [
                {"node_id": "check", "delay_seconds": 30, "files": {"too-late.txt": "bad"}}
            ]
        },
    )
    failures = []

    def check(_engine):
        with factory() as session:
            started = session.scalar(select(StepAttempt)) is not None
        if started:
            failures.append(time.monotonic())
            raise OperationalError("SELECT 1", {}, Exception("fault injection"))
        return True

    monkeypatch.setattr(main, "check_database", check)
    monkeypatch.setattr(main.signal, "signal", lambda *_: None)
    main.run_worker(settings.model_copy(update={"heartbeat_seconds": 0.05}))
    assert len(failures) == 3
    assert not (settings.data_dir / "simulated" / run["id"] / "too-late.txt").exists()
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt)))
        assert len(attempts) == 1 and attempts[0].status != "succeeded"
        assert session.scalar(select(WorkspaceReservation)).released_at is None


def test_group_exhaustion_resume_starts_one_new_round_with_old_budget(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(authenticated, tmp_path, group=True)
    calls = []

    class Adapter:
        def run(self, request):
            calls.append(request.model_id)
            if len(calls) <= 2:
                return LLMResult(
                    ExternalOutcome.UNAVAILABLE,
                    "",
                    None,
                    None,
                    error=AdapterError("unavailable", "unavailable", "safe"),
                    no_effect=True,
                )
            return LLMResult(ExternalOutcome.SUCCEEDED, "{}", {}, "passed")

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).waiting_reason.code == "model_group_exhausted"
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    assert calls == ["alpha", "beta", "alpha"]
    with factory() as session:
        row = session.get(Run, run["id"])
        assert json.loads(row.runtime_json)["external_calls"] == 3
        assert (
            session.scalar(
                select(StepExecution).where(StepExecution.node_id == "check")
            ).attempt_count
            == 3
        )


def test_explicit_unknown_resolution_keeps_unknown_history(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    run_now(run, factory, settings)
    with factory() as session:
        attempt_id = session.scalar(select(StepAttempt.id))
    response = command(
        authenticated,
        run,
        "resolve",
        {
            "reconciliation": {
                "attempt_id": attempt_id,
                "action": "retry_authorized",
                "evidence": "Reviewed the private workspace; authorize a fresh attempt.",
            }
        },
    )
    assert response.status_code == 200, response.text
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        assert session.get(StepAttempt, attempt_id).status == "unknown"
        assert (
            session.scalar(
                select(StepExecution).where(StepExecution.node_id == "check")
            ).attempt_count
            == 2
        )


def test_quiescent_cancel_and_cleanup_are_audited(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    assert command(authenticated, run, "pause").status_code == 200
    assert run_now(run, factory, settings).final_state == "paused"
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    payload = {"command_id": "cleanup-once", "expected_state_version": current["state_version"]}
    url = f"/api/runs/{run['id']}/reservations/cleanup"
    first = client.post(url, headers=headers, json=payload)
    assert first.status_code == 200, first.text
    assert client.post(url, headers=headers, json=payload).json() == first.json()
    assert command(authenticated, run, "cancel").json()["status"] == "applied"
    assert client.get(f"/api/runs/{run['id']}", headers=headers).json()["state"] == "cancelled"


def test_cleanup_refuses_live_owner(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    runner_for(run, factory, settings)
    client, headers = authenticated
    response = client.post(
        f"/api/runs/{run['id']}/reservations/cleanup",
        headers=headers,
        json={"command_id": "unsafe-cleanup", "expected_state_version": run["state_version"]},
    )
    assert response.status_code == 409
    with factory() as session:
        assert session.scalar(select(WorkspaceReservation)).released_at is None


def test_migration_from_0008_preserves_run(authenticated, tmp_path, settings):
    from pathlib import Path

    from alembic import command as alembic_command
    from alembic.config import Config

    from agents_ide.persistence import database

    run, factory = make_run(authenticated, tmp_path)
    with factory() as session:
        snapshot = session.get(Run, run["id"]).snapshot_json
    engine = authenticated[0].app.state.engine
    with engine.connect() as connection:
        config = Config()
        config.set_main_option(
            "script_location", str(Path(database.__file__).parent / "migrations")
        )
        config.attributes["connection"] = connection
        alembic_command.downgrade(config, "0008_stage5_controls")
    database.migrate(settings)
    with factory() as session:
        assert session.get(Run, run["id"]).snapshot_json == snapshot
        assert session.scalar(select(QueueJob)).owner_pid is None


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Job kill-on-owner-death")
def test_supervisor_job_kills_tree_when_owner_crashes(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    marker = tmp_path / "tree.json"
    child = tmp_path / "child.py"
    child.write_text(
        "import subprocess,sys,time,json,psutil,os\nfrom pathlib import Path\n"
        "from agents_ide.security.filesystem import atomic_write\n"
        "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'])\n"
        f"atomic_write(Path({str(marker)!r}),json.dumps([[os.getpid(),psutil.Process().create_time()],"
        "[p.pid,psutil.Process(p.pid).create_time()]]).encode())\ntime.sleep(60)\n",
        encoding="utf-8",
    )
    owner = tmp_path / "owner.py"
    owner.write_text(
        "import os,sys,time\nfrom pathlib import Path\n"
        "from sqlalchemy.orm import sessionmaker\nfrom agents_ide.config import Settings\n"
        "from agents_ide.persistence.database import create_database\n"
        "from agents_ide.engine.queue import claim_next_job\n"
        "from agents_ide.worker.processes import ProcessSupervisor,ProcessRegistry\n"
        "f=sessionmaker(bind=create_database(Settings()),expire_on_commit=False)\n"
        "j=claim_next_job(f,worker_id='crash-owner',lease_seconds=30)\n"
        "s=ProcessSupervisor(f,ProcessRegistry(),j.run_id,'crash-owner',j.generation)\n"
        f"s.start([sys.executable,{str(child)!r}],Path({str(tmp_path)!r}),dict(os.environ))\n"
        f"while not Path({str(marker)!r}).exists(): time.sleep(.01)\n"
        "os._exit(74)\n",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(owner)],
        env={**os.environ, "AGENTS_IDE_DATA_DIR": str(settings.data_dir)},
        capture_output=True,
        timeout=10,
    )
    assert process.returncode == 74, process.stderr.decode(errors="replace")
    identities = json.loads(marker.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(process_state(*p) != "dead" for p in identities):
        time.sleep(0.02)
    assert all(process_state(*p) == "dead" for p in identities)
    with factory() as session:
        row = session.scalar(select(ProcessSupervision))
        assert json.loads(row.tree_json)["job_owned"]
        assert row.run_id == run["id"]


@pytest.mark.parametrize(
    "kinds,expected", [(("pause", "stop"), "stopped"), (("pause", "stop", "cancel"), "cancelled")]
)
def test_pending_controls_priority(authenticated, tmp_path, settings, kinds, expected):
    run, factory = make_run(authenticated, tmp_path)
    for kind in kinds:
        assert command(authenticated, run, kind).status_code == 200
    assert run_now(run, factory, settings).final_state == expected
    with factory() as session:
        journals = list(session.scalars(select(CommandJournal).order_by(CommandJournal.sequence)))
        assert [j.status for j in journals] == ["superseded"] * (len(kinds) - 1) + ["applied"]
        assert session.scalar(select(StepAttempt)) is None


def test_explicit_retry_after_invalid_format_reuses_visit(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "invalid_format"}]},
    )
    assert run_now(run, factory, settings).waiting_reason.code == "invalid_response_format"
    assert command(authenticated, run, "resume").status_code == 409
    assert command(authenticated, run, "resolve", {"retry": True}).status_code == 200
    assert command(authenticated, run, "resume").status_code == 200
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        step = session.scalar(select(StepExecution).where(StepExecution.node_id == "check"))
        assert step.visit_index == 1 and step.attempt_count == 2


@pytest.mark.skipif(sys.platform != "win32", reason="CLI credentials use Windows DPAPI")
def test_cli_multiple_commands_pair_once_and_deduplicate(authenticated, tmp_path, settings):
    import socket

    import httpx

    run, factory = make_run(authenticated, tmp_path)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    env = {
        **os.environ,
        "AGENTS_IDE_DATA_DIR": str(settings.data_dir),
        "AGENTS_IDE_PORT": str(port),
        "PYTHONIOENCODING": "utf-8",
    }
    api = subprocess.Popen(
        [sys.executable, "-m", "agents_ide", "api"],
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )

    def cli(*args, success=True):
        process = subprocess.run(
            [sys.executable, "-m", "agents_ide", *args],
            env=env,
            capture_output=True,
            encoding="utf-8",
            timeout=15,
        )
        assert (process.returncode == 0) == success, process.stderr
        return json.loads(process.stdout) if success else process

    try:
        deadline = time.monotonic() + 8
        with httpx.Client(trust_env=False) as client:
            while time.monotonic() < deadline:
                try:
                    if client.get(f"http://127.0.0.1:{port}/api/health").status_code == 200:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.05)
        assert cli("runs", "status", "--run-id", run["id"])["id"] == run["id"]
        first_cache = (settings.data_dir / f"runtime/cli-session-{port}.json").read_text()
        assert cli("runs", "status", "--run-id", run["id"])["state"] == "queued"
        assert (settings.data_dir / f"runtime/cli-session-{port}.json").read_text() == first_cache
        first = cli("runs", "pause", "--run-id", run["id"], "--command-id", "cli-pause")
        second = cli("runs", "pause", "--run-id", run["id"], "--command-id", "cli-pause")
        assert first == second
        assert cli("projects", "list")[0]["id"] == run["project_id"]
        body = tmp_path / "chat.json"
        body.write_text('{"title":"CLI chat"}', encoding="utf-8")
        chat = cli(
            "chats", "create", "--project-id", run["project_id"], "--payload-json", str(body)
        )
        assert chat["project_id"] == run["project_id"]
        assert cli("runs", "diagnostics", "--run-id", run["id"])["run_id"] == run["id"]
        assert "not_found" in cli("runs", "status", "--run-id", "missing", success=False).stderr
        with factory() as session:
            commands = list(
                session.scalars(
                    select(CommandJournal).where(CommandJournal.command_id == "cli-pause")
                )
            )
            assert len(commands) == 1
        from sqlalchemy import text

        with factory() as session:
            session.execute(text("UPDATE auth_sessions SET revoked=1"))
            session.commit()
        assert "auth_required" in cli("runs", "status", "--run-id", run["id"], success=False).stderr
        assert cli("runs", "status", "--run-id", run["id"], "--re-pair")["id"] == run["id"]
    finally:
        api.terminate()
        api.communicate(timeout=8)
