"""Compare and explicitly accept selected protected-file changes before Git intent."""

from __future__ import annotations

import difflib
import hashlib
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field, ValidationError
from sqlalchemy.orm import Session

from agents_ide.domain.common import to_json
from agents_ide.domain.schemas import ApiModel, ApiOutput, RunCommand
from agents_ide.domain.workspace import collect_workspace, workspace_scope
from agents_ide.engine import artifacts
from agents_ide.engine import git_commit as git
from agents_ide.engine.git_process import GitTransport, run_git, using_transport
from agents_ide.engine.run_configuration import effective_snapshot
from agents_ide.engine.worktrees import effective_workspace
from agents_ide.errors import AppError
from agents_ide.persistence.models import Run
from agents_ide.security.workspace_read import read_workspace_file
from agents_ide.services.git_head_changes import GitHeadReview, compare_head, head_boundary
from agents_ide.services.mapping import get_or_404
from agents_ide.services.run_controls import _git_check_retry, unsettled_attempts
from agents_ide.worker.processes import stored_processes_stopped


class GitFileState(ApiOutput):
    exists: bool
    size: int | None
    ignored: bool
    mode: str | None


class GitProtectedChange(ApiOutput):
    path: str
    change: Literal["added", "modified", "removed"]
    before: GitFileState | None
    after: GitFileState | None
    changed_fields: list[str]
    risk: Literal["low", "medium", "high", "unknown"]
    assessment: str
    comparison: Literal["text", "metadata_only", "hidden"]
    comparison_note: str
    diff: str | None = None
    diff_truncated: bool = False


class GitChangesReview(ApiOutput):
    run_id: str
    attempt_id: str | None
    state_version: int
    reviewed_at: float
    comparison_id: str
    workspace_path: str
    changes: list[GitProtectedChange]
    omitted_changes: int
    blockers: list[str]
    can_accept: bool
    effect: str
    kind: Literal["protected_files", "head"] = "protected_files"
    head: GitHeadReview | None = None
    checks_performed: list[str] = Field(default_factory=list)


class GitChangesAcceptance(ApiModel):
    comparison_id: str = Field(pattern=r"^[a-f0-9]{64}$")
    paths: list[str] = Field(default_factory=list, max_length=100)
    accept_head: bool = False
    acknowledge_risk: Literal[True]


def _digest(value: Any) -> str:
    return hashlib.sha256(to_json(value).encode()).hexdigest()


def _state(value: dict[str, Any] | None) -> GitFileState | None:
    return (
        GitFileState(
            exists=not value.get("missing", False),
            size=value.get("size"),
            ignored=bool(value.get("ignored")),
            mode=value.get("mode"),
        )
        if value is not None
        else None
    )


def _sensitive(path: str) -> bool:
    parts = PurePosixPath(path.lower()).parts
    return any(
        part == ".env"
        or part.startswith(".env.")
        or part in {".ssh", "id_rsa", "id_ed25519"}
        or re.search(r"secret|credential|password|token", part)
        for part in parts
    ) or PurePosixPath(path.lower()).suffix in {".pem", ".key", ".p12", ".pfx"}


def _text_diff(
    workspace: Path,
    path: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    entries: dict[str, Any],
) -> tuple[str | None, str, bool]:
    """Only compare verified original bytes. A Git blob need not equal a dirty baseline."""
    limit = 64 * 1024
    if max((before or {}).get("size", 0), (after or {}).get("size", 0)) > limit:
        return None, "Файл больше 64 КиБ: доступно сравнение метаданных.", False
    old = b""
    if before and not before.get("missing"):
        oid = entries.get(path, {}).get("sha256", "")
        if not re.fullmatch(r"[a-f0-9]{40,64}", oid):
            return (
                None,
                "Исходный текст не сохранён; отпечаток не позволяет восстановить строки.",
                False,
            )
        if int(run_git(workspace, ["cat-file", "-s", oid]).strip()) > limit:
            return None, "Исходный объект больше 64 КиБ.", False
        old = run_git(workspace, ["cat-file", "blob", oid])
        if hashlib.sha256(old).hexdigest() != before.get("sha256"):
            return (
                None,
                "Версия в Git не совпадает с исходным отпечатком; построчный diff недостоверен.",
                False,
            )
    new = b""
    if after and not after.get("missing"):
        new = read_workspace_file(workspace, path, limit)
        if hashlib.sha256(new).hexdigest() != after.get("sha256"):
            raise AppError(
                "git_comparison_stale", "Файл изменился при сравнении. Обновите данные.", 409
            )
    try:
        if b"\0" in old or b"\0" in new:
            raise UnicodeError()
        old_text, new_text = old.decode("utf-8"), new.decode("utf-8")
    except UnicodeError:
        return None, "Бинарный файл или текст не в UTF-8: доступно сравнение метаданных.", False
    lines = list(
        difflib.unified_diff(
            old_text.splitlines(),
            new_text.splitlines(),
            fromfile="Исходное состояние",
            tofile="Текущее состояние",
            lineterm="",
        )
    )
    diff = "\n".join(lines[:200])
    truncated = len(lines) > 200 or len(diff) > 8000
    return (
        str(artifacts.sanitize(diff[:8000])),
        "Исходные байты сверены с сохранённым отпечатком.",
        truncated,
    )


def _change(
    workspace: Path,
    path: str,
    before: dict[str, Any] | None,
    after: dict[str, Any] | None,
    entries: dict[str, Any],
    *,
    include_diff: bool,
) -> GitProtectedChange:
    change: Literal["added", "modified", "removed"] = (
        "added"
        if before is None
        else "removed"
        if not after or after.get("missing")
        else "modified"
    )
    fields = [
        key
        for key in ("sha256", "size", "mode", "ignored", "missing")
        if (before or {}).get(key) != (after or {}).get(key)
    ]
    sensitive = _sensitive(path)
    suffix = PurePosixPath(path.lower()).suffix
    risk: Literal["low", "medium", "high", "unknown"] = "unknown"
    assessment = "Влияние на работу не установлено: требуется проверка назначения файла."
    if (
        sensitive
        or change == "removed"
        or "mode" in fields
        or suffix
        in {
            ".py",
            ".js",
            ".ts",
            ".tsx",
            ".jsx",
            ".json",
            ".toml",
            ".yaml",
            ".yml",
            ".ini",
            ".sh",
            ".ps1",
            ".bat",
            ".cmd",
            ".sql",
            ".exe",
            ".dll",
        }
        or PurePosixPath(path).name.lower()
        in {".gitignore", ".gitattributes", "dockerfile", "makefile"}
    ):
        risk, assessment = (
            "high",
            "Код, настройки, секреты, удаление или режим файла могут влиять на работу.",
        )
    elif suffix == ".log" and before and before.get("ignored") and after and after.get("ignored"):
        risk, assessment = (
            "medium",
            (
                "По имени и политике это игнорируемый журнал. Вероятное влияние на код небольшое, "
                "но назначение и безвредность содержимого не подтверждены."
            ),
        )
    diff, note, truncated = None, "Сравнение содержимого не запрашивалось.", False
    if sensitive:
        note = "Содержимое потенциально чувствительного файла скрыто."
    elif include_diff:
        diff, note, truncated = _text_diff(workspace, path, before, after, entries)
    elif before and path not in entries:
        note = "Для исходного файла сохранены только метаданные; построчное сравнение недоступно."
    if (
        risk == "unknown"
        and diff is not None
        and not truncated
        and suffix in {".md", ".txt", ".rst"}
    ):
        risk, assessment = (
            "low",
            "Изменён текстовый документ. Оценка по типу файла; проверьте строки diff.",
        )
    return GitProtectedChange(
        path=path,
        change=change,
        before=_state(before),
        after=_state(after),
        changed_fields=fields,
        risk=risk,
        assessment=assessment,
        comparison="hidden" if sensitive else "text" if diff is not None else "metadata_only",
        comparison_note=note,
        diff=diff,
        diff_truncated=truncated,
    )


def _review(
    session: Session,
    run: Run,
    *,
    include_diff: bool,
) -> tuple[GitChangesReview, dict[str, Any], dict[str, Any]]:
    runtime = json.loads(run.runtime_json or "{}")
    snapshot = effective_snapshot(run)
    state = runtime.get("git") or {}
    baseline = state.get("baseline")
    if not baseline:
        raise AppError("git_baseline_missing", "Нет исходного состояния Git для сравнения.", 409)
    workspace = Path(effective_workspace(snapshot, runtime)["workspace_path"])
    waiting = json.loads(run.waiting_reason_json or "null") or runtime.get("waiting_reason") or {}
    blockers = []
    if run.state not in {"waiting_input", "paused", "stopped"}:
        blockers.append("Принятие возможно только у остановившегося запуска.")
    eligible = _git_check_retry(session, run, snapshot, waiting)
    boundary = head_boundary(run)
    if not eligible and not boundary:
        blockers.append("Нужен остановившийся Git-этап до сохранения намерения коммита.")
    if not stored_processes_stopped(session, run):
        blockers.append("Остановка прежних процессов не подтверждена.")
    if any(attempt.id != eligible for attempt in unsettled_attempts(session, run)):
        blockers.append("Есть другие операции с неподтверждённым результатом.")
    with using_transport(GitTransport(deadline=time.monotonic() + 15)):
        expected_workspace = effective_workspace(snapshot, runtime)
        _, normalized, dev, ino, repository = collect_workspace(str(workspace))
        if [dev, ino] != [
            expected_workspace.get("identity_dev"),
            expected_workspace.get("identity_ino"),
        ] or workspace_scope(Path(normalized), repository).get(
            "git_common_identity"
        ) != expected_workspace.get("scope", {}).get("git_common_identity"):
            blockers.append("Изменилась сама рабочая область запуска.")
        head, branch = git.read_head_sha(workspace), git.read_branch(workspace)
        fingerprint = git.git_fingerprint(workspace)
        index = git.index_hash(workspace)
        lock_path = Path(
            run_git(workspace, ["rev-parse", "--git-path", "index.lock"]).decode().strip()
        )
        if not lock_path.is_absolute():
            lock_path = workspace / lock_path
        if lock_path.exists():
            blockers.append("Индекс Git заблокирован другой операцией.")
        if branch != state.get("branch") or (head != state.get("head") and not boundary):
            blockers.append(
                "Ветка отличается от ожидаемой. Принятие HEAD не разрешает смену ветки."
                if boundary
                else "Ветка или HEAD отличаются от ожидаемых; принятие файлов это не исправляет."
            )
        if not git.git_policy_matches(fingerprint, baseline.get("fingerprint", {})):
            blockers.append("Изменились Git hooks или настройки подписи.")
        if boundary and runtime.get("git_paused_workspace_hash"):
            from agents_ide.engine.context_sources import workspace_hash

            if workspace_hash(workspace) != runtime["git_paused_workspace_hash"]:
                blockers.append(
                    "Изменился снимок файлов, сохранённый при паузе. "
                    "Принятие HEAD не снимает эту блокировку."
                )
        if any(
            item["code"] != "??" and item["code"][0] != "." for item in git.list_status(workspace)
        ):
            blockers.append("Индекс содержит подготовленные изменения.")
        manifest = git.file_manifest(workspace)
        previous = baseline.get("protected", {})
        allowed = baseline.get("allowlist") or state.get("allowlist", [])
        current = {
            path: value
            for path, value in manifest.items()
            if path in previous
            or (not value.get("ignored") and not git.is_path_allowed(path, allowed))
        }
        changed = sorted(
            path
            for path in previous.keys() | current.keys()
            if previous.get(path) != current.get(path)
        )
        entries = {item["path"]: item for item in baseline.get("manifest", [])}
        changes = [
            _change(
                workspace,
                path,
                previous.get(path),
                current.get(path),
                entries,
                include_diff=include_diff,
            )
            for path in changed[:100]
        ]
        head_review = None
        if boundary:
            head_review, head_blockers = compare_head(
                workspace, state.get("head") or "", head, allowed, include_diff=include_diff
            )
            blockers.extend(head_blockers)
            if changed:
                blockers.append(
                    "Изменены защищённые файлы. Принятие HEAD не принимает эти расхождения."
                )
    comparison_id = _digest(
        {
            "run": run.id,
            "version": run.state_version,
            "attempt": run.current_attempt_id,
            "workspace": str(workspace.resolve()),
            "baseline": baseline,
            "manifest": manifest,
            "head": head,
            "branch": branch,
            "index": index,
            "fingerprint": fingerprint,
            "resume_target": run.resume_target_json,
            "kind": "head" if boundary else "protected_files",
        }
    )
    return (
        GitChangesReview(
            run_id=run.id,
            attempt_id=run.current_attempt_id,
            state_version=run.state_version,
            reviewed_at=time.time(),
            comparison_id=comparison_id,
            workspace_path=str(workspace),
            changes=changes,
            omitted_changes=max(0, len(changed) - 100),
            blockers=blockers,
            can_accept=(head != state.get("head") if boundary else bool(changes)) and not blockers,
            kind="head" if boundary else "protected_files",
            head=head_review,
            checks_performed=[
                "workspace_identity",
                "processes_stopped",
                "unsettled_attempts",
                "head",
                "branch",
                "index",
                "index_lock",
                "hooks",
                "signing",
                "protected_files",
                *(["head_ancestry", "committed_paths"] if boundary else []),
            ],
            effect=(
                "Принятие сохраняет текущий HEAD как ожидаемую основу следующего этапа. "
                "Коммиты, файлы, индекс и защита файлов не меняются. "
                "Незакоммиченная работа сохраняется. Затем отдельно нажмите «Продолжить»."
                if boundary
                else "Принятие обновляет эталон только выбранных защищённых файлов. "
                "Содержимое файлов, ветка, индекс и разрешённые пути не меняются. "
                "Игнорируемый файл не попадёт в коммит. Продолжение запуска выполняется отдельно."
            ),
        ),
        previous,
        current,
    )


def review_changes(session: Session, run_id: str, *, include_diff: bool = True) -> GitChangesReview:
    run = get_or_404(session, Run, run_id)
    return _review(session, run, include_diff=include_diff)[0]


def resolution_status(run: Run) -> dict[str, Any]:
    if run.state not in {"waiting_input", "paused", "stopped"}:
        return {}
    runtime = json.loads(run.runtime_json or "{}")
    waiting = json.loads(run.waiting_reason_json or "null") or runtime.get("waiting_reason") or {}
    if waiting.get("code") != "external_change_detected" or not runtime.get("git", {}).get(
        "baseline"
    ):
        return {}
    boundary = head_boundary(run)
    if not boundary and not any(
        node["id"] == run.current_node_id and node.get("type") == "GitCommit"
        for node in effective_snapshot(run).get("graph", {}).get("nodes", [])
    ):
        return {
            "can_review": False,
            "accepted": False,
            "unavailable_reason": "Принятие доступно для защищённых файлов перед GitCommit "
            "или HEAD перед новым AgentTask без начатой попытки.",
        }
    if boundary:
        accepted_head = runtime["git"].get("accepted_head", {})
        return {
            "can_review": True,
            "kind": "head",
            "attempt_id": None,
            "accepted": accepted_head.get("node_id") == run.current_node_id
            and accepted_head.get("head") == runtime["git"].get("head"),
        }
    accepted = runtime["git"].get("accepted_changes", {})
    return {
        "can_review": True,
        "kind": "protected_files",
        "attempt_id": run.current_attempt_id,
        "accepted": bool(
            run.current_attempt_id
            and accepted.get("attempt_id") == run.current_attempt_id
            and accepted.get("remaining_changes") == 0
        ),
    }


def accept_changes(session: Session, run: Run, command: RunCommand) -> dict[str, Any]:
    if set(command.payload) != {"git_changes"}:
        raise AppError("resolution_invalid", "Принятие Git-изменений отправляется отдельно.", 422)
    try:
        decision = GitChangesAcceptance.model_validate(command.payload["git_changes"])
    except ValidationError as error:
        raise AppError(
            "resolution_invalid", "Нужны сравнение, выбранные пути и подтверждение риска.", 422
        ) from error
    # Repeat ALL guard checks and compare the complete manifest, not client-provided hashes.
    review, previous, current = _review(session, run, include_diff=True)
    if review.comparison_id != decision.comparison_id:
        raise AppError(
            "git_comparison_stale", "Состояние изменилось после сравнения. Обновите его.", 409
        )
    if review.blockers:
        raise AppError(
            "git_acceptance_blocked",
            "Принятие сейчас недоступно.",
            409,
            {"blockers": review.blockers},
        )
    if review.kind == "head":
        if not decision.accept_head or decision.paths or not review.can_accept or not review.head:
            raise AppError(
                "resolution_invalid", "Подтвердите принятие HEAD целиком, без выбора файлов.", 422
            )
        runtime = json.loads(run.runtime_json)
        audit = artifacts.record_artifact(
            session,
            run.id,
            artifacts.ArtifactPayload(
                "git_head_accepted",
                body={
                    "command_id": command.command_id,
                    "comparison_id": review.comparison_id,
                    "node_id": run.current_node_id,
                    "acknowledge_risk": True,
                    "comparison": review.head.model_dump(
                        exclude={"changes": {"__all__": {"diff"}}}
                    ),
                },
            ),
            source_kind="api",
        )
        runtime["git"]["head"] = review.head.current
        runtime["git"]["accepted_head"] = {
            "node_id": run.current_node_id,
            "head": review.head.current,
            "artifact_id": audit.id,
        }
        run.runtime_json = to_json(runtime)
        return {
            "applied": True,
            "artifact_id": audit.id,
            "accepted_head": review.head.current,
            "remaining_changes": 0,
            "next_action": "resume",
        }
    paths = set(decision.paths)
    changes = {change.path: change for change in review.changes}
    if (
        decision.accept_head
        or not paths
        or len(paths) != len(decision.paths)
        or not paths <= changes.keys()
    ):
        raise AppError(
            "resolution_invalid", "Выберите уникальные пути из актуального сравнения.", 422
        )
    runtime = json.loads(run.runtime_json)
    state = runtime["git"]
    audit = artifacts.record_artifact(
        session,
        run.id,
        artifacts.ArtifactPayload(
            "git_changes_accepted",
            body={
                "command_id": command.command_id,
                "comparison_id": review.comparison_id,
                "attempt_id": run.current_attempt_id,
                "acknowledge_risk": True,
                "changes": [
                    {
                        "path": path,
                        "before": previous.get(path),
                        "after": current.get(path),
                        "risk": changes[path].risk,
                        "assessment": changes[path].assessment,
                    }
                    for path in sorted(paths)
                ],
            },
        ),
        source_kind="api",
        step_execution_id=run.current_execution_id,
        step_attempt_id=run.current_attempt_id,
    )
    for path in paths:
        if path in current:
            state["baseline"]["protected"][path] = current[path]
        else:
            state["baseline"]["protected"].pop(path, None)
    remaining = len(review.changes) + review.omitted_changes - len(paths)
    state["accepted_changes"] = {
        "attempt_id": run.current_attempt_id,
        "artifact_id": audit.id,
        "remaining_changes": remaining,
    }
    run.runtime_json = to_json(runtime)
    return {
        "applied": True,
        "artifact_id": audit.id,
        "accepted_paths": sorted(paths),
        "remaining_changes": remaining,
        "next_action": "resume" if not remaining else "review",
    }


def check_head_resume(session: Session, run: Run) -> bool:
    """Only a reviewed boundary decision authorizes this otherwise blocked resume."""
    if not head_boundary(run):
        return False
    state = json.loads(run.runtime_json).get("git", {})
    accepted = state.get("accepted_head", {})
    if accepted.get("node_id") != run.current_node_id or accepted.get("head") != state.get("head"):
        return False
    review = _review(session, run, include_diff=False)[0]
    if review.blockers or not review.head or review.head.relation != "same":
        raise AppError(
            "git_acceptance_stale",
            "После принятия состояние изменилось. Откройте сравнение снова.",
            409,
        )
    return True
