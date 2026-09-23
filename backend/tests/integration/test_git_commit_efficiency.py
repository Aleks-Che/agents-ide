"""Commit preparation scales with changed files, retaining verified content and Git rules."""

import pytest
from test_stage8_git_plan import baseline, command, intent
from test_stage8_git_plan import repository as repository  # pytest fixture

from agents_ide.engine import git_commit as git


@pytest.mark.parametrize("flag", [None, "--assume-unchanged", "--skip-worktree"])
def test_only_changed_files_spawn_hash_object_even_with_user_index_flags(
    repository, monkeypatch, flag
):
    for index in range(100):
        (repository / "src" / f"clean-{index}.txt").write_bytes(b"unchanged\n")
    command(repository, "add", "src")
    command(repository, "commit", "-qm", "more files")
    value = baseline(repository)
    if flag:
        command(repository, "update-index", flag, "src/a.txt")
    (repository / "src/a.txt").write_bytes(b"new contents\n")
    hashed = []
    original = git.run_git

    def observe(workspace, args, **kwargs):
        if args[0] == "hash-object":
            hashed.append(args)
        return original(workspace, args, **kwargs)

    monkeypatch.setattr(git, "run_git", observe)
    result = git.execute(repository, value, intent(value))
    assert result.sha
    assert len(hashed) == 1 and "--path=src/a.txt" in hashed[0]
    assert command(repository, "show", "HEAD:src/a.txt") == "new contents"
    assert command(repository, "show", "HEAD:src/clean-0.txt") == "unchanged"


def test_clean_crlf_files_are_not_rehashed_but_changed_content_is_normalized(
    repository, monkeypatch
):
    (repository / ".gitattributes").write_bytes(b"src/*.txt text eol=crlf\n")
    (repository / "src/a.txt").write_bytes(b"base\n")
    (repository / "src/clean.txt").write_bytes(b"unchanged\n")
    command(repository, "add", "--renormalize", ".")
    command(repository, "add", ".gitattributes", "src/clean.txt")
    command(repository, "commit", "-qm", "line endings")
    value = baseline(repository)
    (repository / "src/clean.txt").write_bytes(b"unchanged\r\n")
    (repository / "src/a.txt").write_bytes(b"changed\r\n")
    hashed = []
    original = git.run_git

    def observe(workspace, args, **kwargs):
        if args[0] == "hash-object":
            hashed.append(args)
        return original(workspace, args, **kwargs)

    monkeypatch.setattr(git, "run_git", observe)
    result = git.execute(repository, value, intent(value))
    assert result.sha
    assert len(hashed) == 1 and "--path=src/a.txt" in hashed[0]
    assert original(repository, ["show", "HEAD:src/a.txt"]) == b"changed\n"
    assert original(repository, ["show", "HEAD:src/clean.txt"]) == b"unchanged\n"


def test_clean_filter_changes_are_applied_to_unchanged_working_bytes(repository):
    (repository / ".gitattributes").write_bytes(b"src/a.txt filter=example\n")
    command(repository, "config", "filter.example.clean", "cat")
    command(repository, "config", "filter.example.required", "true")
    command(repository, "add", ".gitattributes")
    command(repository, "commit", "-qm", "filter")
    value = baseline(repository)
    # A config change can change the Git blob even if raw file bytes did not change.
    command(repository, "config", "filter.example.clean", "tr a-z A-Z")
    result = git.execute(repository, value, intent(value))
    assert result.sha
    assert command(repository, "show", "HEAD:src/a.txt") == "BASE"


@pytest.mark.parametrize(
    "path,error",
    [
        ("src/a.txt", "File changed while building tree"),
        ("README.md", "Files changed during Git filtering"),
    ],
)
def test_file_changed_after_bulk_comparison_still_blocks_commit(
    repository, monkeypatch, path, error
):
    value = baseline(repository)
    (repository / "src/a.txt").write_text("new content")
    original = git.run_git

    def mutate(workspace, args, **kwargs):
        result = original(workspace, args, **kwargs)
        if args[:2] == ["diff", "--name-only"]:
            (repository / path).write_text("concurrent edit")
        return result

    monkeypatch.setattr(git, "run_git", mutate)
    with pytest.raises(git.GitCommitError, match=error):
        git.execute(repository, value, intent(value))
    assert command(repository, "rev-parse", "HEAD") == value.head_sha
