"""HEAD drift before a new AgentTask: review, durable decision, guarded resume."""

import json

import pytest
from sqlalchemy import select
from test_git_changes_acceptance import review
from test_stage5_review import command as run_command
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture
from test_worktree_runs import WritingAgent, binding_for, claim, start

from agents_ide.domain.common import to_json
from agents_ide.engine import git_commit as git
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import ArtifactManifest, Run
from agents_ide.services import git_changes
from agents_ide.services.assistance import collect_context
from agents_ide.services.assistance_catalog import AssistanceTarget

repository = repository_fixture


@pytest.fixture
def boundary(authenticated, repository, settings, monkeypatch):
    cache = repository / "src/__pycache__/cache.pyc"
    cache.parent.mkdir()
    cache.write_bytes(b"generated cache")
    command(repository, "add", "src/__pycache__/cache.pyc")
    command(repository, "commit", "-qm", "cache at start")
    project, binding = binding_for(authenticated, repository, mode="project")
    run = start(authenticated, project, binding)
    factory = authenticated[0].app.state.session_factory
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    runner = claim(factory, settings, run)
    controls = runner._controls

    def changed_before_agent(**kwargs):
        if runner.runtime.get("next_node_id") == "agent" and runner.runtime.get("git"):
            command(repository, "rm", "src/__pycache__/cache.pyc")
            command(repository, "commit", "-qm", "remove generated cache")
            (repository / "src/draft.txt").write_text("unfinished work\n")
            runner._workspace_check()
        return controls(**kwargs)

    runner._controls = changed_before_agent
    result = runner.execute(run["id"])
    assert result.final_state == "waiting_input", result
    # Reproduce the persisted dispatch_next checkpoint reported by the user:
    # the completed previous execution is retained in history, not current.
    with factory.begin() as session:
        saved = session.get(Run, run["id"])
        saved.current_node_id = "agent"
        saved.current_execution_id = saved.current_attempt_id = None
        assert saved.current_node_id == "agent"
        assert saved.current_attempt_id is None and saved.current_execution_id is None
        reason = json.loads(saved.waiting_reason_json)
        assert reason["details"]["message"] == "Git HEAD изменился после Start"
        assert reason["details"]["details"]["check"] == "head"
    return run, factory, agent


def decision(comparison):
    return {
        "git_changes": {
            "comparison_id": comparison["comparison_id"],
            "accept_head": True,
            "acknowledge_risk": True,
        }
    }


def test_head_acceptance_preserves_work_and_resumes_new_stage(
    authenticated, repository, settings, boundary
):
    run, factory, agent = boundary
    comparison = review(authenticated, run)
    assert comparison["kind"] == "head" and comparison["can_accept"], comparison
    head = comparison["head"]
    assert head["relation"] == "fast_forward"
    assert len(head["commits"]) == 1
    assert head["changes"][0]["risk"] == "medium"
    assert head["changes"][0]["status"] == "D"
    assert head["worktree_changes"] == [{"path": "src/draft.txt", "code": "??"}]
    original_head, original_index = (
        command(repository, "rev-parse", "HEAD"),
        git.index_hash(repository),
    )
    with factory() as session:
        original_baseline = json.loads(session.get(Run, run["id"]).runtime_json)["git"]["baseline"]
    assert run_command(authenticated, run, "resume").status_code == 409
    response = run_command(authenticated, run, "resolve", decision(comparison), "head-decision")
    assert response.status_code == 200, response.text
    replay = authenticated[0].post(
        f"/api/runs/{run['id']}/commands",
        headers=authenticated[1],
        json={
            "command_id": "head-decision",
            "command_type": "resolve",
            "expected_state_version": comparison["state_version"],
            "payload": decision(comparison),
        },
    )
    assert replay.json() == response.json()
    assert command(repository, "rev-parse", "HEAD") == original_head
    assert git.index_hash(repository) == original_index
    assert (repository / "src/draft.txt").read_text() == "unfinished work\n"
    with factory() as session:
        saved = session.get(Run, run["id"])
        assert saved.state == "waiting_input"
        state = json.loads(saved.runtime_json)["git"]
        assert state["baseline"] == original_baseline
        assert state["head"] == original_head
        (audit,) = list(
            session.scalars(
                select(ArtifactManifest).where(
                    ArtifactManifest.run_id == saved.id,
                    ArtifactManifest.schema_type == "git_head_accepted",
                )
            )
        )
        body = json.loads(audit.body_json)
        assert body["comparison"]["expected"] == head["expected"]
        assert body["comparison"]["current"] == original_head
        assert "diff" not in body["comparison"]["changes"][0]
    current = authenticated[0].get(f"/api/runs/{run['id']}").json()
    assert current["waiting_reason"]["resolution_schema"]["git_changes"]["accepted"]
    assert run_command(authenticated, run, "resume").status_code == 200
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert len(agent.paths) == 1
    assert (repository / "src/draft.txt").read_text() == "unfinished work\n"


def test_head_review_explains_available_recovery_without_old_error(
    authenticated, settings, boundary
):
    run, factory, _ = boundary
    # The actual old run lost its AppError message before this fix.
    with factory.begin() as session:
        saved = session.get(Run, run["id"])
        reason = json.loads(saved.waiting_reason_json)
        reason["details"] = {"reason": "external_change_detected", "details": {}}
        saved.waiting_reason_json = to_json(reason)
    with factory() as session:
        context = collect_context(
            session,
            settings,
            AssistanceTarget(zone="project", project_id=run["project_id"]),
            inspect_workspace=True,
        )
        finding = context.findings[0]
        assert context.questions == []
        assert "Git-коммитом" not in finding.question
        assert "Решить через помощника" in finding.next_step
        assert finding.evidence["run_details"]["stage"]["type"] == "AgentTask"
        assert finding.evidence["git_acceptance"]["review"]["can_accept"]
        assert finding.evidence["git_acceptance"]["review"]["head"]["changes"][0]["diff"] is None
        rules = " ".join(finding.evidence["git_policy"]["recovery_rules"])
        assert "runtime.git.head" in rules
        assert "baseline.protected" not in rules
        assert "восстановление файлов из резервной копии не меняет HEAD" in rules
        assert "начинает AgentTask" in rules
        assert "unfinished work" not in context.model_dump_json()


@pytest.mark.parametrize("when", ["before_accept", "before_resume"])
def test_head_rechecks_state_at_accept_and_resume(authenticated, repository, boundary, when):
    run, _, _ = boundary
    comparison = review(authenticated, run)
    if when == "before_resume":
        assert run_command(authenticated, run, "resolve", decision(comparison)).status_code == 200
    command(repository, "commit", "--allow-empty", "-qm", "moved again")
    response = (
        run_command(authenticated, run, "resolve", decision(comparison))
        if when == "before_accept"
        else run_command(authenticated, run, "resume")
    )
    assert response.status_code == 409
    assert response.json()["code"] == (
        "git_comparison_stale" if when == "before_accept" else "git_acceptance_stale"
    )


@pytest.mark.parametrize(
    "blocker",
    [
        "branch",
        "rewound",
        "diverged",
        "index",
        "hooks",
        "process",
        "protected",
        "outside_paths",
        "active_stage",
    ],
)
def test_head_acceptance_does_not_bypass_guards(
    authenticated, repository, boundary, monkeypatch, blocker
):
    run, factory, _ = boundary
    comparison = review(authenticated, run)
    if blocker == "branch":
        command(repository, "checkout", "-b", "other")
    elif blocker in {"rewound", "diverged"}:
        command(repository, "reset", "--mixed", comparison["head"]["expected"] + "~1")
        if blocker == "diverged":
            command(repository, "commit", "--allow-empty", "-qm", "diverged history")
    elif blocker == "index":
        command(repository, "add", "src/draft.txt")
    elif blocker == "hooks":
        command(repository, "config", "commit.gpgsign", "true")
    elif blocker == "process":
        monkeypatch.setattr(git_changes, "stored_processes_stopped", lambda *_: False)
    elif blocker == "protected":
        (repository / "README.md").write_text("changed protected file")
    elif blocker == "outside_paths":
        (repository / "README.md").write_text("changed protected file")
        command(repository, "add", "README.md")
        command(repository, "commit", "-qm", "outside scope")
    else:
        with factory.begin() as session:
            saved = session.get(Run, run["id"])
            target = json.loads(saved.resume_target_json)
            target["action"] = "retry_attempt"
            saved.resume_target_json = to_json(target)
    comparison = review(authenticated, run)
    assert not comparison["can_accept"] and comparison["blockers"]
    response = run_command(authenticated, run, "resolve", decision(comparison))
    assert response.status_code == 409, response.text
