"""Stage 8 Git acceptance against real disposable local repositories."""

import json
import os
import subprocess
import threading
import time

import pytest

from agents_ide.engine import git_commit as git
from agents_ide.engine.git_process import GitTransport, using_transport


def command(workspace, *args):
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith("GIT_")}
    return subprocess.check_output(["git", "-C", str(workspace), *args], env=env).decode().strip()


@pytest.fixture
def repository(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    command(workspace, "init", "-q", "-b", "master")
    command(workspace, "config", "user.name", "Test")
    command(workspace, "config", "user.email", "test@example.invalid")
    command(workspace, "config", "core.autocrlf", "false")
    (workspace / "src").mkdir()
    (workspace / "src" / "a.txt").write_text("base\n")
    (workspace / "README.md").write_text("keep\n")
    command(workspace, "add", ".")
    command(workspace, "commit", "-qm", "base")
    return workspace


def baseline(workspace, *, policy="strict", allowlist=("src/**",)):
    value = git.capture_baseline(workspace, "RUN", allowlist, dirty_policy=policy)
    git.update_baseline_ref(workspace, "RUN", value.head_sha)
    git.ensure_run_branch(workspace, "RUN", value.head_sha)
    return value


def intent(
    value, *, operation="op1", parent=None, branch="agents-ide/run/RUN", allowlist=("src/**",)
):
    return git.CommitIntent(
        operation,
        "RUN",
        "attempt",
        parent or value.head_sha,
        branch,
        "",
        allowlist,
        "Apply change",
        "",
        {git.INTENT_TRAILER: operation},
        "strict",
        "allow_pre_configured",
        value.signing_required,
    )


@pytest.mark.parametrize("path", ["../escape", ".git/HEAD", ".env", "C:/x", "-rf", "src\\..\\x"])
def test_allowlist_rejects_unsafe_paths(path):
    with pytest.raises(git.GitCommitError):
        git.normalize_allowlist([path])


def test_no_changes_intent_recovers_without_commit(repository):
    value = baseline(repository)
    saved = intent(value)
    assert git.execute(repository, value, saved).no_changes
    recovered = git.execute(repository, value, saved, recover_only=True)
    assert recovered.no_changes and recovered.recovered
    assert command(repository, "rev-list", "--count", "HEAD") == "1"


def test_untracked_requires_explicit_permission(repository):
    value = baseline(repository)
    (repository / "src" / "new.txt").write_text("new")
    saved = intent(value)
    saved.allow_untracked = False
    assert git.execute(repository, value, saved).no_changes
    assert command(repository, "status", "--porcelain") == "?? src/new.txt"


def test_multiple_git_nodes_use_shared_baseline_with_separate_allowlists(repository):
    value = baseline(repository, allowlist=("src/**", "README.md"))
    (repository / "src" / "a.txt").write_text("first")
    (repository / "README.md").write_text("second")
    first = git.execute(repository, value, intent(value, allowlist=("src/**",)))
    second = git.execute(
        repository,
        value,
        intent(value, operation="op2", parent=first.sha, allowlist=("README.md",)),
    )
    assert second.sha and command(repository, "status", "--porcelain") == ""


def test_current_branch_policy(repository):
    value = git.capture_baseline(repository, "RUN", ("src/**",))
    (repository / "src" / "a.txt").write_text("changed")
    result = git.execute(repository, value, intent(value, branch="master"))
    assert result.sha == command(repository, "rev-parse", "master")
    assert command(repository, "branch", "--show-current") == "master"


def test_group_close_is_safe_during_health_reads():
    from concurrent.futures import ThreadPoolExecutor

    from agents_ide.worker.processes import ProcessGroup

    group = ProcessGroup()
    with ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(group.close if i % 2 else group.members) for i in range(100)]
        for future in futures:
            future.result()


@pytest.mark.parametrize("signing", [False, True])
def test_blocked_hook_or_signer_times_out_without_commit(repository, signing):
    script = repository / ".git" / ("signer" if signing else "hooks/pre-commit")
    script.write_text("#!/bin/sh\nsleep 60\n", encoding="utf-8")
    script.chmod(0o755)
    if signing:
        command(repository, "config", "commit.gpgsign", "true")
        command(repository, "config", "gpg.program", script.as_posix())
    value = baseline(repository)
    assert value.signing_required == signing
    (repository / "src" / "a.txt").write_text("verified change")
    started = time.monotonic()
    with (
        using_transport(GitTransport(deadline=started + 5)),
        pytest.raises(git.GitCommitError, match="timed out"),
    ):
        git.execute(repository, value, intent(value))
    assert time.monotonic() - started < 15
    assert command(repository, "rev-parse", "HEAD") == value.head_sha
    assert command(repository, "diff", "--cached", "--name-only") == ""


def test_globs_do_not_expand_single_star_across_directories():
    assert git.is_path_allowed("src/a.py", ("src/**/*.py",))
    assert git.is_path_allowed("src/deep/a.py", ("src/**/*.py",))
    assert not git.is_path_allowed("src/deep/a.py", ("src/*.py",))
    assert not git.is_path_allowed(".env", ("**",))


@pytest.mark.parametrize(
    "change,stage", [("tracked", False), ("untracked", False), ("tracked", True)]
)
def test_start_rejects_preexisting_dirty_files(repository, change, stage):
    path = repository / "src" / ("a.txt" if change == "tracked" else "new.txt")
    path.write_text("user work")
    if stage:
        command(repository, "add", "src")
    before = (repository / ".git" / "index").read_bytes()
    with pytest.raises(git.GitCommitError):
        git.capture_baseline(repository, "RUN", ("src/**",))
    assert (repository / ".git" / "index").read_bytes() == before
    assert path.read_text() == "user work"
    assert command(repository, "branch", "--show-current") == "master"


def test_nonoverlap_preserves_user_bytes_and_records_protection(repository):
    (repository / "README.md").write_text("user work\n")
    value = baseline(repository, policy="allow_nonoverlap")
    (repository / "src" / "a.txt").write_text("agent work\n")
    result = git.execute(repository, value, intent(value))
    assert result.sha
    assert (repository / "README.md").read_text() == "user work\n"
    assert command(repository, "show", "HEAD:README.md") == "keep"
    assert command(repository, "show", "master:src/a.txt") == "base"
    assert command(repository, "diff", "--cached", "--name-only") == ""


def test_commit_rename_delete_spaces_and_consecutive_history(repository):
    value = baseline(repository)
    (repository / "src" / "a.txt").rename(repository / "src" / "with space.txt")
    first = git.execute(repository, value, intent(value))
    assert command(repository, "ls-tree", "-r", "--name-only", "HEAD").splitlines() == [
        "README.md",
        "src/with space.txt",
    ]
    second_intent = intent(value, operation="op2", parent=first.sha)
    (repository / "src" / "with space.txt").write_text("second\n")
    second = git.execute(repository, value, second_intent)
    assert command(repository, "rev-parse", "HEAD^") == first.sha
    assert (
        command(repository, "rev-parse", git.BASELINE_REF_TEMPLATE.format(run_id="RUN"))
        == value.head_sha
    )
    assert len(git.list_run_commits(repository, "RUN")) == 2
    assert second.sha != first.sha
    assert command(repository, "status", "--porcelain") == ""


def test_no_changes_compares_trees_not_path_presence(repository):
    value = baseline(repository)
    result = git.execute(repository, value, intent(value))
    assert result.no_changes and result.sha is None
    assert command(repository, "rev-parse", "HEAD") == value.head_sha


def test_existing_branch_is_never_reset(repository):
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("first")
    result = git.execute(repository, value, intent(value))
    with pytest.raises(git.GitCommitError, match="moved"):
        git.ensure_run_branch(repository, "RUN", value.head_sha)
    assert command(repository, "rev-parse", "HEAD") == result.sha


@pytest.mark.parametrize("change", ["outside", "staged", "hook", "config", "branch"])
def test_external_changes_block_commit(repository, change):
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("agent\n")
    if change == "outside":
        (repository / "new-user-file").write_text("user\n")
    elif change == "staged":
        command(repository, "add", "src/a.txt")
    elif change == "hook":
        (repository / ".git" / "hooks" / "pre-commit").write_text("#!/bin/sh\nexit 0\n")
    elif change == "config":
        command(repository, "config", "commit.gpgsign", "true")
    elif change == "branch":
        command(repository, "checkout", "-b", "user-branch")
    with pytest.raises(git.GitCommitError):
        git.execute(repository, value, intent(value))
    assert command(repository, "rev-parse", "HEAD") == value.head_sha


def test_commit_intent_is_saved_before_effect_and_recovers_without_duplicate(repository):
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("change\n")
    saved = {}

    def crash(type_, body):
        if type_ == "git.commit_intent_saved":
            assert command(repository, "rev-parse", "HEAD") == value.head_sha
            saved.update(json.loads(json.dumps(body)))
        elif type_ == "git.commit_created":
            raise RuntimeError("simulated crash after Git before database")

    with pytest.raises(RuntimeError):
        git.execute(repository, value, intent(value), on_event=crash)
    assert saved["expected_tree"] and saved["manifest_hash"]
    sha = command(repository, "rev-parse", "HEAD")
    recovered = git.execute(repository, value, git.CommitIntent.from_dict(saved), recover_only=True)
    assert recovered.recovered and recovered.sha == sha
    again = git.execute(repository, value, git.CommitIntent.from_dict(saved), recover_only=True)
    assert again.sha == sha and command(repository, "rev-list", "--count", "master..HEAD") == "1"


@pytest.mark.parametrize("when", ["before_commit", "during_commit", "before_recovery"])
def test_shared_config_changes_do_not_block_commit_or_recovery(repository, when):
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("change\n")
    saved = intent(value)

    def change_config():
        command(repository, "config", "extensions.worktreeConfig", "true")
        assert git.git_fingerprint(repository)["config_hash"] != value.fingerprint["config_hash"]

    def on_event(type_, body):
        if when == "during_commit" and type_ == "git.commit_created":
            change_config()

    if when == "before_commit":
        change_config()
    result = git.execute(repository, value, saved, on_event=on_event)
    if when == "before_recovery":
        change_config()
    recovered = git.execute(repository, value, saved, recover_only=True)
    assert recovered.recovered and recovered.sha == result.sha
    assert command(repository, "rev-list", "--count", "master..HEAD") == "1"
    assert command(repository, "show", "HEAD:src/a.txt") == "change"
    assert command(repository, "status", "--porcelain") == ""


def test_recovery_without_commit_never_executes_git_commit(repository):
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("change\n")
    saved = {}

    def crash(type_, body):
        if type_ == "git.commit_intent_saved":
            saved.update(body)
            raise RuntimeError("crash before Git")

    with pytest.raises(RuntimeError):
        git.execute(repository, value, intent(value), on_event=crash)
    with pytest.raises(git.GitCommitError) as error:
        git.execute(repository, value, git.CommitIntent.from_dict(saved), recover_only=True)
    assert error.value.code == "unknown_external_result"
    assert command(repository, "rev-parse", "HEAD") == value.head_sha


def test_hook_executes_and_changed_tree_is_not_accepted(repository):
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\necho hook >> src/a.txt\ngit add src/a.txt\n")
    hook.chmod(0o755)
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("verified\n")
    observed = []
    with pytest.raises(git.GitCommitError) as error:
        git.execute(repository, value, intent(value), on_event=lambda t, p: observed.append((t, p)))
    assert error.value.code == "external_change_detected"
    assert "hook" in command(repository, "show", "HEAD:src/a.txt")
    created = next(p for t, p in observed if t == "git.commit_created")
    assert not created["verified"] and created["expected_tree"] != created["actual_tree"]


def test_refusing_hook_is_not_bypassed(repository):
    hook = repository / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    value = baseline(repository)
    (repository / "src" / "a.txt").write_text("verified\n")
    with pytest.raises(git.GitCommitError):
        git.execute(repository, value, intent(value))
    assert command(repository, "rev-parse", "HEAD") == value.head_sha


def test_git_transport_honors_pre_dispatch_stop(repository):
    stop = threading.Event()
    stop.set()
    with (
        using_transport(GitTransport(stop=stop, deadline=time.monotonic() + 1)),
        pytest.raises(git.GitCommitError),
    ):
        git.read_head_sha(repository)


def test_ignored_files_are_never_staged_by_broad_allowlist(repository):
    (repository / ".gitignore").write_text("private.txt\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore")
    (repository / "private.txt").write_text("user-private-data")
    value = baseline(repository, allowlist=("**",))
    (repository / "src" / "a.txt").write_text("new")
    result = git.execute(repository, value, intent(value, allowlist=("**",)))
    assert result.sha
    assert (
        "private.txt"
        not in command(repository, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    )
    assert (repository / "private.txt").read_text() == "user-private-data"


def test_deleted_path_recovery_uses_the_same_manifest(repository):
    value = baseline(repository)
    (repository / "src" / "a.txt").unlink()
    operation = intent(value)
    result = git.execute(repository, value, operation)
    recovered = git.execute(repository, value, operation, recover_only=True)
    assert recovered.sha == result.sha


@pytest.mark.parametrize("isolated", [False, True])
def test_new_ignored_setup_files_do_not_block_commit(repository, isolated):
    (repository / ".gitignore").write_text(".env\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore local environment")
    if isolated:
        workspace = repository.parent / "isolated"
        command(repository, "worktree", "add", "-b", "isolated", str(workspace))
    else:
        workspace = repository
    value = baseline(workspace, allowlist=("**",))
    (workspace / ".env").write_text("LOCAL_SETTING=test\n")
    cache = workspace / ".pytest_cache"
    cache.mkdir()
    (cache / ".gitignore").write_text("*\n")
    (cache / "README.md").write_text("test cache\n")
    (workspace / "src/a.txt").write_text("agent change\n")
    # Both single ignored files and self-ignoring caches reproduce the failures.
    manifest = git.file_manifest(workspace)
    assert ".env" not in manifest
    assert ".pytest_cache/README.md" not in manifest
    operation = intent(value, allowlist=("**",))
    result = git.execute(workspace, value, operation)
    assert result.sha
    assert git.execute(workspace, value, operation, recover_only=True).sha == result.sha
    tracked = command(workspace, "ls-tree", "-r", "--name-only", "HEAD").splitlines()
    assert ".env" not in tracked
    assert not any(path.startswith(".pytest_cache/") for path in tracked)
    assert (workspace / ".env").read_text() == "LOCAL_SETTING=test\n"
    assert (cache / "README.md").read_text() == "test cache\n"
    assert command(workspace, "status", "--porcelain") == ""
    if isolated:
        assert command(repository, "rev-parse", "HEAD") == value.head_sha
        assert (repository / "src/a.txt").read_text() == "base\n"


@pytest.mark.parametrize("change", ["modify", "delete"])
def test_preexisting_ignored_files_do_not_affect_commit(repository, change):
    (repository / ".gitignore").write_text(".env\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore local environment")
    private = repository / ".env"
    private.write_text("user setting\n")
    value = baseline(repository, allowlist=("**",))
    (repository / "src/a.txt").write_text("agent change\n")
    if change == "modify":
        private.write_text("changed setting\n")
    else:
        private.unlink()
    assert ".env" not in value.protected
    assert git.execute(repository, value, intent(value, allowlist=("**",))).sha
    assert command(repository, "ls-files", ".env") == ""


def test_ignored_dependency_tree_does_not_exhaust_manifest_or_freshness_budget(
    repository, monkeypatch
):
    from agents_ide.engine.context_sources import workspace_hash

    (repository / ".gitignore").write_text("node_modules/\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore dependencies")
    deps = repository / "node_modules"
    deps.mkdir()
    for i in range(10):
        (deps / f"dependency{i}.js").write_text("dependency")
    monkeypatch.setattr(git, "MAX_FILES", 5)
    manifest = git.file_manifest(repository)
    assert not any(path.startswith("node_modules/") for path in manifest)
    original = workspace_hash(repository)
    assert original
    (deps / "dependency0.js").write_text("updated dependency")
    assert workspace_hash(repository) == original
    (repository / "src" / "a.txt").write_text("changed source")
    assert workspace_hash(repository) != original
    (repository / "new.txt").write_text("untracked source")
    assert "new.txt" in git.file_manifest(repository)
    (repository / "README.md").unlink()
    assert git.file_manifest(repository)["README.md"] == {"missing": True}
