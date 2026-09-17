"""Capture a bounded Council read snapshot while app writers cannot start."""

import json
import sys
from contextlib import ExitStack
from pathlib import Path, PureWindowsPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from agents_ide.domain.common import content_hash
from agents_ide.domain.workspace import collect_workspace, scopes_overlap, workspace_scope
from agents_ide.engine.artifacts import sanitize
from agents_ide.engine.context_sources import _read_file, _safe_relative
from agents_ide.errors import AppError
from agents_ide.persistence.models import Project, WorkspaceReservation
from agents_ide.security.workspace_read import hold_command_directory


def capture_context(
    session: Session, project: Project, text: str, paths: list[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not paths:
        return {"kind": "provided_text", "context_section": sanitize(text)}, {
            "mode": "no_workspace_access"
        }
    # The caller holds BEGIN IMMEDIATE throughout capture. Queue claims and
    # reservations share this lock; already-active writers are rejected below.
    _, normalized, dev, ino, git = collect_workspace(project.workspace_normalized_path)
    root = Path(normalized)
    scope = workspace_scope(root, git)
    for reservation in session.scalars(
        select(WorkspaceReservation).where(WorkspaceReservation.released_at.is_(None))
    ):
        if reservation.workspace_json is None or scopes_overlap(
            scope, json.loads(reservation.workspace_json)
        ):
            raise AppError(
                "planning_workspace_busy",
                "Сначала завершите или остановите Run, владеющий областью чтения",
                409,
            )
    files: list[dict[str, Any]] = []
    with ExitStack() as stack:
        for relative in paths:
            if not _safe_relative(relative):
                raise AppError(
                    "planning_context_denied", "Недопустимый или защищённый путь контекста", 422
                )
            path = root.joinpath(*PureWindowsPath(relative).parts)
            try:
                stack.enter_context(hold_command_directory(root, path.parent))
                if sys.platform == "win32":
                    import pywintypes
                    import win32con
                    import win32file

                    try:
                        handle = win32file.CreateFile(
                            str(path),
                            win32con.GENERIC_READ,
                            win32con.FILE_SHARE_READ,
                            None,
                            win32con.OPEN_EXISTING,
                            0,
                            None,
                        )
                    except pywintypes.error as exc:
                        raise OSError("Context file unavailable") from exc
                    stack.callback(handle.Close)
            except (OSError, ValueError):
                raise AppError(
                    "planning_context_unavailable",
                    "Файл изменяется, недоступен или находится за пределами проекта",
                    409,
                ) from None
        for relative in paths:
            file, omission = _read_file(root, relative, 64 * 1024)
            if file is None or omission:
                raise AppError(
                    "planning_context_unavailable", "Не удалось зафиксировать запрошенный файл", 409
                )
            files.append(file)
        if sum(file["bytes"] for file in files) > 256 * 1024:
            raise AppError("planning_context_limit", "Контекст файлов превышает 256 КиБ", 422)
        # Detect external writers on platforms without mandatory sharing locks.
        for relative, saved in zip(paths, files, strict=True):
            current, omission = _read_file(root, relative, 64 * 1024)
            if current is None or omission or current["sha256"] != saved["sha256"]:
                raise AppError("planning_context_changed", "Файлы изменились во время чтения", 409)
    packet = {"provided": sanitize(text), "files": files}
    return (
        {
            "kind": "read_snapshot",
            "context_section": json.dumps(packet, ensure_ascii=False),
            "manifest": [{k: v for k, v in file.items() if k != "content"} for file in files],
        },
        {
            "mode": "read_snapshot",
            "scope": scope,
            "identity_dev": dev,
            "identity_ino": ino,
            "snapshot_hash": content_hash(packet),
        },
    )
