import json

import pytest
from sqlalchemy import select
from test_git_changes_acceptance import blocked as blocked_fixture
from test_git_changes_acceptance import review
from test_git_head_acceptance import boundary as boundary_fixture
from test_git_head_acceptance import repository as repository_fixture
from test_stage8_git_plan import command
from test_worktree_runs import claim

from agents_ide.adapters.base import ExternalOutcome, LLMResult
from agents_ide.engine import git_commit as git
from agents_ide.errors import AppError
from agents_ide.persistence.models import ArtifactManifest, CommandJournal, Run
from agents_ide.services import assistance, assistance_tools

boundary = boundary_fixture
blocked = blocked_fixture
repository = repository_fixture


@pytest.mark.parametrize("recovery_kind", ["head", "protected_files"])
def test_model_receives_tool_capability_but_its_answer_cannot_execute_it(
    authenticated, request, monkeypatch, recovery_kind
):
    run, factory, _ = request.getfixturevalue("boundary" if recovery_kind == "head" else "blocked")
    client, headers = authenticated
    connection = client.post(
        "/api/connections",
        headers=headers,
        json={
            "name": "Tool test",
            "provider_kind": "openai_compatible",
            "base_url": "http://127.0.0.1:9999/v1",
            "manual_models": ["test-model"],
        },
    ).json()
    seen = []

    def model(self, request):
        seen.append(request)
        return LLMResult(
            ExternalOutcome.SUCCEEDED, "Используйте инструмент в карточке.", None, None
        )

    monkeypatch.setattr(assistance.HttpLLMAdapter, "run", model)
    response = client.post(
        "/api/assistance/messages",
        headers=headers,
        json={
            "target": {"zone": "project", "project_id": run["project_id"]},
            "connection_id": connection["id"],
            "model_id": "test-model",
            "message": "Объясни проблему и предложи решение",
            "history": [],
        },
    )
    assert response.status_code == 200, response.text
    expected_tool = (
        "accept_git_head_and_resume" if recovery_kind == "head" else "accept_git_files_and_resume"
    )
    evidence = seen[0].context_package["evidence"]
    assert evidence["tools"][0]["name"] == expected_tool
    assert expected_tool in evidence["guide"]["allowed_mutations"][0]
    assert "private-" not in json.dumps(evidence)
    assert "Решить через помощника" in seen[0].prompt
    assert response.json()["context"]["tools"][0]["requires_confirmation"]
    with factory() as session:
        assert session.get(Run, run["id"]).state == "waiting_input"
        assert not list(
            session.scalars(select(CommandJournal).where(CommandJournal.run_id == run["id"]))
        )


def tool_request(authenticated, run):
    comparison = review(authenticated, run)
    return {
        "tool": "accept_git_head_and_resume"
        if comparison["kind"] == "head"
        else "accept_git_files_and_resume",
        **(
            {"paths": [item["path"] for item in comparison["changes"]]}
            if comparison["kind"] != "head"
            else {}
        ),
        "target": {"zone": "project", "project_id": run["project_id"]},
        "run_id": run["id"],
        "comparison_id": comparison["comparison_id"],
        "expected_state_version": comparison["state_version"],
        "acknowledge_risk": True,
        "confirm_resume": True,
    }


@pytest.mark.parametrize("selection", ["log_only", "multiple_files"])
def test_assistant_accepts_protected_files_and_resumes_git_commit(
    authenticated, blocked, repository, settings, selection
):
    run, factory, agent = blocked
    if selection == "log_only":
        (repository / ".env").write_text("PASSWORD=private-original\n")
        (repository / "README.md").write_text("keep\n")
    client, headers = authenticated
    payload = tool_request(authenticated, run)
    if selection == "log_only":
        assert payload["paths"] == ["review-check.log"]
    context = client.get("/api/assistance/context", params=payload["target"]).json()
    assert context["tools"][0]["name"] == "accept_git_files_and_resume"
    assert "Принять выбранные изменения и продолжить" in context["findings"][0]["next_step"]
    head, index = command(repository, "rev-parse", "HEAD"), git.index_hash(repository)
    contents = {path: (repository / path).read_bytes() for path in payload["paths"]}
    with factory() as session:
        before = json.loads(session.get(Run, run["id"]).runtime_json)["git"]
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "queued"
    assert set(response.json()["accepted_paths"]) == set(payload["paths"])
    assert "изменения защищённых файлов" in response.json()["content"]
    assert "private-" not in response.text
    assert (
        client.post("/api/assistance/tools/execute", headers=headers, json=payload).json()
        == response.json()
    )
    assert command(repository, "rev-parse", "HEAD") == head
    assert git.index_hash(repository) == index
    assert {path: (repository / path).read_bytes() for path in contents} == contents
    with factory() as session:
        state = json.loads(session.get(Run, run["id"]).runtime_json)["git"]
        assert state["head"] == before["head"]
        assert state["allowlist"] == before["allowlist"]
        assert state["baseline"]["protected"] != before["baseline"]["protected"]
        records = list(
            session.scalars(select(CommandJournal).where(CommandJournal.run_id == run["id"]))
        )
        assert len(records) == 2 and all(record.initiator == "assistance" for record in records)
        audits = list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == run["id"],
                    ArtifactManifest.schema_type == "git_changes_accepted",
                )
            )
        )
        assert len(audits) == 1
        assert {item["path"] for item in json.loads(audits[0].body_json)["changes"]} == set(
            payload["paths"]
        )
    assert client.get("/api/assistance/context", params=payload["target"]).json()["tools"] == []
    # Actually run the queued server stage; do not equate queueing with a commit.
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert len(agent.paths) == 1
    assert command(repository, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert command(repository, "ls-files", "review-check.log") == ""
    assert {path: (repository / path).read_bytes() for path in contents} == contents


@pytest.mark.parametrize(
    "failure",
    [
        "stale_file",
        "partial_selection",
        "head_drift",
        "resume_failure",
        "unexpected_path",
        "wrong_tool",
        "empty_paths",
        "duplicate_paths",
    ],
)
def test_file_tool_rejects_invalid_or_partial_acceptance_without_mutation(
    authenticated, blocked, repository, monkeypatch, failure
):
    run, factory, _ = blocked
    payload = tool_request(authenticated, run)
    with factory() as session:
        before = session.get(Run, run["id"]).runtime_json
    if failure == "stale_file":
        (repository / "review-check.log").write_text("changed after confirmation\n")
    elif failure == "partial_selection":
        payload["paths"] = ["review-check.log"]
    elif failure == "head_drift":
        command(repository, "commit", "--allow-empty", "-qm", "external head")
    elif failure == "resume_failure":
        original = assistance_tools.submit_command

        def reject_resume(session, run_id, request, **kwargs):
            if request.command_type == "resume":
                raise AppError("test_resume_blocked", "Продолжение заблокировано", 409)
            return original(session, run_id, request, **kwargs)

        monkeypatch.setattr(assistance_tools, "submit_command", reject_resume)
    elif failure == "unexpected_path":
        payload["paths"] = ["src/a.txt"]
    elif failure == "wrong_tool":
        payload["tool"] = "accept_git_head_and_resume"
        payload.pop("paths")
    elif failure == "empty_paths":
        payload["paths"] = []
    else:
        payload["paths"] = ["review-check.log", "review-check.log"]
    response = authenticated[0].post(
        "/api/assistance/tools/execute", headers=authenticated[1], json=payload
    )
    assert response.status_code == (
        422
        if failure
        in {
            "unexpected_path",
            "wrong_tool",
            "empty_paths",
            "duplicate_paths",
        }
        else 409
    ), response.text
    if failure == "partial_selection":
        assert response.json()["code"] == "assistance_git_changes_remaining"
        assert response.json()["details"]["remaining_changes"] == 2
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.runtime_json == before
        assert saved.state == "waiting_input"
        assert saved.state_version == payload["expected_state_version"]
        assert not list(
            session.scalars(select(CommandJournal).where(CommandJournal.run_id == saved.id))
        )
        assert not list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == saved.id,
                    ArtifactManifest.schema_type == "git_changes_accepted",
                )
            )
        )


def test_assistant_executes_confirmed_tool_once_and_preserves_workspace(
    authenticated, boundary, repository
):
    client, headers = authenticated
    run, factory, _ = boundary
    payload = tool_request(authenticated, run)
    context = client.get("/api/assistance/context", params=payload["target"]).json()
    assert context["tools"][0]["name"] == payload["tool"]
    assert context["tools"][0]["run_id"] == run["id"]
    assert "accept_git_head_and_resume" in context["guide"]["allowed_mutations"][0]
    with factory() as session:
        before = json.loads(session.get(Run, run["id"]).runtime_json)["git"]
    head, index = command(repository, "rev-parse", "HEAD"), git.index_hash(repository)
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "queued"
    replay = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert replay.json() == response.json()
    assert command(repository, "rev-parse", "HEAD") == head
    assert git.index_hash(repository) == index
    assert (repository / "src/draft.txt").read_text() == "unfinished work\n"
    with factory() as session:
        saved = session.get(Run, run["id"])
        state = json.loads(saved.runtime_json)["git"]
        assert saved.state_version == payload["expected_state_version"] + 2
        assert state["baseline"] == before["baseline"]
        assert state["head"] == head
        commands = list(
            session.scalars(select(CommandJournal).where(CommandJournal.run_id == run["id"]))
        )
        assert [c.command_type for c in commands] == ["resolve", "resume"]
        assert all(c.initiator == "assistance" for c in commands)
        assert (
            len(
                list(
                    session.scalars(
                        select(ArtifactManifest).where(
                            ArtifactManifest.run_id == run["id"],
                            ArtifactManifest.schema_type == "git_head_accepted",
                        )
                    )
                )
            )
            == 1
        )
    refreshed = client.get("/api/assistance/context", params=payload["target"]).json()
    assert refreshed["tools"] == []


@pytest.mark.parametrize("failure", ["stale_head", "resume_failure"])
def test_failed_tool_does_not_leave_partial_acceptance(
    authenticated, boundary, repository, monkeypatch, failure
):
    run, factory, _ = boundary
    payload = tool_request(authenticated, run)
    with factory() as session:
        before = session.get(Run, run["id"]).runtime_json
    if failure == "stale_head":
        command(repository, "commit", "--allow-empty", "-qm", "changed after review")
    else:
        original = assistance_tools.submit_command

        def reject_resume(session, run_id, request, **kwargs):
            if request.command_type == "resume":
                raise AppError("test_resume_blocked", "Продолжение заблокировано", 409)
            return original(session, run_id, request, **kwargs)

        monkeypatch.setattr(assistance_tools, "submit_command", reject_resume)
    response = authenticated[0].post(
        "/api/assistance/tools/execute", headers=authenticated[1], json=payload
    )
    assert response.status_code == 409, response.text
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.runtime_json == before
        assert saved.state == "waiting_input"
        assert saved.state_version == payload["expected_state_version"]
        assert not list(
            session.scalars(select(CommandJournal).where(CommandJournal.run_id == saved.id))
        )
        assert not list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == saved.id,
                    ArtifactManifest.schema_type == "git_head_accepted",
                )
            )
        )


@pytest.mark.parametrize("invalid", ["scope", "chat", "consent", "resume_consent", "extra", "csrf"])
def test_tool_requires_scope_and_concrete_confirmation(authenticated, boundary, invalid):
    run, factory, _ = boundary
    payload = tool_request(authenticated, run)
    headers = authenticated[1]
    if invalid == "scope":
        payload["target"]["project_id"] = "other-project"
    elif invalid == "chat":
        payload["target"] = {**payload["target"], "zone": "chat", "chat_id": "other-chat"}
    elif invalid == "consent":
        payload["acknowledge_risk"] = False
    elif invalid == "resume_consent":
        payload.pop("confirm_resume")
    elif invalid == "extra":
        payload["command"] = "arbitrary command"
    else:
        headers = {}
    response = authenticated[0].post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == (403 if invalid == "csrf" else 422), response.text
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.state == "waiting_input"
        assert saved.state_version == payload["expected_state_version"]
