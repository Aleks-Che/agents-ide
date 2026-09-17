import json
from dataclasses import replace

import pytest
from sqlalchemy import select

from agents_ide.adapters.base import AgentAdapter, AgentResult, ExternalOutcome
from agents_ide.domain.graph_ast import Value
from agents_ide.engine import queue, visits
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import CommandJournal, Run
from tests.integration.test_stage5_controls import make_run


def agent_graph(field="report"):
    selection = {"kind": "direct", "harness_profile_id": "PROFILE", "model_id": "m"}
    return {
        "nodes": [
            {"id": "start", "type": "Start"},
            {
                "id": "agent",
                "type": "AgentTask",
                "config": {"prompt": "go", "model_selection": selection},
            },
            {
                "id": "review",
                "type": "AgentTask",
                "config": {
                    "prompt": "{{steps.agent.latest.validated_result." + field + "}}",
                    "model_selection": selection,
                },
            },
            {"id": "end", "type": "End"},
        ],
        "edges": [
            {"from": "start", "to": "agent"},
            {"from": "agent", "to": "review"},
            {"from": "review", "to": "end"},
        ],
    }


def test_messages_are_scoped_durable_and_not_replayed(
    authenticated, tmp_path, settings, monkeypatch
):
    client, headers = authenticated
    run, factory = make_run(authenticated, tmp_path, graph=agent_graph())
    url = f"/api/runs/{run['id']}"
    original_snapshot = client.get(url).json()["snapshot_hash"]
    bodies = []

    class InteractiveAgent(AgentAdapter):
        def run(self, request):
            current = client.get(url).json()
            snapshot = client.get(f"{url}/snapshot").json()
            attempt_id = next(
                n
                for n in snapshot["observation"]["nodes"]
                if n["id"] == snapshot["observation"]["current_node_id"]
            )["attempt_id"]
            if request.prompt == "go":
                # Oversized events are archived; the pending question must still
                # survive reloading a snapshot, including after a long transcript.
                questions = [{"id": str(i), "question": "Q" * 3000} for i in range(8)]
                request.emit_event(
                    "agent.input_requested", {"question_id": "q", "questions": questions}
                )
                observed = client.get(f"{url}/snapshot").json()["observation"]["nodes"]
                pending = next(n for n in observed if n["id"] == "agent")["input_request"]
                assert pending["questions"] == questions
                request.emit_event("agent.input_closed", {"question_id": "q"})
                observed = client.get(f"{url}/snapshot").json()["observation"]["nodes"]
                assert next(n for n in observed if n["id"] == "agent")["input_request"] is None
                for number in range(2):
                    body = {
                        "command_id": f"input{number}",
                        "command_type": "message",
                        "expected_state_version": current["state_version"],
                        "payload": {"attempt_id": attempt_id, "text": f"answer{number}"},
                    }
                    bodies.append(body)
                    response = client.post(f"{url}/commands", json=body, headers=headers)
                    assert response.status_code == 200, response.text
                    assert (
                        client.post(f"{url}/commands", json=body, headers=headers).json()
                        == response.json()
                    )
                    current = client.get(url).json()
                message = request.receive_message()
                assert message["text"] == "answer0"
                request.emit_event(
                    "agent.user_message_status",
                    {"command_id": message["command_id"], "delivered": True},
                )
                # Claimed without acknowledgement: do not replay after a lost response.
                assert request.receive_message()["text"] == "answer1"
                assert request.receive_message() is None
            else:
                assert request.prompt == "Report from first agent"
                assert request.receive_message() is None
                stale = {
                    **bodies[0],
                    "command_id": "late",
                    "expected_state_version": current["state_version"],
                }
                response = client.post(f"{url}/commands", json=stale, headers=headers)
                assert response.status_code == 409, response.text
            return AgentResult(
                ExternalOutcome.SUCCEEDED,
                "Report from first agent",
                {"text": "Report from first agent"},
                None,
            )

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (InteractiveAgent(), None))
    job = queue.claim_next_job(factory, worker_id="input-test", lease_seconds=30)
    runner = Runner(
        session_factory=factory,
        worker_id="input-test",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    result = runner.execute(run["id"])
    assert result.final_state.value == "completed"
    with factory() as session:
        commands = list(session.scalars(select(CommandJournal).order_by(CommandJournal.sequence)))
        assert [row.status for row in commands] == ["applied", "rejected"]
        assert json.loads(commands[1].response_json)["reason"] == "delivery_unknown"
        assert session.get(Run, run["id"]).state == "completed"
    assert client.get(url).json()["snapshot_hash"] == original_snapshot
    assert (
        client.post(f"{url}/commands", json=bodies[0], headers=headers).json()["status"]
        == "applied"
    )
    history = client.get(f"{url}/history?node_id=agent").json()["events"]
    assert len([e for e in history if e["type"] == "agent.user_message"]) == 2


@pytest.mark.parametrize("field", ["report", "nonexistent"])
def test_resume_revalidates_old_prompt_without_repeating_completed_agent(
    authenticated, tmp_path, settings, monkeypatch, field
):
    client, headers = authenticated
    run, factory = make_run(authenticated, tmp_path, graph=agent_graph(field))
    original = visits.load_latest_results
    prompts = []

    def old_projection(*args, **kwargs):
        results = original(*args, **kwargs)
        return {
            key: replace(
                result,
                validated_result=Value.of(
                    {k: v for k, v in result.validated_result.raw.items() if k != "report"}
                ),
            )
            for key, result in results.items()
        }

    class Agent(AgentAdapter):
        def run(self, request):
            prompts.append(request.prompt)
            return AgentResult(ExternalOutcome.SUCCEEDED, "Report", {"text": "Report"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (Agent(), None))
    monkeypatch.setattr(visits, "load_latest_results", old_projection)

    def execute():
        job = queue.claim_next_job(factory, worker_id="resume-test", lease_seconds=30)
        result = Runner(
            session_factory=factory,
            worker_id="resume-test",
            generation=job.generation,
            data_dir=settings.data_dir,
            secret_store=None,
        ).execute(run["id"])
        queue.release_job(factory, job_id=job.job_id, worker_id="resume-test")
        return result

    assert execute().final_state.value == "waiting_input"
    assert prompts == ["go"]
    monkeypatch.setattr(visits, "load_latest_results", original)
    url = f"/api/runs/{run['id']}"
    waiting = client.get(url).json()
    assert waiting["waiting_reason"]["details"]["reason"] == "runtime_expression_invalid"
    response = client.post(
        f"{url}/commands",
        headers=headers,
        json={
            "command_id": "resume",
            "command_type": "resume",
            "expected_state_version": waiting["state_version"],
        },
    )
    if field == "nonexistent":
        assert response.status_code == 409, response.text
        assert client.get(url).json()["state"] == "waiting_input"
        assert prompts == ["go"]
    else:
        assert response.status_code == 200, response.text
        assert execute().final_state.value == "completed"
        assert prompts == ["go", "Report"]
    assert client.get(url).json()["snapshot_hash"] == waiting["snapshot_hash"]


@pytest.mark.parametrize("first_outcome", [ExternalOutcome.SUCCEEDED, ExternalOutcome.UNKNOWN])
def test_full_restart_cancels_previous_run_and_starts_from_first_agent(
    authenticated, tmp_path, settings, monkeypatch, first_outcome
):
    client, headers = authenticated
    run, factory = make_run(authenticated, tmp_path, graph=agent_graph())
    url = f"/api/runs/{run['id']}"
    replacement = None
    restart_body = None
    prompts = []

    class Agent(AgentAdapter):
        def run(self, request):
            nonlocal replacement, restart_body
            prompts.append(request.prompt)
            outcome = ExternalOutcome.SUCCEEDED
            if replacement is None:
                outcome = first_outcome
                restart_body = {
                    "command_id": "restart-button",
                    "expected_state_version": client.get(url).json()["state_version"],
                }
                response = client.post(f"{url}/restart", headers=headers, json=restart_body)
                assert response.status_code == 201, response.text
                replacement = response.json()
                assert replacement["id"] != run["id"]
                assert replacement["state"] == "queued"
                fresh = client.get(f"/api/runs/{replacement['id']}/snapshot").json()
                assert fresh["observation"]["current_node_id"] is None
                assert client.get(url).json()["state"] == "stop_requested"
                # Even a concurrent worker cannot dispatch the replacement while
                # the predecessor still owns this workspace.
                assert queue.claim_next_job(factory, worker_id="other", lease_seconds=30) is None
            return AgentResult(outcome, "Report", {"text": "Report"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (Agent(), None))

    def execute_next():
        job = queue.claim_next_job(factory, worker_id="restart-test", lease_seconds=30)
        return Runner(
            session_factory=factory,
            worker_id="restart-test",
            generation=job.generation,
            data_dir=settings.data_dir,
            secret_store=None,
        ).execute(job.run_id)

    stopped = execute_next()
    assert stopped.final_state.value == "cancelled", stopped.waiting_reason
    assert prompts == ["go"]
    assert (
        client.post(f"{url}/restart", headers=headers, json=restart_body).json()["id"]
        == replacement["id"]
    )
    assert execute_next().final_state.value == "completed"
    assert prompts == ["go", "go", "Report"]
    assert len(client.get("/api/runs").json()) == 2


def test_stale_restart_does_not_create_or_cancel_any_run(authenticated, tmp_path):
    client, headers = authenticated
    run, _ = make_run(authenticated, tmp_path)
    response = client.post(
        f"/api/runs/{run['id']}/restart",
        headers=headers,
        json={"command_id": "stale-restart", "expected_state_version": run["state_version"] + 1},
    )
    assert response.status_code == 409, response.text
    assert len(client.get("/api/runs").json()) == 1
    assert client.get(f"/api/runs/{run['id']}").json()["state"] == "queued"


def test_stop_workspace_probe_does_not_hold_database_write_lock(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.engine import context_sources
    from agents_ide.services.transactions import begin_write

    client, headers = authenticated
    run, factory = make_run(authenticated, tmp_path)
    job = queue.claim_next_job(factory, worker_id="stop-check", lease_seconds=30)
    runner = Runner(
        session_factory=factory,
        worker_id="stop-check",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )
    runner.run_id = run["id"]
    runner.runtime = {"git": {"phase": "ready"}, "cycle_id": 1}
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state = "running"
        runner.snapshot = json.loads(row.snapshot_json)
        session.commit()
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={"command_id": "stop", "command_type": "stop", "expected_state_version": 0},
    )
    assert response.status_code == 200, response.text

    def probe(_):
        with factory() as session:
            # A heartbeat can obtain the write reservation during filesystem I/O.
            session.connection().exec_driver_sql("PRAGMA busy_timeout=100")
            begin_write(session)
            session.rollback()
        return "checkpoint"

    monkeypatch.setattr(context_sources, "workspace_hash", probe)
    assert runner._controls().final_state.value == "stopped"
    assert runner.runtime["git_paused_workspace_hash"] == "checkpoint"
