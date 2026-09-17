"""Durable per-run Git worktrees; completed runs retain their working files."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from agents_ide.domain.workspace import collect_workspace, workspace_scope
from agents_ide.engine.git_commit import RUN_BRANCH_TEMPLATE, git_fingerprint
from agents_ide.engine.git_process import optional_git, run_git, using_transport
from agents_ide.errors import AppError

if TYPE_CHECKING:
    from agents_ide.engine.runner import Runner


def worktree_path(repository: Path, run_id: str) -> Path:
    return repository.parent / f"{repository.name}.agents-ide-worktrees" / run_id


def effective_workspace(snapshot: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    return runtime.get("workspace", snapshot["workspace"])  # type: ignore[no-any-return]


def prepare_worktree(runner: Runner) -> None:
    if runner.simulated or runner.snapshot["resolved_settings"].get("workspace_mode") != "worktree":
        return
    if runner.runtime.get("workspace"):
        runner.snapshot["workspace"] = runner.runtime["workspace"]
        return
    from agents_ide.engine.stage8 import transport

    source = runner.snapshot["workspace"]
    root = Path(source["workspace_path"])
    target = Path(source["worktree_path"])
    branch = RUN_BRANCH_TEMPLATE.format(run_id=runner.run_id)
    head = source["git_head_sha"]
    _, normalized, dev, ino, git = collect_workspace(str(root))
    scope = workspace_scope(Path(normalized), git)
    if (
        [dev, ino] != [source["identity_dev"], source["identity_ino"]]
        or scope["git_common_identity"] != source["scope"]["git_common_identity"]
    ):
        raise AppError("workspace_conflict", "Исходный Git-каталог изменился после Start", 409)
    if target.resolve() != target or target.parent.resolve() != target.parent:
        raise AppError("workspace_conflict", "Каталог worktree перенаправлен", 409)
    intent = {"path": str(target), "branch": branch, "head": head}
    if not runner.runtime.get("worktree_intent"):
        if target.exists():
            raise AppError("workspace_conflict", "Каталог worktree уже занят", 409)
        with runner._write() as (session, row):
            runner.runtime["worktree_intent"] = intent
            runner._event(session, "workspace.worktree_planned", intent)
            runner._persist(row)
    elif runner.runtime["worktree_intent"] != intent:
        raise AppError("workspace_conflict", "Параметры worktree изменились", 409)
    with using_transport(transport(runner)):
        expected = runner.snapshot.get("dependencies", {}).get("git", {}).get("fingerprint")
        if expected is not None and git_fingerprint(root) != expected:
            raise AppError("external_change_detected", "Git policy changed after preflight", 409)
        if not target.exists():
            if optional_git(root, ["rev-parse", "--verify", f"refs/heads/{branch}"]):
                raise AppError("workspace_conflict", "Ветка worktree уже существует", 409)
            target.parent.mkdir(parents=True, exist_ok=True)
            run_git(root, ["worktree", "add", "-b", branch, "--", str(target), head])
    _, normalized, dev, ino, git = collect_workspace(str(target))
    scope = workspace_scope(Path(normalized), git)
    if (
        git is None
        or Path(git.root_path) != target
        or scope["git_common_identity"] != source["scope"]["git_common_identity"]
        or git.default_branch != branch
        or git.head_sha != head
    ):
        raise AppError("workspace_conflict", "Не удалось подтвердить созданный worktree", 409)
    workspace = {
        **source,
        "workspace_path": normalized,
        "identity_dev": dev,
        "identity_ino": ino,
        "scope": scope,
        "git_root_path": git.root_path,
        "source_workspace_path": str(root),
        "branch": branch,
    }
    with runner._write() as (session, row):
        runner.runtime["workspace"] = workspace
        runner._event(session, "workspace.worktree_created", workspace)
        runner._persist(row)
    runner.snapshot["workspace"] = workspace
