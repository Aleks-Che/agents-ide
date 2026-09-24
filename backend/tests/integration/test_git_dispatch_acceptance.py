"""Nonignored files outside the allowlist before GitCommit has an attempt."""

import hashlib
import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_assistance_tools import tool_request
from test_git_changes_acceptance import decision, review
from test_stage5_review import command as run_command
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture
from test_worktree_runs import WritingAgent, binding_for, claim, start

from agents_ide.domain.common import new_id, to_json
from agents_ide.engine import git_commit as git
from agents_ide.engine.runner import Runner
from agents_ide.engine.stage8 import prepare_git
from agents_ide.persistence.models import (
    ArtifactManifest,
    CommandJournal,
    Run,
    StepAttempt,
    StepExecution,
)
from agents_ide.services import git_changes
from agents_ide.services.assistance import collect_context
from agents_ide.services.assistance_catalog import AssistanceTarget

repository = repository_fixture
CACHE_PATHS = [
    f"wiki-doc/wiki-doc/scripts/__pycache__/{name}.cpython-312.pyc"
    for name in ("ddl", "sql_ast", "sql_gp")
]


@pytest.fixture
def before_commit(authenticated, repository, settings, monkeypatch, request):
    legacy_ignored = getattr(request, "param", None) == "legacy_ignored"
    (repository / ".gitignore").write_text("*.pyc\n" if legacy_ignored else "*.log\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore logs")
    for path in CACHE_PATHS:
        file = repository / path
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(b"\0previous cache")

    class CacheWritingAgent(WritingAgent):
        def run(self, request):
            result = super().run(request)
            if request.role != "verifier":
                for path in CACHE_PATHS:
                    (Path(request.workspace_path) / path).write_bytes(b"\0updated Python cache")
            return result

    project, binding = binding_for(authenticated, repository, mode="project", allowlist=["src/**"])
    run = start(authenticated, project, binding)
    factory = authenticated[0].app.state.session_factory
    agent = CacheWritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    runner = claim(factory, settings, run)
    controls = runner._controls

    def check_before_commit(**kwargs):
        if runner.runtime.get("next_node_id") == "commit":
            # The same pre-dispatch guard that runs when a worker starts/resumes.
            prepare_git(runner)
        return controls(**kwargs)

    runner._controls = check_before_commit
    with monkeypatch.context() as old_version:
        if legacy_ignored:
            original_manifest = git.file_manifest

            def old_manifest(workspace):
                manifest = original_manifest(workspace)
                for path in CACHE_PATHS:
                    data = (workspace / path).read_bytes()
                    manifest[path] = {
                        "sha256": hashlib.sha256(data).hexdigest(),
                        "size": len(data),
                        "mode": "100644",
                        "ignored": True,
                    }
                return manifest

            def old_protected_states(workspace, previous, manifest, allowed):
                return previous, {
                    path: value
                    for path, value in manifest.items()
                    if path in previous or not git.is_path_allowed(path, allowed)
                }

            old_version.setattr(git, "file_manifest", old_manifest)
            old_version.setattr(git, "protected_states", old_protected_states)
        result = runner.execute(run["id"])
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "external_change_detected"
    assert result.waiting_reason.details["message"] == "Files outside the allowlist changed"
    with factory.begin() as session:
        saved = session.get(Run, run["id"])
        # Persist the user's dispatch_next checkpoint; prior successful stages stay in history.
        saved.current_node_id = "commit"
        saved.current_execution_id = saved.current_attempt_id = None
        target = json.loads(saved.resume_target_json)
        assert target["action"] == "dispatch_next" and target["execution_id"] is None
        assert not session.scalar(
            select(StepExecution).where(
                StepExecution.run_id == saved.id, StepExecution.node_id == "commit"
            )
        )
    return run, factory, agent


@pytest.mark.parametrize("before_commit", ["legacy_ignored"], indirect=True)
@pytest.mark.parametrize("change", ["modified", "deleted"])
def test_old_ignored_cache_stop_resumes_without_acceptance(
    authenticated, repository, settings, before_commit, change
):
    run, factory, agent = before_commit
    if change == "deleted":
        for path in CACHE_PATHS:
            (repository / path).unlink()
    comparison = review(authenticated, run)
    assert comparison["changes"] == [] and comparison["blockers"] == []
    assert not comparison["can_accept"]
    with factory() as session:
        context = collect_context(
            session,
            settings,
            AssistanceTarget(zone="project", project_id=run["project_id"]),
            inspect_workspace=True,
        )
        evidence = context.findings[0].evidence
        assert evidence["git_check"]["protected_changes"] == []
        assert evidence["git_acceptance"]["can_resume"]
        assert not context.tools
        assert "Продолжить" in context.findings[0].next_step
        assert not evidence["git_policy"]["preexisting_ignored_files_protected"]
        # Compatibility is a read-time projection, not silent baseline acceptance.
        saved = session.get(Run, run["id"])
        protected = json.loads(saved.runtime_json)["git"]["baseline"]["protected"]
        assert all(protected[path]["ignored"] for path in CACHE_PATHS)
    head = command(repository, "rev-parse", "HEAD")
    response = run_command(authenticated, run, "resume")
    assert response.status_code == 200, response.text
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert len(agent.paths) == 1
    assert command(repository, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert command(repository, "ls-files", "*__pycache__*") == ""
    with factory() as session:
        assert not list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == run["id"],
                    ArtifactManifest.schema_type == "git_changes_accepted",
                )
            )
        )


@pytest.mark.parametrize("before_commit", ["legacy_ignored"], indirect=True)
@pytest.mark.parametrize("blocker", ["head", "index", "untracked_source"])
def test_ignored_cache_does_not_bypass_other_resume_guards(
    authenticated, repository, before_commit, blocker
):
    run, _, _ = before_commit
    if blocker == "head":
        command(repository, "commit", "--allow-empty", "-qm", "external")
    elif blocker == "index":
        command(repository, "add", "src/a.txt")
    else:
        (repository / "outside.txt").write_text("important new file")
    assert run_command(authenticated, run, "resume").status_code == 409


@pytest.mark.parametrize("changed_before_worker", [False, True])
def test_assistant_accepts_cache_before_first_commit_attempt(
    authenticated, repository, settings, before_commit, changed_before_worker
):
    run, factory, agent = before_commit
    client, headers = authenticated
    comparison = review(authenticated, run)
    assert comparison["can_accept"] and comparison["blockers"] == []
    assert comparison["kind"] == "protected_files" and comparison["attempt_id"] is None
    assert {item["path"] for item in comparison["changes"]} == set(CACHE_PATHS)
    assert all(item["comparison"] == "metadata_only" for item in comparison["changes"])
    assert all(not item["before"]["ignored"] for item in comparison["changes"])
    target = AssistanceTarget(zone="project", project_id=run["project_id"])
    with factory() as session:
        context = collect_context(session, settings, target, inspect_workspace=True)
        acceptance = context.findings[0].evidence["git_acceptance"]
        assert acceptance["before_dispatch"] and not acceptance["accepted"]
        assert acceptance["review"]["can_accept"]
        assert context.tools[0].name == "accept_git_files_and_resume"
        baseline = json.loads(session.get(Run, run["id"]).runtime_json)["git"]["baseline"]
        assert baseline["allowlist"] == ["src/**"]
    assert run_command(authenticated, run, "resume").status_code == 409
    payload = tool_request(authenticated, run)
    head, index = command(repository, "rev-parse", "HEAD"), git.index_hash(repository)
    contents = {path: (repository / path).read_bytes() for path in CACHE_PATHS}
    response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "queued"
    assert (
        client.post("/api/assistance/tools/execute", headers=headers, json=payload).json()
        == response.json()
    )
    assert command(repository, "rev-parse", "HEAD") == head
    assert git.index_hash(repository) == index
    assert {path: (repository / path).read_bytes() for path in CACHE_PATHS} == contents
    with factory() as session:
        saved = session.get(Run, run["id"])
        state = json.loads(saved.runtime_json)["git"]
        assert state["accepted_changes"]["boundary_key"]
        assert state["accepted_changes"]["attempt_id"] is None
        assert state["baseline"]["allowlist"] == baseline["allowlist"]
        (audit,) = list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == saved.id,
                    ArtifactManifest.schema_type == "git_changes_accepted",
                )
            )
        )
        body = json.loads(audit.body_json)
        assert body["node_id"] == "commit" and body["attempt_id"] is None
        assert body["boundary_key"] == state["accepted_changes"]["boundary_key"]
    if changed_before_worker:
        contents[CACHE_PATHS[0]] = b"\0updated after queueing"
        (repository / CACHE_PATHS[0]).write_bytes(contents[CACHE_PATHS[0]])
    result = claim(factory, settings, run).execute(run["id"])
    if changed_before_worker:
        assert result.final_state == "waiting_input", result
        context = client.get(
            "/api/assistance/context", params=target.model_dump(exclude_none=True)
        ).json()
        assert not context["findings"][0]["evidence"]["git_acceptance"]["accepted"]
        assert context["tools"][0]["name"] == "accept_git_files_and_resume"
        payload = tool_request(authenticated, run)
        assert payload["paths"] == [CACHE_PATHS[0]]
        response = client.post("/api/assistance/tools/execute", headers=headers, json=payload)
        assert response.status_code == 200, response.text
        result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert len(agent.paths) == 1
    assert command(repository, "rev-list", "--count", f"{head}..HEAD") == "1"
    assert command(repository, "ls-files", "*__pycache__*") == ""
    assert {path: (repository / path).read_bytes() for path in CACHE_PATHS} == contents


def test_partial_or_stale_acceptance_cannot_resume_boundary(
    authenticated, repository, before_commit
):
    run, factory, _ = before_commit
    comparison = review(authenticated, run)
    response = run_command(authenticated, run, "resolve", decision(comparison, CACHE_PATHS[0]))
    assert response.status_code == 200, response.text
    assert response.json()["response"]["remaining_changes"] == 2
    assert run_command(authenticated, run, "resume").status_code == 409
    comparison = review(authenticated, run)
    assert (
        run_command(
            authenticated, run, "resolve", decision(comparison, *CACHE_PATHS[1:])
        ).status_code
        == 200
    )
    with factory() as session:
        assert git_changes.resolution_status(session.get(Run, run["id"]))["accepted"]
    (repository / CACHE_PATHS[0]).write_bytes(b"\0changed again after acceptance")
    response = run_command(authenticated, run, "resume")
    assert response.status_code == 409 and response.json()["code"] == "git_acceptance_stale"
    comparison = review(authenticated, run)
    assert comparison["can_accept"]
    assert (
        run_command(authenticated, run, "resolve", decision(comparison, CACHE_PATHS[0])).status_code
        == 200
    )
    assert run_command(authenticated, run, "resume").status_code == 200


@pytest.mark.parametrize(
    "blocker",
    [
        "head",
        "index",
        "index_lock",
        "hooks",
        "process",
        "paused_hash",
        "wrong_target",
        "next_node",
        "unknown_operation",
        "unsettled_attempt",
        "stale_file",
    ],
)
def test_boundary_tool_preserves_guards(
    authenticated, repository, before_commit, monkeypatch, blocker
):
    run, factory, _ = before_commit
    payload = tool_request(authenticated, run)
    if blocker == "head":
        command(repository, "commit", "--allow-empty", "-qm", "external")
    elif blocker == "index":
        command(repository, "add", "src/a.txt")
    elif blocker == "index_lock":
        (repository / ".git/index.lock").write_text("")
    elif blocker == "hooks":
        command(repository, "config", "commit.gpgsign", "true")
    elif blocker == "process":
        monkeypatch.setattr(git_changes, "stored_processes_stopped", lambda *_: False)
    elif blocker == "stale_file":
        (repository / CACHE_PATHS[0]).write_bytes(b"\0changed after review")
    else:
        with factory.begin() as session:
            saved = session.get(Run, run["id"])
            runtime = json.loads(saved.runtime_json)
            if blocker == "paused_hash":
                runtime["git_paused_workspace_hash"] = "wrong-checkpoint"
            elif blocker == "next_node":
                runtime["next_node_id"] = "agent"
            elif blocker == "wrong_target":
                target = json.loads(saved.resume_target_json)
                target["execution_id"] = "different-execution"
                saved.resume_target_json = to_json(target)
            elif blocker == "unknown_operation":
                waiting = json.loads(saved.waiting_reason_json)
                waiting["code"] = "unknown_external_result"
                saved.waiting_reason_json = to_json(waiting)
            else:
                execution = StepExecution(
                    id=new_id(),
                    run_id=saved.id,
                    node_id="agent",
                    visit_index=2,
                    cycle_id=0,
                    status="waiting_input",
                )
                session.add(execution)
                session.flush()
                session.add(
                    StepAttempt(
                        id=new_id(), execution_id=execution.id, attempt_index=1, status="unknown"
                    )
                )
            saved.runtime_json = to_json(runtime)
    with factory() as session:
        before = session.get(Run, run["id"]).runtime_json
    if blocker != "stale_file":
        assert not review(authenticated, run)["can_accept"]
    response = authenticated[0].post(
        "/api/assistance/tools/execute", headers=authenticated[1], json=payload
    )
    assert response.status_code == 409, response.text
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.state == "waiting_input" and saved.runtime_json == before
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
