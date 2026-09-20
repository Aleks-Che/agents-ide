"""A failed commit-message request can be retried without replaying earlier stages."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import command as run_command
from test_stage5_review import run_now
from test_stage7_review import chain
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture
from test_stage_restart import stage_payload

from agents_ide.adapters.base import AdapterError, AgentResult, ExternalOutcome, LLMResult
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.engine import artifacts
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import Run, StepAttempt, StepExecution

repository = repository_fixture


def failed_commit_run(authenticated, repository, settings, monkeypatch, failure="provider_error"):
    calls = []

    class Agent:
        def run(self, request):
            calls.append(request)
            (Path(request.workspace_path) / "src/a.txt").write_text("finished work\n")
            return AgentResult(ExternalOutcome.SUCCEEDED, "Done", {"text": "Done"}, None)

    messages = []

    def generate(self, request):
        messages.append(request)
        if len(messages) == 1:
            return LLMResult(
                ExternalOutcome.UNAVAILABLE
                if failure == "provider_error"
                else ExternalOutcome.SUCCEEDED,
                "",
                None,
                None,
                error=AdapterError("invalid_provider_response", "Empty response", "safe")
                if failure == "provider_error"
                else None,
                no_effect=True,
            )
        return LLMResult(
            ExternalOutcome.SUCCEEDED, "fix: finish work", {"text": "fix: finish work"}, None
        )

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (Agent(), None))
    monkeypatch.setattr(HttpLLMAdapter, "run", generate)
    run, factory = make_run(
        authenticated,
        repository.parent,
        graph=chain(
            {
                "id": "agent",
                "type": "AgentTask",
                "config": {"prompt": "go", "harness_profile_id": "PROFILE", "model": "test"},
            },
            {
                "id": "commit",
                "type": "GitCommit",
                "config": {
                    "allowlist": ["src/**"],
                    "generate_message": True,
                    "message_generation": {"connection_id": "CONNECTION", "model": "test"},
                },
            },
        ),
        execution_mode="real",
    )
    result = run_now(run, factory, settings)
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "configuration_invalid"
    assert command(repository, "rev-list", "--count", "HEAD") == "1"
    return run, factory, calls, messages


@pytest.mark.parametrize("failure", ["provider_error", "empty_message"])
@pytest.mark.parametrize("stop_first", [False, True])
def test_restart_commit_message_failure_preserves_previous_work(
    authenticated, repository, settings, monkeypatch, failure, stop_first
):
    run, factory, calls, messages = failed_commit_run(
        authenticated, repository, settings, monkeypatch, failure
    )
    payload = stage_payload(factory, run, "commit")
    with factory() as session:
        old_attempt_id = session.get(Run, run["id"]).current_attempt_id
        agent_execution_id = session.scalar(
            select(StepExecution.id).where(StepExecution.node_id == "agent")
        )
    if stop_first:
        assert run_command(authenticated, run, "stop").status_code == 200
    client, headers = authenticated
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    node = next(node for node in snapshot["observation"]["nodes"] if node["id"] == "commit")
    assert node["restart_blocked_reason"] is None
    body = {
        "command_id": "retry-commit",
        "command_type": "restart_stage",
        "expected_state_version": snapshot["run"]["state_version"],
        "payload": payload,
    }
    response = client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=body)
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "applied"
    duplicate = client.post(f"/api/runs/{run['id']}/commands", headers=headers, json=body)
    assert duplicate.json() == response.json()
    with factory() as session:
        row = session.get(Run, run["id"])
        assert json.loads(row.resume_target_json)["blockers"] == []
        assert row.waiting_reason_json is None
        assert session.get(StepExecution, agent_execution_id).status == "succeeded"
        assert session.get(StepAttempt, old_attempt_id).status == "failed"
    result = run_now(run, factory, settings)
    assert result.final_state == "completed", result
    assert len(calls) == 1 and len(messages) == 2
    assert command(repository, "rev-list", "--count", "HEAD") == "2"
    assert command(repository, "show", "HEAD:src/a.txt") == "finished work"
    assert command(repository, "status", "--porcelain") == ""
    with factory() as session:
        executions = list(
            session.scalars(select(StepExecution).where(StepExecution.node_id == "commit"))
        )
        assert len(executions) == 2
        assert session.get(StepAttempt, old_attempt_id).status == "failed"
        assert session.get(StepExecution, agent_execution_id).status == "succeeded"


@pytest.mark.parametrize("blocker", ["intent", "running", "succeeded", "process_active"])
def test_git_restart_rejects_dispatched_or_active_commit(
    authenticated, repository, settings, monkeypatch, blocker
):
    run, factory, calls, messages = failed_commit_run(
        authenticated, repository, settings, monkeypatch
    )
    payload = stage_payload(factory, run, "commit")
    with factory() as session:
        row = session.get(Run, run["id"])
        if blocker == "intent":
            artifacts.record_artifact(
                session,
                run["id"],
                artifacts.ArtifactPayload("git_intent", body={"operation_id": "dispatched"}),
                step_execution_id=payload["execution_id"],
                step_attempt_id=row.current_attempt_id,
            )
        elif blocker == "running":
            row.state = "running"
        elif blocker == "succeeded":
            session.get(StepExecution, payload["execution_id"]).status = "succeeded"
        session.commit()
    if blocker == "process_active":
        monkeypatch.setattr(
            "agents_ide.worker.processes.stored_processes_stopped", lambda *_: False
        )
    client, headers = authenticated
    before = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    node = next(node for node in before["observation"]["nodes"] if node["id"] == "commit")
    if blocker != "process_active":
        assert node["restart_blocked_reason"]
    response = run_command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "stage_restart_unavailable"
    after = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    assert after["run"]["state_version"] == before["run"]["state_version"]
    assert after["observation"]["current_execution_id"] == payload["execution_id"]
    assert len(calls) == len(messages) == 1
    assert command(repository, "rev-list", "--count", "HEAD") == "1"


@pytest.mark.parametrize("has_intent", [False, True])
def test_restart_recovers_git_stage_hidden_by_legacy_diff_limit(
    authenticated, repository, settings, monkeypatch, has_intent
):
    run, factory, calls, messages = failed_commit_run(
        authenticated, repository, settings, monkeypatch
    )
    payload = stage_payload(factory, run, "commit")
    with factory() as session:
        row = session.get(Run, run["id"])
        attempt = session.get(StepAttempt, row.current_attempt_id)
        attempt.error_code = "commit_diff_too_large"
        runtime = json.loads(row.runtime_json)
        runtime["invalidated_executions"] = [payload["execution_id"]]
        runtime.pop("waiting_reason", None)
        runtime.pop("waiting_code", None)
        row.runtime_json = json.dumps(runtime)
        row.current_execution_id = row.current_attempt_id = None
        row.resume_target_json = json.dumps(
            {"action": "dispatch_next", "node_id": "commit", "blockers": ["configuration_invalid"]}
        )
        session.get(StepExecution, payload["execution_id"]).status = "interrupted"
        if has_intent:
            artifacts.record_artifact(
                session,
                run["id"],
                artifacts.ArtifactPayload("git_intent", body={}),
                step_execution_id=payload["execution_id"],
                step_attempt_id=attempt.id,
            )
        session.commit()
    client, headers = authenticated
    snapshot = client.get(f"/api/runs/{run['id']}/snapshot", headers=headers).json()
    node = next(node for node in snapshot["observation"]["nodes"] if node["id"] == "commit")
    assert node["execution_id"] == payload["execution_id"]
    if has_intent:
        assert node["restart_blocked_reason"]
        assert run_command(authenticated, run, "restart_stage", payload).status_code == 409
        assert len(calls) == len(messages) == 1
        return
    assert node["restart_blocked_reason"] is None
    response = run_command(authenticated, run, "restart_stage", payload)
    assert response.status_code == 200, response.text
    assert run_now(run, factory, settings).final_state == "completed"
    assert len(calls) == 1 and len(messages) == 2
    assert command(repository, "rev-list", "--count", "HEAD") == "2"
