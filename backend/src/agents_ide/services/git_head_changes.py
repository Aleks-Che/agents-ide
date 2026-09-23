"""Review committed HEAD movement at a boundary where no stage has started."""

from __future__ import annotations

import json
import re
from pathlib import Path, PurePosixPath
from typing import Literal

from agents_ide.domain.schemas import ApiOutput
from agents_ide.engine import git_commit as git
from agents_ide.engine.artifacts import sanitize
from agents_ide.engine.git_process import optional_git, run_git
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.persistence.models import Run


class HeadCommit(ApiOutput):
    sha: str
    subject: str


class HeadFileChange(ApiOutput):
    path: str
    status: str
    risk: Literal["medium", "high", "unknown"]
    assessment: str
    diff: str | None = None
    diff_truncated: bool = False


class GitHeadReview(ApiOutput):
    expected: str
    current: str
    relation: Literal["same", "fast_forward", "rewound", "diverged", "unavailable"]
    commits: list[HeadCommit]
    omitted_commits: int
    changes: list[HeadFileChange]
    omitted_changes: int
    worktree_changes: list[dict[str, str]]
    omitted_worktree_changes: int
    assessment: str


def head_boundary(run: Run) -> bool:
    runtime = json.loads(run.runtime_json or "{}")
    waiting = json.loads(run.waiting_reason_json or "null") or runtime.get("waiting_reason") or {}
    target = json.loads(run.resume_target_json or "{}")
    return bool(
        run.state in {"waiting_input", "paused", "stopped"}
        and waiting.get("code") == "external_change_detected"
        and waiting.get("details", {}).get("reason") in {"external_change_detected", "head_changed"}
        and run.current_attempt_id is None
        and run.current_execution_id is None
        and target.get("execution_id") is None
        and target.get("action") == "dispatch_next"
        and target.get("node_id") == run.current_node_id
        and set(target.get("blockers", [])) <= {"external_change_detected"}
        and runtime.get("git", {}).get("phase") == "ready"
        and any(
            node["id"] == run.current_node_id and node.get("type") == "AgentTask"
            for node in effective_snapshot(run).get("graph", {}).get("nodes", [])
        )
    )


def compare_head(
    workspace: Path, expected: str, current: str, allowed: list[str], *, include_diff: bool
) -> tuple[GitHeadReview, list[str]]:
    from agents_ide.services.git_changes import _sensitive

    blockers: list[str] = []
    commits: list[HeadCommit] = []
    changes: list[HeadFileChange] = []
    omitted_commits = omitted_changes = 0
    relation: Literal["same", "fast_forward", "rewound", "diverged", "unavailable"] = "unavailable"
    if not all(re.fullmatch(r"[a-f0-9]{40,64}", sha) for sha in (expected, current)):
        blockers.append("Не удалось определить оба коммита для сравнения.")
    elif optional_git(workspace, ["cat-file", "-t", expected]) != "commit":
        blockers.append("Ожидаемый коммит недоступен в истории Git.")
    else:
        if expected == current:
            relation = "same"
        elif (
            optional_git(workspace, ["merge-base", "--is-ancestor", expected, current]) is not None
        ):
            relation = "fast_forward"
        elif (
            optional_git(workspace, ["merge-base", "--is-ancestor", current, expected]) is not None
        ):
            relation = "rewound"
        else:
            relation = "diverged"
        if relation not in {"same", "fast_forward"}:
            blockers.append(
                "История откатилась или разошлась. "
                "Принять можно только новые коммиты поверх ожидаемого HEAD."
            )
        raw_commits = (
            run_git(workspace, ["rev-list", "--max-count=51", f"{expected}..{current}"])
            .decode()
            .splitlines()
        )
        total = int(run_git(workspace, ["rev-list", "--count", f"{expected}..{current}"]))
        omitted_commits = max(0, total - 50)
        for sha in raw_commits[:50]:
            subject = (
                run_git(workspace, ["show", "-s", "--format=%s", sha])
                .decode("utf-8", "replace")
                .strip()
            )
            commits.append(HeadCommit(sha=sha, subject=str(sanitize(subject[:500]))))
        raw = run_git(
            workspace,
            [
                "diff",
                "--no-ext-diff",
                "--no-textconv",
                "--no-renames",
                "--name-status",
                "-z",
                expected,
                current,
                "--",
            ],
        )
        parts = raw.decode("utf-8", "strict").rstrip("\0").split("\0") if raw else []
        pairs = list(zip(parts[::2], parts[1::2], strict=True))
        omitted_changes = max(0, len(pairs) - 100)
        # Every path is checked, including paths outside the bounded UI preview.
        if any(not git.is_path_allowed(path, allowed) for _, path in pairs):
            blockers.append(
                "Коммиты затрагивают пути вне разрешённого списка; "
                "принятие HEAD не расширяет этот список."
            )
        if omitted_commits or omitted_changes:
            blockers.append(
                "Сравнение слишком большое для полного просмотра. Принятие HEAD недоступно."
            )
        for status, path in pairs[:100]:
            generated = (
                status == "D"
                and "__pycache__" in PurePosixPath(path).parts
                and path.endswith(".pyc")
            )
            item = HeadFileChange(
                path=path,
                status=status,
                risk="medium" if generated else "high",
                assessment=(
                    "Удалён файл Python-кэша. Обычно он пересоздаётся; "
                    "это признак меньшего риска, но корректность проекта не проверена."
                    if generated
                    else "Изменение уже включено в коммит и влияет на основу следующего этапа. "
                    "Проверьте содержимое и назначение."
                ),
            )
            if _sensitive(path):
                item.assessment = (
                    "Чувствительный путь: содержимое скрыто. Нужна отдельная проверка."
                )
            elif include_diff:
                diff = run_git(
                    workspace,
                    [
                        "diff",
                        "--no-ext-diff",
                        "--no-textconv",
                        "--no-renames",
                        "--no-color",
                        "--unified=3",
                        expected,
                        current,
                        "--",
                        ":(literal)" + path,
                    ],
                ).decode("utf-8", "replace")
                lines = diff.splitlines()
                item.diff = str(sanitize("\n".join(lines[:200])[:8000]))
                item.diff_truncated = len(lines) > 200 or len(diff) > 8000
            changes.append(item)
    working_status = git.list_status(workspace)
    return GitHeadReview(
        expected=expected,
        current=current,
        relation=relation,
        commits=commits,
        omitted_commits=omitted_commits,
        changes=changes,
        omitted_changes=omitted_changes,
        worktree_changes=working_status[:100],
        omitted_worktree_changes=max(0, len(working_status) - 100),
        assessment="Сравниваются два коммита. Незакоммиченная работа показана отдельно "
        "и сохраняется. Автор и намерение изменений не устанавливаются этой проверкой.",
    ), blockers
