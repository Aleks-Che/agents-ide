"""User-configured workspace volume limits, including recovery of existing runs."""

import hashlib
import json
import os
import tracemalloc

import pytest
from test_stage5_review import command as run_command
from test_stage8_git_plan import command, intent
from test_stage8_git_plan import repository as repository_fixture
from test_worktree_runs import WritingAgent, binding_for, claim, start

from agents_ide.engine import git_commit as git
from agents_ide.engine.context_sources import workspace_hash
from agents_ide.engine.runner import Runner
from agents_ide.persistence.models import Run
from agents_ide.security.workspace_read import hash_workspace_file, read_workspace_file
from agents_ide.services.general_settings import workspace_fingerprint_limit_bytes

repository = repository_fixture
MIB = 1024**2


def save_limit(authenticated, limit):
    client, headers = authenticated
    value = client.get("/api/settings/general").json()
    value["workspace_fingerprint_limit_mib"] = limit
    response = client.put("/api/settings/general", headers=headers, json=value)
    assert response.status_code == 200, response.text
    return response.json()


def test_limit_defaults_validation_and_persistence(authenticated):
    client, headers = authenticated
    original = client.get("/api/settings/general").json()
    assert original["workspace_fingerprint_limit_mib"] == 64
    for invalid in (0, -1, 1.5, "64", True):
        response = client.put(
            "/api/settings/general",
            headers=headers,
            json={**original, "workspace_fingerprint_limit_mib": invalid},
        )
        assert response.status_code == 422
    for value in (128, None, 1):
        saved = save_limit(authenticated, value)
        assert client.get("/api/settings/general").json() == saved
        assert saved["commit_message"] == original["commit_message"]
        assert saved["harness_watchdog"] == original["harness_watchdog"]
        with client.app.state.session_factory() as session:
            assert workspace_fingerprint_limit_bytes(session) == (
                None if value is None else value * MIB
            )


def test_manifest_boundary_error_and_consistent_hash(repository):
    (repository / "z.txt").write_bytes(b"data")
    total = sum(
        p.stat().st_size
        for p in [repository / "README.md", repository / "src/a.txt", repository / "z.txt"]
    )
    exact = git.file_manifest(repository, max_bytes=total)
    assert git.file_manifest(repository, max_bytes=None) == exact
    assert workspace_hash(repository, max_bytes=total) == workspace_hash(repository, max_bytes=None)
    assert workspace_hash(repository, max_bytes=total - 1) is None
    with pytest.raises(git.GitCommitError) as error:
        git.file_manifest(repository, max_bytes=total - 1)
    assert error.value.code == "git_manifest_size_limit"
    assert error.value.details == {
        "path": "z.txt",
        "limit_bytes": total - 1,
        "minimum_bytes": total,
        "bytes_read": total - 4,
        "file_bytes": 4,
    }
    assert "Без лимита" in error.value.message
    assert "z.txt" in error.value.message
    assert f"({total} байт)" in error.value.message


@pytest.mark.parametrize("limit", [None, 128 * MIB])
def test_single_file_above_old_limit_can_be_hashed_committed_and_recovered(repository, limit):
    baseline = git.capture_baseline(repository, "RUN", ("src/**",), max_bytes=limit)
    git.update_baseline_ref(repository, "RUN", baseline.head_sha)
    git.ensure_run_branch(repository, "RUN", baseline.head_sha)
    # Exercise the old per-file staging cap as well as the total workspace cap.
    large = repository / "src/large.bin"
    with large.open("wb") as stream:
        stream.truncate(64 * MIB + 1)
    operation = intent(baseline)
    result = git.execute(repository, baseline, operation, max_bytes=limit)
    assert result.sha
    recovered = git.execute(repository, baseline, operation, recover_only=True, max_bytes=limit)
    assert recovered.sha == result.sha
    assert command(repository, "status", "--porcelain") == ""


def test_unlimited_hash_streams_and_keeps_link_checks(tmp_path):
    file = tmp_path / "file.bin"
    block = b"x" * MIB
    with file.open("wb") as stream:
        for _ in range(10):
            stream.write(block)
    hash_workspace_file(tmp_path, "file.bin", None)  # warm platform imports
    tracemalloc.start()
    try:
        digest, size = hash_workspace_file(tmp_path, "file.bin", None)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert size == 10 * MIB
    assert digest == hashlib.sha256(block * 10).hexdigest()
    assert peak < 4 * MIB
    os.link(file, tmp_path / "alias.bin")
    with pytest.raises(ValueError, match="linked_file"):
        hash_workspace_file(tmp_path, "alias.bin", None)
    with pytest.raises(ValueError, match="linked_file"):
        read_workspace_file(tmp_path, "alias.bin", None)


def test_file_growth_after_manifest_is_reported_as_external_change(repository, monkeypatch):
    baseline = git.capture_baseline(repository, "RUN", ("src/**",), max_bytes=None)
    git.update_baseline_ref(repository, "RUN", baseline.head_sha)
    git.ensure_run_branch(repository, "RUN", baseline.head_sha)
    (repository / "src/a.txt").write_text("edit")
    read = git.read_workspace_file

    def grow(workspace, path, cap):
        (workspace / path).write_text("changed after fingerprint")
        return read(workspace, path, cap)

    monkeypatch.setattr(git, "read_workspace_file", grow)
    with pytest.raises(git.GitCommitError) as error:
        git.execute(repository, baseline, intent(baseline), max_bytes=None)
    assert error.value.code == "external_change_detected"
    assert command(repository, "rev-parse", "HEAD") == baseline.head_sha


@pytest.mark.parametrize("limit", [2, None])
@pytest.mark.parametrize("failure", ["baseline", "legacy", "commit"])
def test_saved_limit_allows_waiting_run_to_resume(
    authenticated, repository, settings, monkeypatch, limit, failure
):
    (repository / "bulk.bin").write_bytes(b"x" * MIB)
    command(repository, "add", "bulk.bin")
    command(repository, "commit", "-qm", "large baseline")
    save_limit(authenticated, 2 if failure == "commit" else 1)
    project, binding = binding_for(authenticated, repository)
    run = start(authenticated, project, binding)
    factory = authenticated[0].app.state.session_factory
    agent = WritingAgent()
    monkeypatch.setattr(Runner, "_build_adapters", lambda *_: (agent, None))
    execute = git.execute
    if failure == "commit":

        def exceed(workspace, baseline, intent, **kwargs):
            # Another run or the user can lower the global limit between checks.
            save_limit(authenticated, 1)
            return execute(workspace, baseline, intent, **{**kwargs, "max_bytes": MIB})

        monkeypatch.setattr(git, "execute", exceed)
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "waiting_input", result
    assert result.waiting_reason.details["reason"] == "git_manifest_size_limit"
    assert "Без лимита" in result.waiting_reason.details["message"]
    if failure == "legacy":
        with factory() as session:
            row = session.get(Run, run["id"])
            waiting = json.loads(row.waiting_reason_json)
            waiting["details"]["reason"] = "path_violation"
            waiting["details"]["message"] = "Cannot safely fingerprint workspace"
            row.waiting_reason_json = json.dumps(waiting)
            runtime = json.loads(row.runtime_json)
            runtime["waiting_reason"] = waiting
            row.runtime_json = json.dumps(runtime)
            session.commit()
    monkeypatch.setattr(git, "execute", execute)
    save_limit(authenticated, limit)
    resumed = run_command(authenticated, run, "resume")
    assert resumed.status_code == 200, resumed.text
    result = claim(factory, settings, run).execute(run["id"])
    assert result.final_state == "completed", result
    assert len(agent.paths) == 1  # Completed agent work must not be replayed.


def test_preflight_uses_current_saved_limit(authenticated, repository):
    (repository / "bulk.bin").write_bytes(b"x" * MIB)
    command(repository, "add", "bulk.bin")
    command(repository, "commit", "-qm", "large baseline")
    _, binding = binding_for(authenticated, repository, mode="project")
    client, headers = authenticated
    for limit in (1, 2, None):
        save_limit(authenticated, limit)
        response = client.post(f"/api/bindings/{binding['id']}/preflight", headers=headers, json={})
        body = response.json()
        assert body["ok"] == (limit != 1), body
        if limit == 1:
            assert any(e["code"] == "git_manifest_size_limit" for e in body["errors"])
