"""Durable Git worktrees shared by successive runs in the same dialog."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.workspace import collect_workspace, workspace_scope
from agents_ide.engine.git_commit import RUN_BRANCH_TEMPLATE, git_fingerprint, git_policy_matches
from agents_ide.engine.git_process import optional_git, run_git, using_transport
from agents_ide.errors import AppError
from agents_ide.persistence.models import Run

if TYPE_CHECKING:
    from agents_ide.engine.runner import Runner


def worktree_path(repository: Path, run_id: str) -> Path:
    return repository.parent / f"{repository.name}.agents-ide-worktrees" / run_id


def plan_worktree(
    session: Session, repository: Path, run_id: str, chat_id: str | None
) -> dict[str, str]:
    """Pin the dialog to its first worktree; adopt existing dialogs lazily.

    Run snapshots are immutable and retained, including after history compaction.
    A planned destination is reused even if its first run never reached dispatch.
    For legacy dialogs with several worktrees, prefer the latest prepared one.
    The caller holds the start transaction, so simultaneous starts share this plan.
    """
    fallback = None
    if chat_id:
        for row in session.scalars(
            select(Run).where(Run.chat_id == chat_id).order_by(Run.created_at.desc(), Run.id.desc())
        ):
            workspace = json.loads(row.snapshot_json)["workspace"]
            if not workspace.get("worktree_path"):
                continue
            plan = {
                "worktree_path": workspace["worktree_path"],
                "worktree_run_id": workspace.get("worktree_run_id", row.id),
            }
            if workspace.get("worktree_run_id") or json.loads(row.runtime_json).get("workspace"):
                return plan
            if fallback is None:
                fallback = plan
    return fallback or {
        "worktree_path": str(worktree_path(repository, run_id)),
        "worktree_run_id": run_id,
    }


def _previous_worktree(
    runner: Runner,
) -> tuple[dict[str, Any], dict[str, Any] | None, bool]:
    """Read creation provenance and the latest saved identity after reserving the path."""
    owner_id = runner.snapshot["workspace"].get("worktree_run_id", runner.run_id)
    with runner.session_factory() as session:
        owner = session.get(Run, owner_id)
        current = session.get(Run, runner.run_id)
        if (
            owner is None
            or current is None
            or (
                owner_id != runner.run_id
                and (not current.chat_id or owner.chat_id != current.chat_id)
            )
        ):
            raise AppError("workspace_conflict", "Владелец worktree недоступен", 409)
        source = json.loads(owner.snapshot_json)["workspace"]
        if source["worktree_path"] != runner.snapshot["workspace"]["worktree_path"]:
            raise AppError("workspace_conflict", "Worktree диалога изменился", 409)
        planned = bool(runner.runtime.get("worktree_intent"))
        if current.chat_id:
            for row in session.scalars(
                select(Run)
                .where(Run.chat_id == current.chat_id, Run.id != runner.run_id)
                .order_by(Run.created_at.desc(), Run.id.desc())
            ):
                prior = json.loads(row.snapshot_json)["workspace"]
                if prior.get("worktree_path") != source["worktree_path"]:
                    continue
                runtime = json.loads(row.runtime_json)
                if workspace := runtime.get("workspace"):
                    return source, workspace, True
                planned = planned or bool(runtime.get("worktree_intent"))
        return source, None, planned


def effective_workspace(snapshot: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    return runtime.get("workspace", snapshot["workspace"])  # type: ignore[no-any-return]


def reservation_scope(snapshot: dict[str, Any], runtime: dict[str, Any]) -> dict[str, Any]:
    workspace = effective_workspace(snapshot, runtime)
    scope = dict(workspace["scope"])
    if (
        snapshot.get("resolved_settings", {}).get("workspace_mode") == "worktree"
        and snapshot.get("execution_mode") != "simulated"
    ):
        scope["worktree_path"] = snapshot["workspace"]["worktree_path"]
    return scope


def prepare_worktree(runner: Runner) -> None:
    if runner.simulated or runner.snapshot["resolved_settings"].get("workspace_mode") != "worktree":
        return
    if runner.runtime.get("workspace"):
        runner.snapshot["workspace"] = runner.runtime["workspace"]
        return
    from agents_ide.engine.stage8 import transport

    source, previous, planned = _previous_worktree(runner)
    root = Path(source["workspace_path"])
    target = Path(source["worktree_path"])
    branch = RUN_BRANCH_TEMPLATE.format(
        run_id=runner.snapshot["workspace"].get("worktree_run_id", runner.run_id)
    )
    head = source["git_head_sha"]
    _, normalized, dev, ino, git = collect_workspace(str(root))
    scope = workspace_scope(Path(normalized), git)
    if [dev, ino] != [source["identity_dev"], source["identity_ino"]] or scope[
        "git_common_identity"
    ] != source["scope"]["git_common_identity"]:
        raise AppError("workspace_conflict", "Исходный Git-каталог изменился после Start", 409)
    if target.resolve() != target or target.parent.resolve() != target.parent:
        raise AppError("workspace_conflict", "Каталог worktree перенаправлен", 409)
    intent = {"path": str(target), "branch": branch, "head": head}
    if not runner.runtime.get("worktree_intent"):
        if target.exists() and not planned:
            raise AppError("workspace_conflict", "Каталог worktree уже занят", 409)
        with runner._write() as (session, row):
            runner.runtime["worktree_intent"] = intent
            runner._event(session, "workspace.worktree_planned", intent)
            runner._persist(row)
    elif runner.runtime["worktree_intent"] != intent:
        raise AppError("workspace_conflict", "Параметры worktree изменились", 409)
    with using_transport(transport(runner)):
        expected = runner.snapshot.get("dependencies", {}).get("git", {}).get("fingerprint")
        if expected is not None and not git_policy_matches(git_fingerprint(root), expected):
            raise AppError("external_change_detected", "Git policy changed after preflight", 409)
        if not target.exists():
            if previous:
                raise AppError("workspace_conflict", "Worktree диалога больше не существует", 409)
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
        or (previous is None and (git.head_sha != head or git.dirty))
        or (
            previous is not None
            and [dev, ino] != [previous["identity_dev"], previous["identity_ino"]]
        )
    ):
        raise AppError("workspace_conflict", "Не удалось подтвердить созданный worktree", 409)
    workspace = {
        **source,
        "workspace_path": normalized,
        "identity_dev": dev,
        "identity_ino": ino,
        "scope": scope,
        "git_root_path": git.root_path,
        "git_head_sha": git.head_sha,
        "source_workspace_path": str(root),
        "branch": branch,
    }
    with runner._write() as (session, row):
        runner.runtime["workspace"] = workspace
        runner._event(
            session,
            "workspace.worktree_reused" if previous else "workspace.worktree_created",
            workspace,
        )
        runner._persist(row)
    runner.snapshot["workspace"] = workspace
