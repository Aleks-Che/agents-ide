"""Bounded reads verified against the opened handle, including on Windows."""

from __future__ import annotations

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


def read_workspace_file(root: Path, relative: str, cap: int) -> bytes:
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
        if before.st_size > cap:
            raise ValueError("too_large")
        raw = stream.read(cap + 1)
        after = os.fstat(stream.fileno())
        if len(raw) > cap:
            raise ValueError("too_large")
        if (before.st_ino, before.st_size, before.st_mtime_ns, before.st_nlink) != (
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_nlink,
        ) or target.stat().st_ino != before.st_ino:
            raise ValueError("unstable_file")
        return raw
