"""A bounded file fingerprint for the private simulated workspace."""

import hashlib
from pathlib import Path

from agents_ide.errors import AppError


def fingerprint(root: Path) -> str:
    digest = hashlib.sha256()
    if not root.exists():
        return digest.hexdigest()
    if root.resolve() != root:
        raise AppError("path_violation", "Каталог simulation перенаправлен", 409)
    size = 0
    paths: list[Path] = []
    for path in root.rglob("*"):
        if len(paths) >= 10000:
            raise AppError("path_violation", "Слишком много файлов simulation", 409)
        paths.append(path)
    for path in sorted(paths):
        if (
            path.is_junction()
            or path.is_symlink()
            or not path.resolve().is_relative_to(root.resolve())
        ):
            raise AppError("path_violation", "Неподдержанный каталог simulation", 409)
        if path.is_file():
            size += path.stat().st_size
            if size > 32 * 1024 * 1024:
                raise AppError("path_violation", "Каталог simulation превышает 32 MiB", 409)
            digest.update(path.relative_to(root).as_posix().encode())
            digest.update(b"\0")
            with path.open("rb") as stream:
                digest.update(hashlib.file_digest(stream, "sha256").digest())
    return digest.hexdigest()
