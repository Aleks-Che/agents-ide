"""Retry pre-commit guards without replaying agents or a dispatched commit."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select
from test_stage5_review import command as run_command
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture
from test_worktree_runs import WritingAgent, binding_for, claim, start

from agents_ide.engine import git_commit as git
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import ArtifactManifest, Run, StepAttempt

repository = repository_fixture


@pytest.mark.parametrize(
    "cause", ["new_ignored", "config_changed", "outside_allowlist", "committed_hook_change"]
)
def test_resume_rechecks_git_guards_before_intent_only(
    authenticated, repository, settings, monkeypatch, cause
):
    (repository / ".gitignore").write_text(".env\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore environment")
    source_head = command(repository, "rev-parse", "HEAD")
    if cause == "committed_hook_change":
        hook = repository / ".git/hooks/pre-commit"
        hook.write_text("#!/bin/sh\necho hook >> src/a.txt\ngit add src/a.txt\n")
        hook.chmod(0o755)

    class SetupAgent(WritingAgent):
        def run(self, request):
            result = super().run(request)
            if request.role != "verifier":
                root = Path(request.workspace_path)
                if cause == "new_ignored":
                    (root / ".env").write_text("LOCAL_SETTING=test\n")
                    cache = root / ".pytest_cache"
                    cache.mkdir()
                    (cache / ".gitignore").write_text("*\n")
                    (cache / "README.md").write_text("test cache\n")
                elif cause == "outside_allowlist":
                    (root / "outside.txt").write_text("unrelated file\n")
                elif cause == "config_changed":
                    command(root, "config", "extensions.worktreeConfig", "true")
            return result

    project, binding = binding_for(authenticated, repository)
    run = start(authenticated, project, binding)
    factory = authenticated[0].app.state.session_factory
    agent = SetupAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    runner = claim(factory, settings, run)
    current_check = git.check_workspace

    def legacy_check(workspace, baseline, allowlist, **kwargs):
        manifest = current_check(workspace, baseline, allowlist, **kwargs)
        if cause == "config_changed":
            if git.git_fingerprint(workspace) != baseline.fingerprint:
                raise git.GitCommitError(
                    "external_change_detected", "Git hooks/config changed after Start"
                )
            return manifest
        protected = {
            path: item
            for path, item in manifest.items()
            if item.get("ignored") or not git.is_path_allowed(path, baseline.allowlist)
        }
        if protected != baseline.protected:
            raise git.GitCommitError(
                "external_change_detected", "Files outside the allowlist changed"
            )
        return manifest

    with monkeypatch.context() as old:
        if cause in {"new_ignored", "config_changed"}:
            old.setattr(git, "check_workspace", legacy_check)
        result = runner.execute(run["id"])
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "external_change_detected"
    root = agent.paths[0]
    with factory() as session:
        row = session.get(Run, run["id"])
        attempt_id = row.current_attempt_id
        assert session.get(StepAttempt, attempt_id).status == "unknown"
        has_intent = session.scalar(
            select(ArtifactManifest.id).where(
                ArtifactManifest.run_id == run["id"],
                ArtifactManifest.schema_type == "git_intent",
            )
        )
        assert bool(has_intent) == (cause == "committed_hook_change")
    before_resume = command(root, "rev-parse", "HEAD")
    response = run_command(authenticated, run, "resume")
    if cause == "committed_hook_change":
        assert response.status_code == 409, response.text
        assert before_resume != source_head
        assert command(root, "rev-parse", "HEAD") == before_resume
    else:
        assert response.status_code == 200, response.text
        assert before_resume == source_head
        resumed = claim(factory, settings, run).execute(run["id"])
        if cause in {"new_ignored", "config_changed"}:
            assert resumed.final_state == "completed", resumed
            assert command(root, "rev-list", "--count", f"{source_head}..HEAD") == "1"
            assert command(root, "status", "--porcelain") == ""
            if cause == "new_ignored":
                assert (root / ".env").read_text() == "LOCAL_SETTING=test\n"
        else:
            assert resumed.final_state == "waiting_input", resumed
            assert resumed.waiting_reason.code == "external_change_detected"
            assert command(root, "rev-parse", "HEAD") == source_head
        with factory() as session:
            row = session.get(Run, run["id"])
            assert attempt_id in json.loads(row.runtime_json)["retry_authorized_attempts"]
            assert session.get(StepAttempt, attempt_id).status == "unknown"
    assert agent.paths == [root]
    assert command(repository, "rev-parse", "HEAD") == source_head
    assert (repository / "src/a.txt").read_text() == "base\n"
