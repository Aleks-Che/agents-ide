import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage4_review import make_run
from test_stage5_review import run_now
from test_stage7_review import chain
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture

from agents_ide.adapters.base import AgentResult, ExternalOutcome, LLMResult
from agents_ide.adapters.llm_http import HttpLLMAdapter
from agents_ide.engine import artifacts
from agents_ide.engine.runner import Runner
from agents_ide.errors import AppError
from agents_ide.persistence.models import (
    ArtifactManifest,
    CommandJournal,
    ProviderConnection,
    Run,
    StepAttempt,
    StepExecution,
)

repository = repository_fixture


@pytest.fixture
def blocked_connection(authenticated, repository, settings, monkeypatch):
    return prepare_blocked_connection(authenticated, repository, settings, monkeypatch)


@pytest.fixture
def connection_after_git_guard(authenticated, repository, settings, monkeypatch):
    return prepare_blocked_connection(
        authenticated, repository, settings, monkeypatch, guard_first=True
    )


def prepare_blocked_connection(
    authenticated, repository, settings, monkeypatch, *, guard_first=False
):
    calls, messages = [], []
    if guard_first:
        (repository / ".gitignore").write_text("*.log\n")
        command(repository, "add", ".gitignore")
        command(repository, "commit", "-qm", "ignore review logs")
        (repository / "frontend").mkdir()
        (repository / "frontend/review-check.log").write_text("previous review\n")
    run, factory = make_run(
        authenticated,
        repository.parent,
        execution_mode="real",
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
    )

    class Agent:
        def run(self, request):
            calls.append(request)
            (Path(request.workspace_path) / "src/a.txt").write_text("finished work\n")
            if guard_first:
                (Path(request.workspace_path) / "frontend/review-check.log").write_text(
                    "updated review\n"
                )
            else:
                with factory() as session:
                    resource = session.scalar(select(ProviderConnection))
                    resource.version += 1
                    resource.name = "Renamed connection"
                    session.commit()
            return AgentResult(ExternalOutcome.SUCCEEDED, "Done", {"text": "Done"}, None)

    def generate(self, request):
        messages.append(request)
        return LLMResult(ExternalOutcome.SUCCEEDED, "fix: finish work", None, None)

    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (Agent(), None))
    monkeypatch.setattr(HttpLLMAdapter, "run", generate)
    result = run_now(run, factory, settings)
    if guard_first:
        assert result.waiting_reason.code == "external_change_detected"
        client, headers = authenticated
        comparison = client.get(f"/api/runs/{run['id']}/git-changes").json()
        assert comparison["can_accept"]
        with factory() as session:
            session.scalar(select(ProviderConnection)).version += 1
            session.commit()
        response = client.post(
            "/api/assistance/tools/execute",
            headers=headers,
            json={
                "tool": "accept_git_files_and_resume",
                "target": {"zone": "project", "project_id": run["project_id"]},
                "run_id": run["id"],
                "comparison_id": comparison["comparison_id"],
                "expected_state_version": comparison["state_version"],
                "paths": ["frontend/review-check.log"],
                "acknowledge_risk": True,
                "confirm_resume": True,
            },
        )
        assert response.status_code == 200, response.text
        result = run_now(run, factory, settings)
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "configuration_invalid"
    assert result.waiting_reason.details["reason"] == "resource_changed"
    assert not messages
    return run, factory, calls, messages


def test_connection_can_continue_after_accepting_protected_file(
    authenticated,
    connection_after_git_guard,
    settings,
    repository,
):
    run, factory, calls, messages = connection_after_git_guard
    client, headers = authenticated
    review, payload = review_and_payload(authenticated, run)
    assert review["can_accept"], review["blockers"]
    with factory() as session:
        row = session.get(Run, run["id"])
        old_attempt_id = row.current_attempt_id
        assert old_attempt_id is not None
        assert review["previous_guard_attempt_id"] == old_attempt_id
        attempt = session.get(StepAttempt, old_attempt_id)
        history = (
            attempt.status,
            attempt.error_code,
            attempt.finished_at,
            attempt.external_outcome,
        )
        git_state = json.loads(row.runtime_json)["git"]
    head = command(repository, "rev-parse", "HEAD")
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert (
        client.post("/api/assistance/tools/execute", headers=headers, json=payload).json()
        == response.json()
    )
    with factory() as session:
        assert json.loads(session.get(Run, run["id"]).runtime_json)["git"] == git_state
    result = run_now(run, factory, settings)
    assert result.final_state == "completed", result
    assert len(calls) == len(messages) == 1
    assert command(repository, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert command(repository, "show", "HEAD:src/a.txt") == "finished work"
    assert command(repository, "ls-files", "frontend/review-check.log") == ""
    assert (repository / "frontend/review-check.log").read_text() == "updated review\n"
    with factory() as session:
        attempt = session.get(StepAttempt, old_attempt_id)
        assert (
            attempt.status,
            attempt.error_code,
            attempt.finished_at,
            attempt.external_outcome,
        ) == history


@pytest.mark.parametrize(
    "blocker", ["not_authorized", "unfinished", "non_guard_error", "intent", "newer_attempt"]
)
def test_previous_git_attempt_does_not_bypass_connection_recovery_guards(
    authenticated,
    connection_after_git_guard,
    blocker,
):
    from agents_ide.domain.common import new_id, to_json, utc_now

    run, factory, _, _ = connection_after_git_guard
    client, headers = authenticated
    with factory() as session:
        row = session.get(Run, run["id"])
        attempt = session.get(StepAttempt, row.current_attempt_id)
        if blocker == "not_authorized":
            runtime = json.loads(row.runtime_json)
            runtime["retry_authorized_attempts"] = []
            row.runtime_json = to_json(runtime)
        elif blocker == "unfinished":
            attempt.finished_at = None
        elif blocker == "non_guard_error":
            attempt.error_code = "network_error"
        elif blocker == "intent":
            artifacts.record_artifact(
                session,
                row.id,
                artifacts.ArtifactPayload("git_intent", body={}),
                step_execution_id=row.current_execution_id,
            )
        else:
            session.add(
                StepAttempt(
                    id=new_id(),
                    execution_id=row.current_execution_id,
                    attempt_index=attempt.attempt_index + 1,
                    status="succeeded",
                    finished_at=utc_now(),
                )
            )
        session.commit()
        before = row.runtime_json
    review, payload = review_and_payload(authenticated, run)
    assert not review["can_accept"] and review["blockers"]
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 409, response.text
    with factory() as session:
        assert session.get(Run, run["id"]).runtime_json == before
        assert not session.scalar(
            select(ArtifactManifest.id).where(
                ArtifactManifest.schema_type == "commit_connection_accepted"
            )
        )


def review_and_payload(authenticated, run):
    client, _ = authenticated
    response = client.get(f"/api/assistance/runs/{run['id']}/commit-connection")
    assert response.status_code == 200, response.text
    review = response.json()
    return review, {
        "tool": "refresh_commit_connection_and_resume",
        "target": {"zone": "project", "project_id": run["project_id"]},
        "run_id": run["id"],
        "comparison_id": review["comparison_id"],
        "expected_state_version": review["state_version"],
        "acknowledge_risk": True,
        "confirm_resume": True,
    }


def test_diagnosis_offers_connection_tool_without_executing_it(
    authenticated,
    blocked_connection,
    monkeypatch,
):
    run, factory, _, _ = blocked_connection
    client, headers = authenticated
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Assistant model",
            "base_url": "http://127.0.0.1:9/v1",
            "manual_models": ["test"],
        },
    ).json()
    requests = []

    def respond(self, request):
        requests.append(request)
        return LLMResult(ExternalOutcome.SUCCEEDED, "Откройте инструмент помощника.", None, None)

    monkeypatch.setattr(HttpLLMAdapter, "run", respond)
    response = client.post(
        "/api/assistance/messages",
        headers=headers,
        json={
            "target": {"zone": "project", "project_id": run["project_id"]},
            "connection_id": connection["id"],
            "model_id": "test",
            "message": "Как исправить?",
        },
    )
    assert response.status_code == 200, response.text
    evidence = requests[0].context_package["evidence"]
    assert evidence["tools"][0]["name"] == "refresh_commit_connection_and_resume"
    review = evidence["findings"][0]["evidence"]["commit_connection"]["review"]
    assert review["can_accept"] and review["execution_settings_match"]
    assert "refresh_commit_connection_and_resume" in requests[0].prompt
    assert "secret_reference" not in json.dumps(evidence)
    with factory() as session:
        assert session.get(Run, run["id"]).state == "waiting_input"
        assert not session.scalar(select(CommandJournal.id))


def test_provider_changed_again_after_acceptance_is_still_checked_by_worker(
    authenticated,
    blocked_connection,
    settings,
):
    run, factory, calls, messages = blocked_connection
    client, headers = authenticated
    _, payload = review_and_payload(authenticated, run)
    assert (
        client.post("/api/assistance/tools/execute", headers=headers, json=payload).status_code
        == 200
    )
    with factory() as session:
        session.scalar(select(ProviderConnection)).version += 1
        session.commit()
    result = run_now(run, factory, settings)
    assert result.waiting_reason.details["reason"] == "resource_changed"
    assert len(calls) == 1 and not messages
    review, payload = review_and_payload(authenticated, run)
    assert review["expected_version"] == 2 and review["current_version"] == 3
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert run_now(run, factory, settings).final_state == "completed"
    assert len(calls) == len(messages) == 1


def test_assistant_refreshes_only_commit_revision_and_continues_once(
    authenticated,
    blocked_connection,
    repository,
    settings,
):
    run, factory, calls, messages = blocked_connection
    client, headers = authenticated
    review, payload = review_and_payload(authenticated, run)
    assert review["can_accept"] and review["execution_settings_match"]
    assert review["expected_version"] == 1 and review["current_version"] == 2
    assert review["changed_fields"] == []
    context = client.get("/api/assistance/context", params=payload["target"]).json()
    assert context["tools"][0]["name"] == payload["tool"]
    assert "commit_connection" in context["findings"][0]["evidence"]
    with factory() as session:
        row = session.get(Run, run["id"])
        snapshot, git = row.snapshot_json, json.loads(row.runtime_json)["git"]
        execution = row.current_execution_id
    # Generic resolve/resume cannot replace the explicit review.
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "blind-resume",
            "command_type": "resume",
            "expected_state_version": review["state_version"],
        },
    )
    assert response.status_code == 409
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "queued"
    assert (
        client.post("/api/assistance/tools/execute", headers=headers, json=payload).json()
        == response.json()
    )
    assert command(repository, "rev-list", "--count", "HEAD") == "1"
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.snapshot_json == snapshot
        assert json.loads(row.runtime_json)["git"] == git
        assert row.current_execution_id == execution
        assert row.current_attempt_id is None
        assert (
            len(
                list(
                    session.scalars(
                        select(CommandJournal).where(CommandJournal.initiator == "assistance")
                    )
                )
            )
            == 2
        )
    result = run_now(run, factory, settings)
    assert result.final_state == "completed", result
    assert len(calls) == len(messages) == 1
    assert messages[0].model_id == "test"
    assert messages[0].connection["base_url"] == "http://127.0.0.1:9/v1"
    assert command(repository, "rev-list", "--count", "HEAD") == "2"
    assert command(repository, "show", "HEAD:src/a.txt") == "finished work"
    with factory() as session:
        assert (
            len(
                list(session.scalars(select(StepExecution).where(StepExecution.node_id == "agent")))
            )
            == 1
        )


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "url",
        "secret",
        "archived",
        "intent",
        "process",
        "resume",
        "scope",
        "consent",
        "paths",
    ],
)
def test_commit_refresh_rejects_changed_or_unsafe_state_atomically(
    authenticated,
    blocked_connection,
    monkeypatch,
    change,
):
    run, factory, _, _ = blocked_connection
    client, headers = authenticated
    review, payload = review_and_payload(authenticated, run)
    with factory() as session:
        row = session.get(Run, run["id"])
        before = row.runtime_json
        resource = session.scalar(select(ProviderConnection))
        if change == "revision":
            resource.version += 1
        elif change == "url":
            resource.base_url = "https://different.invalid/v1"
        elif change == "secret":
            resource.secret_reference = "sensitive-reference"
        elif change == "archived":
            resource.archived_at = 1
        elif change == "intent":
            artifacts.record_artifact(
                session,
                row.id,
                artifacts.ArtifactPayload("git_intent", body={}),
                step_execution_id=row.current_execution_id,
            )
        session.commit()
    if change == "process":
        monkeypatch.setattr(
            "agents_ide.worker.processes.stored_processes_stopped", lambda *_: False
        )
    elif change == "resume":

        def fail(*_):
            raise AppError("test_resume_failure", "No resume", 409)

        monkeypatch.setattr("agents_ide.services.run_controls.ensure_queue", fail)
    elif change == "scope":
        payload["target"]["project_id"] = "other-project"
    elif change == "consent":
        payload["acknowledge_risk"] = False
    elif change == "paths":
        payload["paths"] = ["irrelevant.txt"]
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code in {409, 422}, response.text
    with factory() as session:
        row = session.get(Run, run["id"])
        assert row.runtime_json == before
        assert row.state == "waiting_input" and row.state_version == review["state_version"]
        assert not session.scalar(
            select(ArtifactManifest.id).where(
                ArtifactManifest.schema_type == "commit_connection_accepted"
            )
        )
        assert not session.scalar(
            select(CommandJournal.id).where(CommandJournal.initiator == "assistance")
        )
    if change in {"url", "secret", "archived"}:
        updated, _ = review_and_payload(authenticated, run)
        assert not updated["can_accept"] and updated["blockers"]
        assert "sensitive-reference" not in json.dumps(updated)
