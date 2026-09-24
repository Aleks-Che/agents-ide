"""Real, concurrent worktrees through bindings, queue, runner and process supervision."""

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from test_stage8_git_plan import command
from test_stage8_git_plan import repository as repository_fixture

from agents_ide.adapters.base import AgentAdapter, AgentResult, ExternalOutcome
from agents_ide.domain.workspace import reservations_overlap
from agents_ide.engine import queue
from agents_ide.engine.runner import Runner
from agents_ide.engine.worktrees import prepare_worktree
from agents_ide.errors import AppError
from agents_ide.persistence.models import ProcessSupervision, Run, WorkspaceReservation

repository = repository_fixture


def test_nested_worktree_destinations_still_conflict(tmp_path):
    parent = {"worktree_path": str(tmp_path / "first")}
    nested = {"worktree_path": str(tmp_path / "first" / "nested")}
    other = {"worktree_path": str(tmp_path / "second")}
    assert reservations_overlap(parent, nested)
    assert reservations_overlap(nested, parent)
    assert not reservations_overlap(parent, other)


class WritingAgent(AgentAdapter):
    name = "worktree-test"

    def __init__(self, barrier=None):
        self.barrier = barrier
        self.paths = []

    def run(self, request):
        root = Path(request.workspace_path)
        if request.role == "verifier":
            assert (root / "src/a.txt").read_text() == root.name
            body = {"verdict": "passed"}
            return AgentResult(ExternalOutcome.SUCCEEDED, json.dumps(body), body, "passed")
        self.paths.append(root)
        assert (root / "src/a.txt").read_text() == "base\n"
        (root / "src/a.txt").write_text(root.name)
        if self.barrier:
            self.barrier.wait(timeout=30)
        return AgentResult(ExternalOutcome.SUCCEEDED, "done", {"output": "done"}, None)


def binding_for(authenticated, workspace, *, commit=True, mode="worktree", allowlist=None):
    client, headers = authenticated

    def post(url, body):
        response = client.post(f"/api/{url}", json=body, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    project = post("projects", {"name": "project", "workspace_path": str(workspace)})
    template = post("templates", {"name": "worktree"})
    profile = post(
        "harness_profiles",
        {
            "name": "agent",
            "harness_kind": "codex",
            "settings": {"permission_mode": "read_only"},
        },
    )
    nodes = [
        {"id": "start", "type": "Start"},
        {
            "id": "agent",
            "type": "AgentTask",
            "config": {
                "prompt": "edit",
                "harness_profile_id": profile["id"],
                "model": "test",
            },
        },
        {
            "id": "command",
            "type": "Command",
            "config": {
                "commands": [
                    {
                        "id": "write",
                        "program": sys.executable,
                        "args": [
                            "-c",
                            "from pathlib import Path; "
                            "Path('src/cwd.txt').write_text(str(Path.cwd()))",
                        ],
                        "success_exit_codes": [0],
                        "retry_safety": "safe",
                    }
                ],
            },
        },
    ]
    if commit:
        nodes.append(
            {
                "id": "verify",
                "type": "AgentTask",
                "config": {
                    "role": "verifier",
                    "prompt": "verify",
                    "response_format": "json",
                    "harness_profile_id": profile["id"],
                    "model": "test",
                },
            }
        )
        nodes.append(
            {
                "id": "commit",
                "type": "GitCommit",
                "config": {
                    "allowlist": ["src/**"] if allowlist is None else allowlist,
                    "verification_node_id": "verify",
                },
            }
        )
    nodes.append({"id": "end", "type": "End"})
    graph = {
        "nodes": nodes,
        "edges": [{"from": a["id"], "to": b["id"]} for a, b in zip(nodes, nodes[1:], strict=False)],
    }
    version = post(f"templates/{template['id']}/versions", {"graph": graph})
    binding = post(
        f"versions/{version['id']}/bindings",
        {
            "project_id": project["id"],
            "name": "isolated",
            "workspace_mode": mode,
        },
    )
    return project, binding


def start(authenticated, project, binding, **extra):
    client, headers = authenticated
    chat_id = extra.pop("chat_id", None)
    if chat_id is None:
        chat_id = client.post(
            f"/api/projects/{project['id']}/chats", json={"title": str(uuid4())}, headers=headers
        ).json()["id"]
    response = client.post(
        "/api/runs",
        headers=headers,
        json={
            "project_id": project["id"],
            "binding_id": binding["id"],
            "chat_id": chat_id,
            "message": "go",
            "idempotency_key": str(uuid4()),
            **extra,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def restart(authenticated, run):
    client, headers = authenticated
    current = client.get(f"/api/runs/{run['id']}").json()
    response = client.post(
        f"/api/runs/{run['id']}/restart",
        headers=headers,
        json={"command_id": str(uuid4()), "expected_state_version": current["state_version"]},
    )
    assert response.status_code == 201, response.text
    return response.json()


class ContinuingAgent(AgentAdapter):
    def __init__(self, root, expected):
        self.root, self.expected = root, expected
        self.paths = []

    def run(self, request):
        root = Path(request.workspace_path)
        assert root == self.root
        if request.role == "verifier":
            assert (root / "src/a.txt").read_text() == self.expected + " continued"
            body = {"verdict": "passed"}
            return AgentResult(ExternalOutcome.SUCCEEDED, json.dumps(body), body, "passed")
        self.paths.append(root)
        assert (root / "src/a.txt").read_text() == self.expected
        assert (root / "src/draft.txt").read_text() == "unfinished draft"
        (root / "src/a.txt").write_text(self.expected + " continued")
        return AgentResult(ExternalOutcome.SUCCEEDED, "done", {"output": "done"}, None)


@pytest.mark.parametrize("commit", [False, True])
@pytest.mark.parametrize("paused", [False, True])
def test_restart_and_new_run_in_dialog_keep_worktree_changes(
    authenticated, repository, settings, monkeypatch, commit, paused
):
    client, headers = authenticated
    project, binding = binding_for(authenticated, repository, commit=commit)
    first = start(authenticated, project, binding)
    factory = client.app.state.session_factory
    runner = claim(factory, settings, first)
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    controls = runner._controls

    def pause_after_edit(**kwargs):
        if runner.runtime.get("next_node_id") == "command":
            current = client.get(f"/api/runs/{first['id']}").json()
            response = client.post(
                f"/api/runs/{first['id']}/commands",
                headers=headers,
                json={
                    "command_id": "pause",
                    "command_type": "pause",
                    "expected_state_version": current["state_version"],
                },
            )
            assert response.status_code == 200, response.text
        return controls(**kwargs)

    if paused:
        runner._controls = pause_after_edit
    assert runner.execute(first["id"]).final_state == ("paused" if paused else "completed")
    root = agent.paths[0]
    # Keep both agent output and edits added between runs, regardless of dirty policy.
    (root / "src/a.txt").write_text(first["id"] + " edited")
    (root / "src/draft.txt").write_text("unfinished draft")
    (root / "README.md").write_text("keep outside allowlist")
    command(repository, "checkout", "-b", "source-moved")
    (repository / "README.md").write_text("source only")
    command(repository, "commit", "-am", "source advanced")
    source_head = command(repository, "rev-parse", "HEAD")
    with factory() as session:
        original_snapshot = session.get(Run, first["id"]).snapshot_json

    second = restart(authenticated, first)
    agent = ContinuingAgent(root, first["id"] + " edited")
    result = claim(factory, settings, second).execute(second["id"])
    assert result.final_state == "completed", result
    assert agent.paths == [root]
    # A fresh start (e.g. another message/template) shares the same durable assignment.
    third = start(authenticated, project, binding, chat_id=first["chat_id"])
    agent = ContinuingAgent(root, first["id"] + " edited continued")
    result = claim(factory, settings, third).execute(third["id"])
    assert result.final_state == "completed", result
    assert agent.paths == [root]
    assert command(root, "branch", "--show-current") == f"agents-ide/run/{first['id']}"
    assert command(repository, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert command(repository, "rev-parse", "HEAD") == source_head
    assert (repository / "src/a.txt").read_text() == "base\n"
    assert (root / "README.md").read_text() == "keep outside allowlist"
    if commit:
        assert command(root, "rev-list", "--count", "HEAD") == ("3" if paused else "4")
        assert command(root, "status", "--porcelain") == "M README.md"
    with factory() as session:
        assert session.get(Run, first["id"]).snapshot_json == original_snapshot
        assert {
            json.loads(row.snapshot_json)["workspace"]["worktree_path"]
            for row in session.scalars(select(Run).where(Run.chat_id == first["chat_id"]))
        } == {str(root)}


def test_queued_runs_in_dialog_reserve_one_destination(
    authenticated, repository, settings, monkeypatch
):
    client, headers = authenticated
    project, binding = binding_for(authenticated, repository, commit=False)
    first = start(authenticated, project, binding)
    second = start(authenticated, project, binding, chat_id=first["chat_id"])
    factory = client.app.state.session_factory
    first_runner = claim(factory, settings, first)
    assert queue.claim_next_job(factory, worker_id="second", lease_seconds=300) is None
    current = client.get(f"/api/runs/{first['id']}").json()
    response = client.post(
        f"/api/runs/{first['id']}/commands",
        headers=headers,
        json={
            "command_id": "cancel-before-checkout",
            "command_type": "cancel",
            "expected_state_version": current["state_version"],
        },
    )
    assert response.status_code == 200, response.text
    assert first_runner.execute(first["id"]).final_state == "cancelled"
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    result = claim(factory, settings, second).execute(second["id"])
    assert result.final_state == "completed", result
    assert agent.paths[0].name == first["id"]
    third = restart(authenticated, second)
    with factory() as session:
        assert json.loads(session.get(Run, third["id"]).snapshot_json)["workspace"][
            "worktree_path"
        ] == str(agent.paths[0])
    assert command(repository, "worktree", "list", "--porcelain").count("worktree ") == 2


def test_legacy_dialog_reuses_prepared_worktree_before_queued_restart(
    authenticated, repository, settings, monkeypatch
):
    from agents_ide.engine import worktrees

    client, _ = authenticated
    project, binding = binding_for(authenticated, repository, commit=False)
    with monkeypatch.context() as legacy:
        legacy.setattr(
            worktrees,
            "plan_worktree",
            lambda session, repository, run_id, chat_id: {
                "worktree_path": str(worktrees.worktree_path(repository, run_id))
            },
        )
        first = start(authenticated, project, binding)
        factory = client.app.state.session_factory
        agent = WritingAgent()
        monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
        assert claim(factory, settings, first).execute(first["id"]).final_state == "completed"
        queued = start(authenticated, project, binding, chat_id=first["chat_id"])
    root = agent.paths[0]
    (root / "src/draft.txt").write_text("unfinished draft")
    replacement = restart(authenticated, queued)
    # Restart journals cancellation; the worker acknowledges it before dispatching again.
    assert claim(factory, settings, queued).execute(queued["id"]).final_state == "cancelled"
    agent = ContinuingAgent(root, first["id"])
    result = claim(factory, settings, replacement).execute(replacement["id"])
    assert result.final_state == "completed", result
    assert agent.paths == [root]
    assert command(repository, "worktree", "list", "--porcelain").count("worktree ") == 2


def test_missing_dialog_worktree_is_not_recreated(authenticated, repository, settings, monkeypatch):
    client, _ = authenticated
    project, binding = binding_for(authenticated, repository, commit=False)
    first = start(authenticated, project, binding)
    factory = client.app.state.session_factory
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    assert claim(factory, settings, first).execute(first["id"]).final_state == "completed"
    root = agent.paths[0]
    retained = root.with_name("moved-worktree")
    command(repository, "worktree", "move", str(root), str(retained))
    replacement = restart(authenticated, first)
    result = claim(factory, settings, replacement).execute(replacement["id"])
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.code == "workspace_conflict"
    assert not root.exists()
    assert (retained / "src/a.txt").read_text() == first["id"]
    assert len(agent.paths) == 1
    assert command(repository, "worktree", "list", "--porcelain").count("worktree ") == 2


def claim(factory, settings, run):
    job = queue.claim_next_job(factory, worker_id="worktree", lease_seconds=300)
    assert job and job.run_id == run["id"]
    return Runner(
        session_factory=factory,
        worker_id="worktree",
        generation=job.generation,
        data_dir=settings.data_dir,
        secret_store=None,
    )


@pytest.mark.parametrize("mode", ["project", "worktree"])
def test_shared_config_change_after_start_does_not_block_run(
    authenticated, repository, settings, monkeypatch, mode
):
    project, binding = binding_for(authenticated, repository, mode=mode)
    run = start(authenticated, project, binding)
    command(repository, "config", "extensions.worktreeConfig", "true")
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    factory = authenticated[0].app.state.session_factory
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert command(agent.paths[0], "rev-list", "--count", "HEAD") == "2"
    assert command(agent.paths[0], "status", "--porcelain") == ""


def test_four_dialogs_run_and_commit_concurrently_without_touching_source(
    authenticated,
    repository,
    settings,
    monkeypatch,
):
    client, headers = authenticated
    (repository / "src/a.txt").write_text("user staged change")
    command(repository, "add", "src/a.txt")
    (repository / "private.txt").write_text("user untracked file")
    original_head = command(repository, "rev-parse", "HEAD")
    original_index = (repository / ".git/index").read_bytes()
    original_status = command(repository, "status", "--porcelain")
    project, binding = binding_for(authenticated, repository)
    assert binding["workspace_mode"] == "worktree"
    checked = client.post(f"/api/bindings/{binding['id']}/preflight", json={}, headers=headers)
    assert checked.json()["ok"], checked.text
    assert checked.json()["git_plan"]["will_create_worktree"]
    assert len(command(repository, "worktree", "list", "--porcelain").split("worktree ")) == 2
    runs = [start(authenticated, project, binding) for _ in range(4)]
    factory = client.app.state.session_factory
    runners = [claim(factory, settings, run) for run in runs]
    agent = WritingAgent(threading.Barrier(4))
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    with ThreadPoolExecutor(4) as pool:
        futures = [
            pool.submit(runner.execute, run["id"])
            for runner, run in zip(runners, runs, strict=True)
        ]
        for future in futures:
            result = future.result(timeout=120)
            assert result.final_state == "completed", json.dumps(
                result.waiting_reason.model_dump() if result.waiting_reason else {},
                ensure_ascii=True,
            )
    assert len(set(agent.paths)) == 4
    with factory() as session:
        for run, root in zip(
            runs, [Path(r.runtime["workspace"]["workspace_path"]) for r in runners], strict=True
        ):
            saved = session.get(Run, run["id"])
            assert json.loads(saved.snapshot_json)["workspace"]["workspace_path"] == str(repository)
            assert root != repository and root.is_dir()
            assert (root / "src/a.txt").read_text() == run["id"]
            assert Path((root / "src/cwd.txt").read_text()) == root
            assert not (root / "private.txt").exists()
            assert command(root, "branch", "--show-current") == f"agents-ide/run/{run['id']}"
            assert command(root, "rev-list", "--count", "HEAD") == "2"
            assert command(root, "status", "--porcelain") == ""
            processes = list(
                session.scalars(
                    select(ProcessSupervision).where(
                        ProcessSupervision.run_id == run["id"],
                        ProcessSupervision.kind == "command",
                    )
                )
            )
            assert processes
            assert all(
                json.loads(p.workspace_json)["workspace_path"] == str(root) for p in processes
            )
        assert not list(
            session.scalars(
                select(WorkspaceReservation).where(
                    WorkspaceReservation.released_at.is_(None),
                )
            )
        )
    assert command(repository, "branch", "--show-current") == "master"
    assert command(repository, "rev-parse", "HEAD") == original_head
    assert (repository / ".git/index").read_bytes() == original_index
    assert command(repository, "status", "--porcelain") == original_status


def test_resume_reuses_worktree_even_without_git_commit_node(
    authenticated,
    repository,
    settings,
    monkeypatch,
):
    client, headers = authenticated
    project, binding = binding_for(authenticated, repository, commit=False)
    run = start(authenticated, project, binding)
    factory = client.app.state.session_factory
    runner = claim(factory, settings, run)
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    controls = runner._controls
    paused = False

    def pause_after_agent(**kwargs):
        nonlocal paused
        if runner.runtime.get("next_node_id") == "command" and not paused:
            paused = True
            current = client.get(f"/api/runs/{run['id']}").json()
            response = client.post(
                f"/api/runs/{run['id']}/commands",
                headers=headers,
                json={
                    "command_id": "pause",
                    "command_type": "pause",
                    "expected_state_version": current["state_version"],
                },
            )
            assert response.status_code == 200, response.text
        return controls(**kwargs)

    runner._controls = pause_after_agent
    assert runner.execute(run["id"]).final_state == "paused"
    root = agent.paths[0]
    # Source checkout changes do not redirect a resumed run.
    command(repository, "checkout", "-b", "user-other-branch")
    (repository / "README.md").write_text("user changes")
    current = client.get(f"/api/runs/{run['id']}").json()
    response = client.post(
        f"/api/runs/{run['id']}/commands",
        headers=headers,
        json={
            "command_id": "resume",
            "command_type": "resume",
            "expected_state_version": current["state_version"],
        },
    )
    assert response.status_code == 200, response.text
    resumed = claim(factory, settings, run)
    assert resumed.execute(run["id"]).final_state == "completed"
    assert agent.paths == [root]
    assert Path((root / "src/cwd.txt").read_text()) == root
    assert (repository / "README.md").read_text() == "user changes"


@pytest.mark.parametrize(
    "first_mode,second_mode",
    [("project", "project"), ("project", "worktree"), ("worktree", "project")],
)
def test_only_shared_working_files_block_other_dialogs(
    authenticated, repository, settings, first_mode, second_mode
):
    client, _ = authenticated
    project, binding = binding_for(authenticated, repository, mode=first_mode)
    first = start(authenticated, project, binding)
    second = start(authenticated, project, binding, overrides={"workspace_mode": second_mode})
    factory = client.app.state.session_factory
    claim(factory, settings, first)
    claimed = queue.claim_next_job(factory, worker_id="second", lease_seconds=300)
    if first_mode == second_mode:
        assert claimed is None
    else:
        assert claimed and claimed.run_id == second["id"]


def test_worktree_executes_while_source_checkout_is_reserved(
    authenticated, repository, settings, monkeypatch
):
    client, _ = authenticated
    project, binding = binding_for(authenticated, repository, commit=False, mode="project")
    source = start(authenticated, project, binding)
    isolated = start(authenticated, project, binding, overrides={"workspace_mode": "worktree"})
    factory = client.app.state.session_factory
    claim(factory, settings, source)
    runner = claim(factory, settings, isolated)
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    assert runner.execute(isolated["id"]).final_state == "completed"
    assert (repository / "src/a.txt").read_text() == "base\n"
    assert (agent.paths[0] / "src/a.txt").read_text() == isolated["id"]


def test_binding_update_and_invalid_worktree_configuration(authenticated, tmp_path):
    client, headers = authenticated
    workspace = tmp_path / "plain"
    workspace.mkdir()
    command(workspace, "init")  # No commit; do not inherit the checkout containing test files.
    _, binding = binding_for(authenticated, workspace, commit=False, mode="project")
    url = f"/api/bindings/{binding['id']}"
    updated = client.patch(
        url,
        headers=headers,
        json={
            "expected_version": binding["version"],
            "workspace_mode": "worktree",
        },
    )
    assert updated.status_code == 200, updated.text
    assert client.get(url).json()["workspace_mode"] == "worktree"
    checked = client.post(f"{url}/preflight", headers=headers, json={}).json()
    assert not checked["ok"]
    assert "git_repository_required" in [e["code"] for e in checked["errors"]]
    checked = client.post(
        f"{url}/preflight",
        headers=headers,
        json={
            "overrides": {"branch_policy": "current"},
        },
    ).json()
    assert "configuration_invalid" in [e["code"] for e in checked["errors"]]


@pytest.mark.parametrize("dirty_after_crash", [False, True])
def test_created_worktree_is_recovered_after_crash_before_metadata_save(
    authenticated,
    repository,
    settings,
    monkeypatch,
    dirty_after_crash,
):
    client, _ = authenticated
    project, binding = binding_for(authenticated, repository, commit=False)
    run = start(authenticated, project, binding)
    factory = client.app.state.session_factory
    runner = claim(factory, settings, run)
    persist = runner._persist

    def crash(row):
        if runner.runtime.get("workspace"):
            raise AppError("queue_job_lost", "simulated crash", 409)
        persist(row)

    monkeypatch.setattr(runner, "_persist", crash)
    with pytest.raises(AppError, match="simulated crash"):
        runner.execute(run["id"])
    with factory() as session:
        row = session.get(Run, run["id"])
        runner.snapshot = json.loads(row.snapshot_json)
        runner.runtime = json.loads(row.runtime_json)
    assert "workspace" not in runner.runtime
    monkeypatch.setattr(runner, "_persist", persist)
    if dirty_after_crash:
        root = Path(runner.snapshot["workspace"]["worktree_path"])
        changed = root / "src/a.txt"
        changed.write_text("unfinished checkout or external edit")
        with pytest.raises(AppError, match="worktree"):
            prepare_worktree(runner)
        assert changed.read_text() == "unfinished checkout or external edit"
        assert "workspace" not in runner.runtime
        return
    prepare_worktree(runner)
    root = Path(runner.runtime["workspace"]["workspace_path"])
    assert root.is_dir()
    prepare_worktree(runner)
    assert command(repository, "worktree", "list", "--porcelain").count("worktree ") == 2
