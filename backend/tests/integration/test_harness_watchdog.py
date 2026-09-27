"""Idle recovery preserves the native session and yields to user controls."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage6a_review import runner_for, seed

from agents_ide.adapters.base import AdapterError, AgentResult, ExternalOutcome
from agents_ide.domain.common import new_id
from agents_ide.engine.events import append_event
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    AgentSession,
    CommandJournal,
    Run,
    RunEvent,
    StepAttempt,
    StepExecution,
)
from agents_ide.worker.harness_watchdog import check_harnesses


def active_run(authenticated, tmp_path):
    run, _ = seed(authenticated, tmp_path, roles=("implementer",))
    factory = authenticated[0].app.state.session_factory
    with factory() as db:
        execution = StepExecution(
            id=new_id(),
            run_id=run["id"],
            node_id="a0",
            visit_index=1,
            cycle_id=1,
            status="running",
            started_at=100,
        )
        db.add(execution)
        db.flush()
        attempt = StepAttempt(
            id=new_id(),
            execution_id=execution.id,
            attempt_index=1,
            status="running",
            started_at=100,
            heartbeat_at=99999,
        )
        db.add(attempt)
        db.flush()
        db.add(
            AgentSession(
                id=new_id(),
                attempt_id=attempt.id,
                harness_kind="opencode",
                role="implementer",
                started_at=100,
                last_external_event_at=100,
            )
        )
        row = db.get(Run, run["id"])
        row.state = "running"
        row.current_node_id = "a0"
        row.current_execution_id = execution.id
        row.current_attempt_id = attempt.id
        db.commit()
    return run["id"], factory


def mark_paused(factory, run_id, *, status="interrupted", session_id="saved-session"):
    with factory() as db:
        row = db.get(Run, run_id)
        row.state = "paused"
        attempt = db.get(StepAttempt, row.current_attempt_id)
        attempt.status = status
        row.runtime_json = json.dumps(
            {
                "agent_continuation": {
                    "session_id": session_id,
                    "execution_id": row.current_execution_id,
                }
            }
        )
        pause = db.scalar(select(CommandJournal).where(CommandJournal.run_id == run_id))
        pause.status = "applied"
        db.commit()


def test_settings_default_validation_and_persistence(authenticated):
    client, headers = authenticated
    original = client.get("/api/settings/general").json()
    assert original["harness_watchdog"] == {"enabled": True, "idle_minutes": 30}
    for minutes in (0, 1441, 1.5, "30", True):
        invalid = {**original, "harness_watchdog": {"idle_minutes": minutes}}
        assert client.put("/api/settings/general", headers=headers, json=invalid).status_code == 422
    changed = {**original, "harness_watchdog": {"enabled": False, "idle_minutes": 42}}
    saved = client.put("/api/settings/general", headers=headers, json=changed)
    assert saved.status_code == 200
    assert (
        client.get("/api/settings/general").json()["harness_watchdog"]
        == changed["harness_watchdog"]
    )
    assert saved.json()["commit_message"] == original["commit_message"]


def test_stale_activity_ignores_heartbeat_and_recovers_once(authenticated, tmp_path):
    run_id, factory = active_run(authenticated, tmp_path)
    check_harnesses(factory, now=1899)
    with factory() as db:
        assert db.get(Run, run_id).state == "running"
    check_harnesses(factory, now=1900)
    check_harnesses(factory, now=2000)
    with factory() as db:
        assert db.get(Run, run_id).state == "pause_requested"
        assert len(list(db.scalars(select(CommandJournal)))) == 1
    mark_paused(factory, run_id)
    # Do not race the outgoing dispatch thread's cleanup with a new dispatch.
    check_harnesses(factory, active_runs={run_id}, now=2000)
    with factory() as db:
        assert db.get(Run, run_id).state == "paused"
    check_harnesses(factory, now=2001)
    check_harnesses(factory, now=2002)
    with factory() as db:
        assert db.get(Run, run_id).state == "queued"
        assert [
            c.command_type
            for c in db.scalars(select(CommandJournal).order_by(CommandJournal.sequence))
        ] == ["pause", "resume"]


@pytest.mark.parametrize("kind", ["question", "permission", "artifact"])
def test_pending_input_is_not_a_stall_and_reply_resets_interval(authenticated, tmp_path, kind):
    run_id, factory = active_run(authenticated, tmp_path)
    with factory() as db:
        row = db.get(Run, run_id)
        payload = {"question_id": "q1", "kind": kind}
        if kind == "artifact":
            payload["questions"] = [{"question": "x" * 70000}]
        append_event(
            db, run_id, "agent.input_requested", payload, attempt_id=row.current_attempt_id
        )
        db.commit()
    check_harnesses(factory, now=5000)
    with factory() as db:
        row = db.get(Run, run_id)
        assert row.state == "running"
        append_event(
            db,
            run_id,
            "agent.input_closed",
            {"question_id": "q1"},
            attempt_id=row.current_attempt_id,
        )
        agent = db.scalar(select(AgentSession))
        agent.last_external_event_at = 5000
        db.commit()
    check_harnesses(factory, now=6799)
    with factory() as db:
        assert db.get(Run, run_id).state == "running"
    check_harnesses(factory, now=6800)
    with factory() as db:
        assert db.get(Run, run_id).state == "pause_requested"


@pytest.mark.parametrize("kind", ["pause", "stop", "cancel"])
def test_manual_control_cancels_automatic_continuation(authenticated, tmp_path, kind):
    run_id, factory = active_run(authenticated, tmp_path)
    check_harnesses(factory, now=1900)
    mark_paused(factory, run_id)
    client, headers = authenticated
    current = client.get(f"/api/runs/{run_id}").json()
    response = client.post(
        f"/api/runs/{run_id}/commands",
        headers=headers,
        json={
            "command_id": "manual",
            "command_type": kind,
            "expected_state_version": current["state_version"],
        },
    )
    assert response.status_code == 200, response.text
    check_harnesses(factory, now=4000)
    with factory() as db:
        assert (
            db.get(Run, run_id).state
            == {
                "pause": "paused",
                "stop": "stopped",
                "cancel": "cancelled",
            }[kind]
        )
        assert not db.scalar(select(CommandJournal).where(CommandJournal.command_type == "resume"))


@pytest.mark.parametrize("status,session_id", [("unknown", "saved"), ("interrupted", None)])
def test_unconfirmed_or_missing_session_is_never_replayed(
    authenticated, tmp_path, status, session_id
):
    run_id, factory = active_run(authenticated, tmp_path)
    check_harnesses(factory, now=1900)
    mark_paused(factory, run_id, status=status, session_id=session_id)
    check_harnesses(factory, now=2000)
    check_harnesses(factory, now=3000)
    with factory() as db:
        assert db.get(Run, run_id).state == "paused"
        assert len(list(db.scalars(select(CommandJournal)))) == 1
        events = list(db.scalars(select(RunEvent).where(RunEvent.type == "agent.watchdog")))
        assert len(events) == 2
        assert json.loads(events[-1].payload_json)["action"] == "blocked"


@pytest.mark.parametrize("change", ["disabled", "fresh", "fake", "manual_pause", "longer_timeout"])
def test_only_enabled_idle_native_calls_are_monitored(authenticated, tmp_path, change):
    run_id, factory = active_run(authenticated, tmp_path)
    client, headers = authenticated
    if change in {"disabled", "longer_timeout"}:
        settings = client.get("/api/settings/general").json()
        settings["harness_watchdog"] = {"enabled": change != "disabled", "idle_minutes": 60}
        assert (
            client.put("/api/settings/general", headers=headers, json=settings).status_code == 200
        )
    with factory() as db:
        if change == "fresh":
            db.scalar(select(AgentSession)).last_external_event_at = 1899
        if change == "fake":
            db.scalar(select(AgentSession)).harness_kind = "fake"
        if change == "manual_pause":
            db.get(Run, run_id).state = "paused"
        db.commit()
    check_harnesses(factory, now=1900)
    with factory() as db:
        assert not list(db.scalars(select(CommandJournal)))


def test_runner_auto_pause_resume_keeps_session_model_stage_and_edits(
    authenticated, settings, tmp_path, monkeypatch
):
    run, _ = seed(authenticated, tmp_path, roles=("implementer",))
    factory = authenticated[0].app.state.session_factory
    calls = []

    class Agent:
        def run(self, request):
            calls.append(request)
            request.emit_event("agent.session_created", {"session_id": "saved-session"})
            path = Path(request.workspace_path) / "partial.txt"
            if len(calls) == 1:
                path.write_text("preserved work", encoding="utf-8")
                with factory() as db:
                    db.scalar(select(AgentSession)).last_external_event_at = 100
                    db.scalar(select(StepAttempt)).started_at = 100
                    db.commit()
                request.emit_event("attempt.progress", {"native_type": "session.status"})
                with factory() as db:
                    assert db.scalar(select(AgentSession)).last_external_event_at == 100
                request.emit_event("attempt.progress", {"activity": True})
                with factory() as db:
                    progress_at = db.scalar(select(AgentSession)).last_external_event_at
                    assert progress_at > 100
                    assert not any(
                        json.loads(e.payload_json).get("activity")
                        for e in db.scalars(select(RunEvent))
                    )
                check_harnesses(factory, now=progress_at + 1800)
                return AgentResult(
                    ExternalOutcome.UNKNOWN,
                    "partial",
                    None,
                    None,
                    error=AdapterError(
                        "interrupted", "interrupted", details={"interruption_confirmed": True}
                    ),
                )
            assert path.read_text(encoding="utf-8") == "preserved work"
            return AgentResult(
                ExternalOutcome.SUCCEEDED, '{"verdict":"passed"}', {"verdict": "passed"}, "passed"
            )

    monkeypatch.setattr(Runner, "_agent_adapter_for", lambda *_: Agent())
    assert runner_for(authenticated[0], settings).execute(run["id"]).final_state == "paused"
    check_harnesses(factory)
    assert runner_for(authenticated[0], settings).execute(run["id"]).final_state == "completed"
    assert len(calls) == 2
    assert calls[1].resume_session_id == "saved-session"
    assert calls[1].resume_required
    assert calls[1].model_id == calls[0].model_id
    with factory() as db:
        assert (
            len(list(db.scalars(select(StepExecution).where(StepExecution.node_id == "a0")))) == 1
        )


def test_failed_resume_rolls_back_mutations_and_is_not_retried(
    authenticated, tmp_path, monkeypatch
):
    from agents_ide.services import run_controls

    run_id, factory = active_run(authenticated, tmp_path)
    check_harnesses(factory, now=1900)
    mark_paused(factory, run_id)
    calls = []

    def reject(session, run):
        calls.append(run.id)
        run.state = "queued"
        session.flush()
        raise AppError("reconciliation_required", "Требуется сверка", 409)

    monkeypatch.setattr(run_controls, "_check_resume", reject)
    check_harnesses(factory, now=2000)
    check_harnesses(factory, now=3000)
    with factory() as db:
        assert db.get(Run, run_id).state == "paused"
        assert len(list(db.scalars(select(CommandJournal)))) == 1
    assert calls == [run_id]
