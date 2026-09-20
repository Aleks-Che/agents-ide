"""Selected-stage restart keeps provenance and fences the previous writer."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import command, run_now, wait_for

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.domain.common import to_json, utc_now
from agents_ide.engine.runner import Runner
from agents_ide.engine.visits import load_latest_results
from agents_ide.persistence.models import CommandJournal, Run, StepAttempt, StepExecution


def stage_payload(factory, run, node_id="check"):
    with factory() as session:
        execution = session.scalar(
            select(StepExecution)
            .where(StepExecution.run_id == run["id"], StepExecution.node_id == node_id)
            .order_by(StepExecution.visit_index.desc())
        )
        return {"node_id": node_id, "execution_id": execution.id}


@pytest.mark.parametrize("state", ["waiting_input", "paused", "stopped", "failed"])
@pytest.mark.parametrize("hidden_by_old_restart", [False, True])
def test_stopped_stage_can_restart_with_old_configuration_blocker(
    authenticated, tmp_path, settings, monkeypatch, state, hidden_by_old_restart
):
    run, factory = make_run(authenticated, tmp_path)
    with monkeypatch.context() as failing:
        failing.setattr(
            Runner,
            "_external",
            lambda self, node, visit, adapter: self._waiting(
                "configuration_invalid", {"reason": "test_configuration_failure"}, visit
            ),
        )
        assert run_now(run, factory, settings).final_state == "waiting_input"
    payload = stage_payload(factory, run)
    with factory() as session:
        row = session.get(Run, run["id"])
        row.state = state
        if state == "failed":
            row.finished_at = utc_now()
        if hidden_by_old_restart:
            runtime = json.loads(row.runtime_json)
            runtime["invalidated_executions"] = [payload["execution_id"]]
            row.runtime_json = to_json(runtime)
            row.current_execution_id = row.current_attempt_id = None
            target = json.loads(row.resume_target_json)
            target.update(action="dispatch_next", execution_id=None)
            row.resume_target_json = to_json(target)
            session.get(StepExecution, payload["execution_id"]).status = "interrupted"
        session.commit()
    client, headers = authenticated
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    node = next(node for node in snapshot["observation"]["nodes"] if node["id"] == "check")
    assert node["execution_id"] == payload["execution_id"]
    assert node["restart_blocked_reason"] is None
    response = command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200, response.text
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.finished_at is None
        assert json.loads(row.resume_target_json)["blockers"] == []
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        assert session.get(StepExecution, payload["execution_id"]).status == "interrupted"
        visits = list(
            session.scalars(select(StepExecution).where(StepExecution.node_id == "check"))
        )
        assert len(visits) == 2


def test_unknown_attempt_restarts_once_without_erasing_history(
    authenticated, tmp_path, settings, monkeypatch
):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    assert run_now(run, factory, settings).waiting_reason.code == "unknown_external_result"
    payload = stage_payload(factory, run)
    client, headers = authenticated
    row = client.get(f"/api/runs/{run['id']}", headers=headers).json()
    body = {
        "command_id": "restart-once",
        "command_type": "restart_stage",
        "expected_state_version": row["state_version"],
        "payload": payload,
    }
    first = client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=body)
    assert first.status_code == 200, first.text
    assert first.json()["status"] == "applied"
    again = client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=body)
    assert again.json() == first.json()
    from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, FakeScenario

    monkeypatch.setattr(
        Runner,
        "_build_adapters",
        lambda *_: (FakeAgentAdapter(FakeScenario()), FakeLLMAdapter(FakeScenario())),
    )
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        attempts = list(session.scalars(select(StepAttempt).order_by(StepAttempt.started_at)))
        assert [a.status for a in attempts] == ["unknown", "succeeded"]
        assert attempts[0].execution_id != attempts[1].execution_id
        assert all(a.result_artifact_id for a in attempts)
        assert len(list(session.scalars(select(CommandJournal)))) == 1


@pytest.mark.parametrize("stopped_node", ["s", "condition", "e"])
def test_stopped_service_stage_can_restart(
    authenticated, tmp_path, settings, monkeypatch, stopped_node
):
    graph = {
        "nodes": [
            {"id": "s", "type": "Start"},
            {"id": "condition", "type": "Condition", "expression": {"const": True}},
            {"id": "e", "type": "End"},
        ],
        "edges": [
            {"from": "s", "to": "condition"},
            {"from": "condition", "to": "e", "when": "true"},
            {"from": "condition", "to": "e", "when": "false"},
            {"from": "condition", "to": "e", "when": "unknown"},
        ],
    }
    run, factory = make_run(authenticated, tmp_path, graph=graph)
    original = Runner._finish_visit

    def stop_at_stage(self, node, visit, result):
        if node["id"] == stopped_node:
            return self._waiting("missing_data", {"reason": "test_stop"}, visit)
        return original(self, node, visit, result)

    with monkeypatch.context() as stopped:
        stopped.setattr(Runner, "_finish_visit", stop_at_stage)
        assert run_now(run, factory, settings).final_state == "waiting_input"
    payload = stage_payload(factory, run, stopped_node)
    client, headers = authenticated
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    node = next(n for n in snapshot["observation"]["nodes"] if n["id"] == stopped_node)
    assert node["restart_blocked_reason"] is None
    response = command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200, response.text
    assert run_now(run, factory, settings).final_state == "completed"


def test_running_stage_stops_before_fresh_visit(authenticated, tmp_path, settings):
    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "delay_seconds": 1.0}]},
    )
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(run_now, run, factory, settings)
        old = wait_for(
            factory, lambda s: s.scalar(select(StepAttempt).where(StepAttempt.status == "running"))
        )
        response = command(authenticated, run, "restart_stage", stage_payload(factory, run))
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "accepted"
        assert future.result(timeout=10).final_state == "completed"
    with factory() as session:
        old = session.get(StepAttempt, old.id)
        new = session.scalar(select(StepAttempt).where(StepAttempt.id != old.id))
        assert old.status == "interrupted" and new.status == "succeeded"
        assert old.finished_at <= new.started_at
        assert old.execution_id != new.execution_id
        assert session.scalar(select(CommandJournal)).status == "applied"


def test_previous_stage_invalidates_downstream_but_preserves_prefix(
    authenticated, tmp_path, settings, monkeypatch
):
    graph = {
        "nodes": [{"id": "s", "type": "Start"}]
        + [
            {
                "id": node,
                "type": "LLMRequest",
                "config": {
                    "prompt": node,
                    "model_selection": {
                        "kind": "direct",
                        "model_id": "m",
                        "provider_connection_id": "CONNECTION",
                    },
                },
            }
            for node in ["prefix", "first", "second"]
        ]
        + [{"id": "e", "type": "End"}],
        "edges": [
            {"from": a, "to": b}
            for a, b in [("s", "prefix"), ("prefix", "first"), ("first", "second"), ("second", "e")]
        ],
    }
    run, factory = make_run(authenticated, tmp_path, graph=graph)
    calls = []

    class Adapter:
        def run(self, request):
            calls.append(request.prompt)
            if len(calls) == 3:
                assert command(authenticated, run, "pause").status_code == 200
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).final_state == "paused"
    with factory() as session:
        row = session.get(Run, run["id"])
        runtime = json.loads(row.runtime_json)
        runtime["native_sessions"] = {"old": {"session_id": "stale"}}
        row.runtime_json = to_json(runtime)
        session.commit()
    response = command(authenticated, run, "restart_stage", stage_payload(factory, run, "first"))
    assert response.status_code == 200, response.text
    with factory() as session:
        results = load_latest_results(session, run["id"], 1, ["prefix", "first", "second"])
        assert set(results) == {"prefix"}
        assert json.loads(session.get(Run, run["id"]).runtime_json)["native_sessions"] == {}
        assert (
            len(
                list(
                    session.scalars(
                        select(StepExecution).where(StepExecution.status == "succeeded")
                    )
                )
            )
            == 4
        )  # Original output remains in the audit history.
    assert run_now(run, factory, settings).final_state == "completed"
    assert calls == ["prefix", "first", "second", "first", "second"]


def test_restart_rejects_foreign_or_unvisited_execution(authenticated, tmp_path, settings):
    run, factory = make_run(authenticated, tmp_path)
    assert (
        command(
            authenticated, run, "restart_stage", {"node_id": "check", "execution_id": "missing"}
        ).status_code
        == 422
    )
    assert command(authenticated, run, "restart_stage", {}).status_code == 422


def test_cancel_supersedes_pending_restart(authenticated, tmp_path, settings, monkeypatch):
    run, factory = make_run(authenticated, tmp_path)

    class Adapter:
        def run(self, request):
            assert (
                command(
                    authenticated, run, "restart_stage", stage_payload(factory, run)
                ).status_code
                == 200
            )
            assert command(authenticated, run, "cancel").status_code == 200
            return LLMResult(ExternalOutcome.SUCCEEDED, "ok", {"text": "ok"}, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (None, Adapter()))
    assert run_now(run, factory, settings).final_state == "cancelled"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1
        restart = session.scalar(
            select(CommandJournal).where(CommandJournal.command_type == "restart_stage")
        )
        assert restart.status == "superseded"


def test_restart_waits_for_proven_stop_and_supersedes_old_request(
    authenticated, tmp_path, settings, monkeypatch
):
    from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, FakeScenario
    from agents_ide.worker import processes

    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    assert run_now(run, factory, settings).final_state == "waiting_input"
    payload = stage_payload(factory, run)
    monkeypatch.setattr(processes, "stored_processes_stopped", lambda *_: False)
    response = command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    assert run_now(run, factory, settings).final_state == "waiting_input"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 1
        assert session.get(Run, run["id"]).current_execution_id == payload["execution_id"]
    monkeypatch.setattr(processes, "stored_processes_stopped", lambda *_: True)
    response = command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200 and response.json()["status"] == "applied"
    monkeypatch.setattr(
        Runner,
        "_build_adapters",
        lambda *_: (FakeAgentAdapter(FakeScenario()), FakeLLMAdapter(FakeScenario())),
    )
    assert run_now(run, factory, settings).final_state == "completed"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 2
        journals = list(session.scalars(select(CommandJournal).order_by(CommandJournal.sequence)))
        assert [row.status for row in journals] == ["superseded", "applied"]


def test_restart_between_claim_and_dispatch(authenticated, tmp_path, settings, monkeypatch):
    from test_stage5_review import runner_for

    from agents_ide.adapters.fake import FakeAgentAdapter, FakeLLMAdapter, FakeScenario

    run, factory = make_run(
        authenticated,
        tmp_path,
        fake_scenario={"responses": [{"node_id": "check", "outcome": "unknown"}]},
    )
    assert run_now(run, factory, settings).final_state == "waiting_input"
    payload = stage_payload(factory, run)
    with factory() as session:
        from agents_ide.services.run_controls import ensure_queue

        row = session.get(Run, run["id"])
        row.state = "queued"
        ensure_queue(session, row)
        session.commit()
    runner, _ = runner_for(run, factory, settings)
    response = command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200 and response.json()["status"] == "accepted"
    monkeypatch.setattr(
        Runner,
        "_build_adapters",
        lambda *_: (FakeAgentAdapter(FakeScenario()), FakeLLMAdapter(FakeScenario())),
    )
    assert runner.execute(run["id"]).final_state == "completed"
    with factory() as session:
        assert len(list(session.scalars(select(StepAttempt)))) == 2
