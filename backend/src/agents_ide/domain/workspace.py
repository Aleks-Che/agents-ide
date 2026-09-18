"""Local filesystem identities and Git metadata; no repository mutations."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from agents_ide.errors import AppError
from agents_ide.security.filesystem import directory_identity


@dataclass(frozen=True)
class GitMetadata:
    root_path: str
    head_sha: str
    remote_url: str | None
    default_branch: str | None
    dirty: bool
    common_path: str


def normalize_path(entered: str) -> Path:
    if entered.startswith(("//", "\\\\")):
        raise AppError("path_unsupported", "UNC, сетевые и WSL пути не поддерживаются", 400)
    if not entered or "\x00" in entered or not Path(entered).is_absolute():
        raise AppError("path_invalid", "Ожидается абсолютный путь к каталогу", 400)
    try:
        path = Path(entered).resolve(strict=True)
        if not path.is_dir():
            raise AppError("path_invalid", "Ожидается каталог", 400)
        if os.name == "nt":
            import pywintypes
            import win32api
            import win32con
            import win32file

            if str(path).startswith("\\\\"):
                raise AppError("path_unsupported", "Сетевые пути не поддерживаются", 400)
            try:
                volume = win32file.GetVolumePathName(str(path))
                supported = (
                    win32file.GetDriveType(volume) == win32con.DRIVE_FIXED
                    and win32api.GetVolumeInformation(volume)[4] == "NTFS"
                )
            except pywintypes.error:
                raise AppError("path_unavailable", "Том недоступен для проверки", 400) from None
            if not supported:
                raise AppError("path_unsupported", "Нужен локальный NTFS-каталог", 400)
        return path
    except (OSError, RuntimeError):
        raise AppError("path_unavailable", "Каталог недоступен", 404) from None


def collect_workspace(entered: str) -> tuple[str, str, int, int, GitMetadata | None]:
    normalized = normalize_path(entered)
    dev, ino = directory_identity(normalized)
    return entered, str(normalized), dev, ino, detect_git(normalized)


def _git(path: Path, args: list[str], *, optional: bool = False) -> str | None:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment["GIT_OPTIONAL_LOCKS"] = "0"
    creation_flags = 0
    if sys.platform == "win32":
        creation_flags = subprocess.CREATE_NO_WINDOW
    try:
        result = subprocess.run(
            [
                "git",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.quotePath=false",
                "-C",
                str(path),
                *args,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            timeout=10,
            env=environment,
            creationflags=creation_flags,
        )
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        raise AppError("git_unavailable", "Не удалось проверить Git-каталог", 409) from None
    if result.returncode:
        if optional or "not a git repository" in result.stderr:
            return None
        raise AppError("git_unavailable", "Git-каталог недоступен для проверки", 409)
    return result.stdout.strip() or None


def detect_git(path: Path) -> GitMetadata | None:
    if shutil.which("git") is None:
        raise AppError("git_unavailable", "Git нужен для проверки общей рабочей области", 409)
    root = _git(path, ["rev-parse", "--show-toplevel"])
    if root is None:
        return None
    root_path = normalize_path(root)
    head = _git(root_path, ["rev-parse", "--verify", "HEAD"], optional=True)
    remote = _git(root_path, ["config", "--get", "remote.origin.url"], optional=True)
    if remote and "://" in remote:
        parts = urlsplit(remote)
        remote = urlunsplit((parts.scheme, parts.netloc.rsplit("@", 1)[-1], parts.path, "", ""))
    branch = _git(root_path, ["symbolic-ref", "--short", "HEAD"], optional=True)
    common = _git(root_path, ["rev-parse", "--path-format=absolute", "--git-common-dir"])
    return GitMetadata(
        str(root_path),
        head or "",
        remote,
        branch,
        bool(_git(root_path, ["status", "--porcelain"])),
        str(normalize_path(common)) if common else str(root_path),
    )


def assert_identity_matches(target: Path, dev: int, ino: int) -> None:
    path = normalize_path(str(target))
    if directory_identity(path) != (dev, ino):
        raise AppError("workspace_conflict", "Каталог изменил идентичность", 409)


def workspace_scope(path: Path, git: GitMetadata | None = None) -> dict[str, Any]:
    """Identity ancestry detects aliases/overlap without string-prefix checks."""
    normalized = normalize_path(str(path))
    return {
        "normalized_path": str(normalized),
        "identity": list(directory_identity(normalized)),
        "ancestors": [list(directory_identity(parent)) for parent in normalized.parents],
        "git_common_identity": list(directory_identity(Path(git.common_path))) if git else None,
    }


def scopes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return (
        left["identity"] == right["identity"]
        or left["identity"] in right["ancestors"]
        or right["identity"] in left["ancestors"]
        or bool(
            left.get("git_common_identity")
            and left["git_common_identity"] == right.get("git_common_identity")
        )
    )


def reservations_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Distinct managed worktrees may share a repository, never a working directory.

    Before checkout, each reservation holds the source identity plus its immutable
    destination. Ordinary/legacy reservations keep the conservative repository lock.
    """
    left_target, right_target = left.get("worktree_path"), right.get("worktree_path")
    if left_target and right_target:
        left_path, right_path = Path(left_target), Path(right_target)
        return left_path.is_relative_to(right_path) or right_path.is_relative_to(left_path)
    return scopes_overlap(left, right)


def touch_workspace(entered: str) -> dict[str, object]:
    entered_path, normalized, dev, ino, git = collect_workspace(entered)
    payload: dict[str, object] = {
        "entered_path": entered_path,
        "normalized_path": normalized,
        "identity_dev": dev,
        "identity_ino": ino,
    }
    if git:
        payload["git"] = {
            "root_path": git.root_path,
            "head_sha": git.head_sha,
            "remote_url": git.remote_url,
            "default_branch": git.default_branch,
            "dirty": git.dirty,
        }
    return payload
