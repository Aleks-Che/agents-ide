import os
import stat
import tempfile
from pathlib import Path

from agents_ide.errors import AppError


def private_directory(path: Path) -> None:
    """Refuse linked data directories; restrict access before writing any data."""
    for candidate in (path, *path.parents):
        if candidate.exists() and (
            candidate.is_symlink() or (os.name == "nt" and candidate.is_junction())
        ):
            raise AppError("path_violation", "Data directories cannot contain links")
    if os.name == "nt" and str(path).startswith("\\\\"):
        raise AppError("path_violation", "Data must be on a local disk")
    path.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        import ntsecuritycon
        import win32api
        import win32con
        import win32file
        import win32security

        if win32file.GetDriveType(str(path.anchor)) != win32con.DRIVE_FIXED:
            raise AppError("path_violation", "Data must be on a fixed local disk")
        if win32api.GetVolumeInformation(str(path.anchor))[4] != "NTFS":
            raise AppError("path_violation", "Data requires a local NTFS volume")
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            sid = win32security.GetTokenInformation(token, win32security.TokenUser)[0]
        finally:
            win32api.CloseHandle(token)
        acl = win32security.ACL()
        acl.AddAccessAllowedAceEx(
            win32security.ACL_REVISION,
            win32con.CONTAINER_INHERIT_ACE | win32con.OBJECT_INHERIT_ACE,
            ntsecuritycon.FILE_ALL_ACCESS,
            sid,
        )
        win32security.SetNamedSecurityInfo(
            str(path),
            win32security.SE_FILE_OBJECT,
            win32security.DACL_SECURITY_INFORMATION
            | win32security.PROTECTED_DACL_SECURITY_INFORMATION,
            None,
            None,
            acl,
            None,
        )
    else:
        path.chmod(stat.S_IRWXU)


def prepare_data_dir(path: Path) -> None:
    names = {"db", "artifacts", "logs", "secrets", "runtime", "temp"}
    if path == Path(path.anchor) or (
        path.exists() and any(entry.name not in names for entry in path.iterdir())
    ):
        raise AppError("path_violation", "Choose a dedicated application data directory")
    private_directory(path)
    for name in names:
        private_directory(path / name)


def atomic_write(path: Path, data: bytes) -> None:
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".write-")
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def directory_identity(path: Path) -> tuple[int, int]:
    """Python's Windows stat obtains the volume and file ID via a file handle."""
    resolved = path.resolve(strict=True)
    if not resolved.is_dir() or (os.name == "nt" and str(resolved).startswith("\\\\")):
        raise AppError("path_violation", "Expected a local directory")
    info = resolved.stat()
    if not info.st_ino:
        raise AppError("path_violation", "Directory identity is unavailable")
    return info.st_dev, info.st_ino
