"""Git ignore semantics across manifests, legacy baselines and commit freshness."""

import hashlib

import pytest
from test_stage8_git_plan import baseline, command, intent
from test_stage8_git_plan import repository as repository_fixture

from agents_ide.engine import git_commit as git
from agents_ide.engine.context_sources import workspace_hash

repository = repository_fixture


@pytest.mark.parametrize("pattern", ["*.pyc\n", "__pycache__/\n", "**/__pycache__/**\n"])
@pytest.mark.parametrize("change", ["modified", "deleted"])
def test_cache_rules_exclude_files_and_legacy_fingerprints(repository, pattern, change):
    (repository / ".gitignore").write_text(pattern)
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore cache")
    relative = "wiki-doc/scripts/__pycache__/sql_ast.cpython-312.pyc"
    cache = repository / relative
    cache.parent.mkdir(parents=True)
    cache.write_bytes(b"\0old cache")
    value = baseline(repository, allowlist=("**",))
    assert relative not in value.protected
    initial = git.file_manifest(repository)
    initial_hash = workspace_hash(repository)
    # Emulate a baseline captured by an older app, which protected ignored files.
    value.protected[relative] = {
        "sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "size": cache.stat().st_size,
        "mode": "100644",
        "ignored": True,
    }
    if change == "deleted":
        cache.unlink()
    else:
        cache.write_bytes(b"\0regenerated Python cache")
    actual = git.check_workspace(
        repository, value, ("**",), expected_head=value.head_sha, branch="agents-ide/run/RUN"
    )
    assert actual == initial
    assert workspace_hash(repository) == initial_hash
    assert git.manifest_hash(actual) == git.manifest_hash(initial)
    assert relative in value.protected
    (repository / "src/a.txt").write_text("meaningful change")
    operation = intent(value, allowlist=("**",))
    result = git.execute(repository, value, operation)
    assert result.sha
    assert git.execute(repository, value, operation, recover_only=True).sha == result.sha
    assert command(repository, "ls-files", relative) == ""


def test_nested_rules_negations_and_tracked_files_match_git(repository):
    folder = repository / "local files"
    folder.mkdir()
    (folder / ".gitignore").write_text("*.pyc\n!keep.pyc\n")
    command(repository, "add", "local files/.gitignore")
    command(repository, "commit", "-qm", "nested ignore rules")
    for name in ("cache.pyc", "keep.pyc", "tracked.pyc"):
        (folder / name).write_text("original")
    command(repository, "add", "-f", "local files/tracked.pyc")
    command(repository, "commit", "-qm", "tracked file matching ignore")
    value = baseline(repository)
    assert "local files/cache.pyc" not in git.file_manifest(repository)
    assert "local files/keep.pyc" in value.protected
    assert "local files/tracked.pyc" in value.protected
    (folder / "cache.pyc").write_text("ignored change")
    expected, actual = git.protected_states(
        repository, value.protected, git.file_manifest(repository), value.allowlist
    )
    assert actual == expected
    (folder / "tracked.pyc").write_text("tracked change")
    with pytest.raises(git.GitCommitError, match="outside the allowlist"):
        git.check_workspace(
            repository,
            value,
            value.allowlist,
            expected_head=value.head_sha,
            branch="agents-ide/run/RUN",
        )


def test_cache_change_during_message_generation_does_not_invalidate_commit(repository):
    (repository / ".gitignore").write_text("*.pyc\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore bytecode")
    cache = repository / "cache.pyc"
    cache.write_bytes(b"\0before")
    value = baseline(repository, allowlist=("**",))
    (repository / "src/a.txt").write_text("updated source")

    def generate_message(diff):
        cache.write_bytes(b"\0after generation")
        return "fix: update source"

    result = git.execute(
        repository, value, intent(value, allowlist=("**",)), generate_message=generate_message
    )
    assert result.sha
    assert command(repository, "ls-files", "cache.pyc") == ""


def test_ignore_changes_do_not_silence_nonignored_paths(repository):
    (repository / ".gitignore").write_text("*.pyc\n")
    command(repository, "add", ".gitignore")
    command(repository, "commit", "-qm", "ignore bytecode")
    value = baseline(repository)
    (repository / "not-ignored.pyc.txt").write_text("new source")
    with pytest.raises(git.GitCommitError, match="outside the allowlist"):
        git.check_workspace(
            repository,
            value,
            value.allowlist,
            expected_head=value.head_sha,
            branch="agents-ide/run/RUN",
        )
