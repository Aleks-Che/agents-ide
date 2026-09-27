"""Bounded reads verified against the opened handle, including on Windows."""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any


@contextmanager
def hold_command_directory(root: Path, cwd: Path) -> Iterator[None]:
    """Keep Windows directory components from being replaced during CreateProcess."""
    root, cwd = root.resolve(strict=True), cwd.resolve(strict=True)
    if not cwd.is_relative_to(root):
        raise ValueError("outside_workspace")
    handles: list[Any] = []
    try:
        current = cwd
        while True:
            if current.is_symlink() or current.is_junction():
                raise ValueError("linked_path")
            if sys.platform == "win32":
                import pywintypes
                import win32con
                import win32file

                try:
                    handle = win32file.CreateFile(
                        str(current),
                        win32con.GENERIC_READ,
                        win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
                        None,
                        win32con.OPEN_EXISTING,
                        win32con.FILE_FLAG_BACKUP_SEMANTICS,
                        None,
                    )
                except pywintypes.error as exc:
                    raise OSError("Workspace directory unavailable") from exc
                handles.append(handle)
                final = Path(
                    str(win32file.GetFinalPathNameByHandle(int(handle), 0)).removeprefix("\\\\?\\")
                )
                if final != current or not final.is_relative_to(root):
                    raise ValueError("outside_workspace")
            if current == root:
                break
            current = current.parent
        yield
    finally:
        for handle in reversed(handles):
            handle.Close()


class WorkspaceFileTooLarge(ValueError):
    def __init__(self, size: int, cap: int):
        super().__init__("too_large")
        self.size = size
        self.cap = cap


def read_workspace_file(root: Path, relative: str, cap: int | None) -> bytes:
    return b"".join(_workspace_file_chunks(root, relative, cap))


def hash_workspace_file(root: Path, relative: str, cap: int | None) -> tuple[str, int]:
    """Fingerprint large files without retaining their contents in memory."""
    digest = hashlib.sha256()
    size = 0
    for chunk in _workspace_file_chunks(root, relative, cap):
        digest.update(chunk)
        size += len(chunk)
    return digest.hexdigest(), size


def _workspace_file_chunks(root: Path, relative: str, cap: int | None) -> Iterator[bytes]:
    root = root.resolve(strict=True)
    target = root / relative
    if not target.is_relative_to(root):
        raise ValueError("outside_workspace")
    for part in (target, *target.parents):
        if part == root:
            break
        if part.is_symlink() or part.is_junction():
            raise ValueError("linked_path")
    with target.open("rb") as stream:
        before = os.fstat(stream.fileno())
        # A harmless filename can be a hard link to a protected file. Its final
        # handle path still looks safe, so pathname checks alone are insufficient.
        if before.st_nlink != 1:
            raise ValueError("linked_file")
        if sys.platform == "win32":
            import msvcrt

            import win32file

            final = Path(
                win32file.GetFinalPathNameByHandle(msvcrt.get_osfhandle(stream.fileno()), 0)
            )
            # GetFinalPathNameByHandle returns a device prefix on Windows.
            final = Path(str(final).removeprefix("\\\\?\\"))
        else:
            final = Path(f"/proc/self/fd/{stream.fileno()}").resolve(strict=True)
        if final != target or not final.is_relative_to(root):
            raise ValueError("outside_workspace")
        if cap is not None and before.st_size > cap:
            raise WorkspaceFileTooLarge(before.st_size, cap)
        # Bound memory even with no volume limit. Read at most the initial size
        # plus one byte, so growth is detected and an active writer cannot keep
        # an unlimited read going indefinitely.
        size = 0
        while size <= before.st_size:
            chunk = stream.read(min(1024 * 1024, before.st_size + 1 - size))
            if not chunk:
                break
            size += len(chunk)
            if cap is not None and size > cap:
                raise WorkspaceFileTooLarge(size, cap)
            yield chunk
        after = os.fstat(stream.fileno())
        if (
            size != before.st_size
            or (before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink)
            != (
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_nlink,
            )
            or target.stat().st_ino != before.st_ino
        ):
            raise ValueError("unstable_file")
