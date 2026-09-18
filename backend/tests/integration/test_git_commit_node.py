"""GitCommit is independent of agent verification and can resume old waiting runs."""

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

from agents_ide.adapters.base import AgentAdapter, AgentResult, ExternalOutcome
from agents_ide.adapters.fake import FakeLLMAdapter, FakeScenario
from agents_ide.engine import stage8
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import StepExecution

repository = repository_fixture


@pytest.mark.parametrize(
    "verification_node,verdict,old_waiting_reason",
    [
        (None, None, None),
        ("agent", None, None),
        ("agent", "failed", None),
        ("agent", "passed", None),
        ("removed_agent", None, None),
        (None, None, "git_verification_required"),
        ("agent", None, "git_verification_not_passed"),
    ],
)
def test_commit_after_agent_fixes_files(
    authenticated, repository, settings, monkeypatch, verification_node, verdict, old_waiting_reason
):
    calls = []

    class FixingAgent(AgentAdapter):
        name = "fixing-agent"

        def run(self, request):
            calls.append(request)
            (Path(request.workspace_path) / "src/a.txt").write_text("checked and fixed\n")
            body = {"text": "Checked the implementation and fixed the issue."}
            if verdict:
                body["verdict"] = verdict
            return AgentResult(ExternalOutcome.SUCCEEDED, json.dumps(body), body, verdict)

    agent = {
        "id": "agent",
        "type": "AgentTask",
        "config": {"prompt": "check and fix", "harness_profile_id": "PROFILE", "model": "test"},
    }
    config = {"allowlist": ["src/**"], "message": "fix: apply reviewed changes"}
    if verification_node:
        config["verification_node_id"] = verification_node
    commit = {"id": "commit", "type": "GitCommit", "config": config}
    run, factory = make_run(
        authenticated, repository.parent, graph=chain(agent, commit), execution_mode="real"
    )
    monkeypatch.setattr(
        Runner, "_build_adapters", lambda *_: (FixingAgent(), FakeLLMAdapter(FakeScenario()))
    )

    if old_waiting_reason:
        with monkeypatch.context() as old:
            old.setattr(
                stage8,
                "git_commit_node",
                lambda runner, node, visit: runner._waiting(
                    "configuration_invalid"
                    if old_waiting_reason == "git_verification_required"
                    else "missing_data",
                    {"reason": old_waiting_reason},
                    visit,
                ),
            )
            assert run_now(run, factory, settings).final_state == "waiting_input"
        assert command(repository, "rev-list", "--count", "HEAD") == "1"
        response = run_command(authenticated, run, "resume")
        assert response.status_code == 200, response.text

    result = run_now(run, factory, settings)
    assert result.final_state == "completed", result
    assert len(calls) == 1
    assert command(repository, "rev-list", "--count", "HEAD") == "2"
    assert command(repository, "show", "HEAD:src/a.txt") == "checked and fixed"
    assert command(repository, "status", "--porcelain") == ""
    with factory() as session:
        execution = session.scalar(select(StepExecution).where(StepExecution.node_id == "commit"))
        body = json.loads(execution.validated_result_json)
        assert body["sha"] == command(repository, "rev-parse", "HEAD")
        if verification_node == "agent":
            source = session.scalar(select(StepExecution).where(StepExecution.node_id == "agent"))
            assert body["verification_id"] == source.id
            # A passed response from an agent that changed files becomes inconclusive.
            assert source.decision != "true"
        else:
            assert body["verification_id"] is None
