"""Review real protected changes, accept exact snapshots, then resume without replay."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage5_review import command as run_command
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture
from test_worktree_runs import WritingAgent, binding_for, claim, start

from agents_ide.domain.common import new_id, to_json
from agents_ide.engine import git_commit as git
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import ArtifactManifest, Run, StepAttempt
from agents_ide.services import git_changes

repository = repository_fixture


@pytest.fixture
def blocked(authenticated, repository, settings, monkeypatch):
    (repository / ".gitignore").write_text("*.cache\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore local files")
    (repository / "review-check.log").write_text("previous log\n")
    (repository / ".env").write_text("PASSWORD=private-original\n")

    class ChangedAgent(WritingAgent):
        def run(self, request):
            result = super().run(request)
            if request.role != "verifier":
                root = Path(request.workspace_path)
                (root / "review-check.log").write_text("updated log\n")
                (root / ".env").write_text("PASSWORD=private-current\n")
                (root / "README.md").write_text("updated document\n")
            return result

    project, binding = binding_for(authenticated, repository, mode="project")
    run = start(authenticated, project, binding)
    factory = authenticated[0].app.state.session_factory
    agent = ChangedAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "external_change_detected"
    return run, factory, agent


def review(authenticated, run):
    response = authenticated[0].get(f"/api/runs/{run['id']}/git-changes")
    assert response.status_code == 200, response.text
    return response.json()


def decision(comparison, *paths):
    return {
        "git_changes": {
            "comparison_id": comparison["comparison_id"],
            "paths": list(paths),
            "acknowledge_risk": True,
        }
    }


def test_compare_accept_selected_audit_idempotency_and_resume(
    authenticated, repository, settings, blocked
):
    run, factory, agent = blocked
    head, index = command(repository, "rev-parse", "HEAD"), git.index_hash(repository)
    before_files = {
        path: (repository / path).read_bytes() for path in (".env", "README.md", "review-check.log")
    }
    comparison = review(authenticated, run)
    assert comparison["can_accept"]
    files = {item["path"]: item for item in comparison["changes"]}
    assert files["README.md"]["risk"] == "low"
    assert files["README.md"]["comparison"] == "text"
    assert "-keep" in files["README.md"]["diff"]
    assert "+updated document" in files["README.md"]["diff"]
    assert files["review-check.log"]["risk"] == "unknown"
    assert files["review-check.log"]["comparison"] == "metadata_only"
    assert files[".env"]["risk"] == "high"
    assert files[".env"]["comparison"] == "hidden"
    assert "private-" not in json.dumps(comparison)
    payload = decision(comparison, "review-check.log", ".env")
    response = run_command(authenticated, run, "resolve", payload, "accept-two")
    assert response.status_code == 200, response.text
    assert response.json()["response"]["remaining_changes"] == 1
    # Retry the identical request after a lost response: a single durable decision.
    replay = authenticated[0].post(
        f"/api/runs/{run['id']}/commands",
        headers=authenticated[1],
        json={
            "command_id": "accept-two",
            "command_type": "resolve",
            "expected_state_version": comparison["state_version"],
            "payload": payload,
        },
    )
    assert replay.json() == response.json()
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.state == "waiting_input"
        assert session.get(StepAttempt, saved.current_attempt_id).status == "unknown"
        (audit,) = list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == saved.id,
                    ArtifactManifest.schema_type == "git_changes_accepted",
                )
            )
        )
        body = json.loads(audit.body_json)
        assert {item["path"] for item in body["changes"]} == {".env", "review-check.log"}
        assert all(item["before"] != item["after"] for item in body["changes"])
        assert "private-" not in audit.body_json
    assert git.index_hash(repository) == index
    assert command(repository, "rev-parse", "HEAD") == head
    assert {path: (repository / path).read_bytes() for path in before_files} == before_files
    # Unselected protected changes remain guarded.
    comparison = review(authenticated, run)
    assert [item["path"] for item in comparison["changes"]] == ["README.md"]
    response = run_command(authenticated, run, "resolve", decision(comparison, "README.md"))
    assert response.status_code == 200, response.text
    current = authenticated[0].get(f"/api/runs/{run['id']}").json()
    assert current["waiting_reason"]["resolution_schema"]["git_changes"]["accepted"]
    assert run_command(authenticated, run, "resume").status_code == 200
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    current = authenticated[0].get(f"/api/runs/{run['id']}").json()
    assert current["waiting_reason"] is None
    assert len(agent.paths) == 1
    assert command(repository, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert command(repository, "show", "HEAD:README.md") == "keep"
    assert (repository / "review-check.log").read_bytes() == before_files["review-check.log"]


def test_stale_comparison_and_invalid_decisions_are_rejected(authenticated, repository, blocked):
    run, factory, _ = blocked
    comparison = review(authenticated, run)
    with factory() as session:
        old_runtime = session.get(Run, run["id"]).runtime_json
    (repository / "review-check.log").write_text("changed after review\n")
    response = run_command(authenticated, run, "resolve", decision(comparison, "review-check.log"))
    assert response.status_code == 409, response.text
    assert response.json()["code"] == "git_comparison_stale"
    current = review(authenticated, run)
    for paths in [("../README.md",), ("src/a.txt",), ("README.md", "README.md")]:
        assert (
            run_command(authenticated, run, "resolve", decision(current, *paths)).status_code == 422
        )
    payload = decision(current, "README.md")
    payload["git_changes"]["acknowledge_risk"] = False
    assert run_command(authenticated, run, "resolve", payload).status_code == 422
    with factory() as session:
        assert session.get(Run, run["id"]).runtime_json == old_runtime
    assert authenticated[0].post(f"/api/runs/{run['id']}/commands", json={}).status_code == 403


@pytest.mark.parametrize("blocker", ["head", "index", "index_lock", "hooks", "process", "intent"])
def test_acceptance_never_bypasses_other_git_guards(
    authenticated, repository, blocked, monkeypatch, blocker
):
    run, factory, _ = blocked
    if blocker == "head":
        command(repository, "commit", "--allow-empty", "-qm", "external")
    elif blocker == "index":
        command(repository, "add", "README.md")
    elif blocker == "index_lock":
        (repository / ".git/index.lock").write_text("")
    elif blocker == "hooks":
        command(repository, "config", "commit.gpgsign", "true")
    elif blocker == "process":
        monkeypatch.setattr(git_changes, "stored_processes_stopped", lambda *_: False)
    else:
        with factory.begin() as session:
            saved = session.get(Run, run["id"])
            session.add(
                ArtifactManifest(
                    id=new_id(),
                    run_id=saved.id,
                    step_execution_id=saved.current_execution_id,
                    step_attempt_id=saved.current_attempt_id,
                    schema_type="git_intent",
                    source_kind="test",
                    byte_length=2,
                    content_hash="0" * 64,
                    body_json=to_json({}),
                )
            )
    comparison = review(authenticated, run)
    assert not comparison["can_accept"]
    assert comparison["blockers"]
    response = run_command(authenticated, run, "resolve", decision(comparison, "README.md"))
    assert response.status_code == 409, response.text


def test_assistant_gets_assessment_without_file_text(authenticated, blocked, settings):
    from agents_ide.services.assistance import collect_context
    from agents_ide.services.assistance_catalog import AssistanceTarget

    run, factory, _ = blocked
    with factory() as session:
        context = collect_context(
            session,
            settings,
            AssistanceTarget(zone="project", project_id=run["project_id"]),
            inspect_workspace=True,
        )
        finding = context.findings[0]
        assert finding.evidence["git_acceptance"]["can_review"]
        review = finding.evidence["git_acceptance"]["review"]
        assert review["can_accept"]
        rules = " ".join(finding.evidence["git_policy"]["recovery_rules"])
        assert "baseline.protected" in rules
        assert "runtime.git.head" not in rules
        assert "восстановить исходное содержимое из проверенной копии" in rules
        assert all(item["diff"] is None for item in review["changes"])
        assert "updated document" not in context.model_dump_json()
        assert "private-" not in context.model_dump_json()
